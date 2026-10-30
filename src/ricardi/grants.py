"""Who has been granted what, as an append-only record.

policy.toml is reviewed and read-only; grants are the half that changes while
Ricardi is running. Each change is a new row that supersedes the last for the
same (person, name, verb), and nothing is ever updated or deleted: the history
is the storage model, so "who let them in, and when" is always answerable.

Unlike the rest of state/, this refuses to open on corruption. threads.json and
sessions.json shrug it off because losing them must not stop Ricardi answering;
a grant store we cannot read must not be guessed at, because a guess is either
access nobody approved or a lockout nobody noticed.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ricardi.policy import RulesPolicy


class GrantError(Exception):
    """The store could not be opened or used. Callers must treat it as a denial."""


@dataclass(frozen=True)
class Grant:
    person: str
    name: str
    verb: str
    granted: bool
    by: str
    at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS grant_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  person  TEXT NOT NULL,
  name    TEXT NOT NULL,
  verb    TEXT NOT NULL,
  granted INTEGER NOT NULL,
  by      TEXT NOT NULL,
  at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS grant_lookup ON grant_log (person, name, verb, id);
-- Append-only is a stated constraint, so the database enforces it rather than
-- relying on there being no method that would break it.
CREATE TRIGGER IF NOT EXISTS grant_log_no_update BEFORE UPDATE ON grant_log
BEGIN SELECT RAISE(ABORT, 'grant_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS grant_log_no_delete BEFORE DELETE ON grant_log
BEGIN SELECT RAISE(ABORT, 'grant_log is append-only'); END;
"""

_COLUMNS = "person, name, verb, granted, by, at"


def normalise(email: str) -> str:
    """The person key. Case and stray whitespace must not make a second person."""
    return email.strip().lower()


def normalise_name(value: str) -> str:
    """The agent-name and verb key, folded for the same reason emails are.

    Every other name comparison here is case-insensitive -- `app._resolve`
    matches a typed name against the catalogue with `.lower()`, and
    `policy._glob` lowers both sides -- so `Ricardi` and `ricardi` must not be
    two different grants. Without this, `grants add --name Ricardi` prints
    success, shows up in `grants list`, and still leaves the person refused.
    """
    return value.strip().lower()


def _grant(row: tuple) -> Grant:
    return Grant(row[0], row[1], row[2], bool(row[3]), row[4], row[5])


class GrantStore:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        # One connection is shared across threads (check_same_thread is off),
        # and sqlite3 connections are not safe to use concurrently.
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> GrantStore:
        p = Path(path)
        conn: sqlite3.Connection | None = None
        try:
            # A missing file is not corruption: connect creates it.
            p.parent.mkdir(parents=True, exist_ok=True)
            # Autocommit, so each add is durable and visible to the CLI's
            # connection the moment it returns.
            conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
            # WAL lets the CLI write beside the live container's reader. A file
            # that is not a database fails here, on the first real read.
            conn.execute("PRAGMA journal_mode=WAL")
            # Short on purpose. decide() reads on the event loop, so a read
            # stuck behind a seeding writer stalls every conversation for as
            # long as it waits; sqlite's 5s default is far too long. Failing
            # fast refuses that one turn, which beats freezing all of them.
            conn.execute("PRAGMA busy_timeout=250")
            conn.executescript(_SCHEMA)
        except (sqlite3.DatabaseError, OSError) as exc:
            if conn is not None:
                conn.close()
            raise GrantError(f"cannot open the grant store at {p}: {exc}") from exc
        return cls(conn)

    def add(self, *, person: str, name: str, verb: str, by: str,
            granted: bool = True, at: str = "") -> None:
        # The timestamp is for people reading history. Which row wins is
        # decided by insertion order, so a skewed clock cannot undo a revoke.
        at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO grant_log (person, name, verb, granted, by, at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (normalise(person), normalise_name(name), normalise_name(verb),
                     int(granted), by, at),
                )
        except sqlite3.DatabaseError as exc:
            raise GrantError(f"cannot record the grant: {exc}") from exc

    def holds(self, person: str, name: str, verb: str) -> bool:
        row = self._query(
            "SELECT granted FROM grant_log WHERE person = ? AND name = ? AND verb = ?"
            " ORDER BY id DESC LIMIT 1",
            (normalise(person), normalise_name(name), normalise_name(verb)),
        )
        return bool(row and row[0][0])

    def current(self) -> tuple[Grant, ...]:
        """Grants in force: the latest row per triple, if that row is a grant."""
        rows = self._query(
            f"SELECT {_COLUMNS} FROM grant_log WHERE granted = 1 AND id IN ("
            " SELECT MAX(id) FROM grant_log GROUP BY person, name, verb)"
            " ORDER BY id"
        )
        return tuple(_grant(r) for r in rows)

    def history(self) -> tuple[Grant, ...]:
        rows = self._query(f"SELECT {_COLUMNS} FROM grant_log ORDER BY id")
        return tuple(_grant(r) for r in rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        try:
            with self._lock:
                return self._conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError as exc:
            raise GrantError(f"cannot read the grant store: {exc}") from exc


def seed_from_policy(store: GrantStore, policy: RulesPolicy, *,
                     names: Sequence[str], by: str) -> int:
    """Grant `ask` to the roster on the names policy.toml already lets them ask.

    Once a missing grant means no access, the deploy that lands that would lock
    out everyone currently allowed. This writes down what policy.toml already
    permits so that deploy is not a flag day. It only ever subtracts: every row
    is a name from `names` (the agent catalogue), enumerated one by one, so the
    reach is exactly today's. A rule naming no `agents` matches every name and
    seeds every name; a rule naming a glob seeds the catalogue names that glob
    matches, because at decision time that is precisely what it permits. A
    wildcard row would also cover names added later, which must not be handed
    to everyone, so no row is ever a pattern. A rule scoped to `users` seeds
    only the roster members it names.

    Idempotent, and a triple with any history is left alone: a second run
    writes nothing, and cannot undo an admin's revoke. Returns rows written.
    """
    # Imported here: policy has no reason to depend on grants, and grants only
    # needs policy's matching when it seeds.
    from ricardi.policy import _any_glob

    seen = {(g.person, g.name, g.verb) for g in store.history()}
    emails = list(dict.fromkeys(normalise(e) for e in policy.roster.values() if e.strip()))
    catalogue = [n for n in names if n]
    written = 0
    for rule in policy.rules:
        if rule.effect != "allow" or (rule.actions and "ask" not in rule.actions):
            continue
        # A rule carrying any tool-shaped condition cannot match an `ask` at
        # all: `request.tool` is "" so `_tool_matches` fails on `_any_glob`'s
        # empty-value guard, `tool_input` is empty so `_paths` returns nothing
        # and both path conditions fail, and there is no `command` for
        # `command_matches`. It permits no asking, so there is none to seed --
        # and seeding it would put rows in `grants list` that are not in force.
        if rule.tools or rule.path_under or rule.path_matches or rule.command_matches:
            continue
        # `agents` is globbed in `Rule.matches`, so a pattern is expanded
        # against the catalogue rather than dropped. Skipping it would seed
        # nothing for a rule that today permits every name it matches, which
        # is the lockout this function exists to prevent.
        if rule.agents:
            allowed = [n for n in catalogue if _any_glob(n, rule.agents)]
        else:
            allowed = catalogue
        for email in emails:
            if rule.users and not _any_glob(email, rule.users):
                continue
            for name in allowed:
                key = (email, normalise_name(name), "ask")
                if key in seen:
                    continue
                store.add(person=email, name=name, verb="ask", by=by)
                seen.add(key)
                written += 1
    return written
