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


_SNAPSHOT_PREFIX = "grants-"


def _copy_database(conn: sqlite3.Connection, dest: Path) -> None:
    """sqlite's own backup API, which is the only thing that reads the WAL.

    Copying `grants.db` as a file is the obvious move and it silently loses
    everything: the auto-checkpoint does not fire until the WAL is about a
    thousand pages, so a store with a handful of rows keeps all of them --
    and the schema -- in `grants.db-wal`. The copy opens fine and reports no
    grants. This goes through sqlite instead, which takes the read lock it
    needs and walks the live pages.
    """
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
        # And out of WAL mode on the way out, so the snapshot is one file. The
        # backup inherits the source's journal mode, which would leave a -wal
        # and a -shm beside every snapshot -- reproducing, in the backup, the
        # exact three-file trap that makes copying the live store useless.
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target.execute("PRAGMA journal_mode=DELETE")
    finally:
        target.close()


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

    def backup_to(self, dest: str | os.PathLike[str]) -> None:
        """Copy the live store, WAL and all, to `dest`.

        Under the same lock as every other use of the connection: sqlite3
        connections are not safe to use concurrently, and a snapshot taken
        beside a seeding writer is exactly when that would bite.
        """
        try:
            with self._lock:
                _copy_database(self._conn, Path(dest))
        except (sqlite3.DatabaseError, OSError) as exc:
            raise GrantError(f"cannot snapshot the grant store: {exc}") from exc

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


def backup_store(store: GrantStore, into: str | os.PathLike[str], *,
                 stamp: str = "", keep: int = 14) -> Path:
    """Write a verified snapshot of `store` into `into`, and prune old ones.

    This is the one piece of state on the box that git cannot rebuild. A
    revoke that was recorded is not re-derivable from policy.toml -- that is
    the whole point of the mutable layer -- so losing the file loses the
    answer to "who let them in, and when".

    Verified, because an unchecked backup is a rumour: the copy is reopened,
    asked for `integrity_check`, and counted against the source. A snapshot
    that comes back short is deleted rather than kept, since a file that
    restores cleanly with rows missing is worse than no file at all -- it is
    discovered on the day it is needed. More rows than expected is fine: a
    grant recorded while the copy ran belongs in it.

    Returns the snapshot's path. Raises GrantError and leaves no file behind
    if anything about it cannot be trusted.
    """
    expected = len(store.history())
    folder = Path(into)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = folder / f"{_SNAPSHOT_PREFIX}{stamp}.db"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # A second run in the same second is a re-run, not a second snapshot.
        dest.unlink(missing_ok=True)
    except OSError as exc:
        raise GrantError(f"cannot write snapshots into {folder}: {exc}") from exc

    store.backup_to(dest)

    try:
        check = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
        try:
            verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
            rows = check.execute("SELECT COUNT(*) FROM grant_log").fetchone()[0]
        finally:
            check.close()
    except sqlite3.DatabaseError as exc:
        dest.unlink(missing_ok=True)
        raise GrantError(f"the snapshot at {dest} is not a readable store: {exc}") from exc

    if verdict != "ok" or rows < expected:
        dest.unlink(missing_ok=True)
        raise GrantError(
            f"the snapshot kept {rows} of {expected} rows (integrity: {verdict}), "
            "so it was discarded"
        )

    _prune(folder, keep)
    return dest


def _prune(folder: Path, keep: int) -> None:
    """Drop all but the newest `keep` snapshots. `keep` of 0 or less keeps all.

    Scoped to the names this module writes, which sort by their own stamp. A
    file someone put here by hand before a migration is not ours to delete.
    """
    if keep <= 0:
        return
    snapshots = sorted(folder.glob(f"{_SNAPSHOT_PREFIX}*.db"))
    for old in snapshots[:-keep]:
        try:
            old.unlink()
        except OSError as exc:
            # The snapshot is already written and verified; say what is true.
            raise GrantError(
                f"the snapshot was written, but {old} could not be pruned: {exc}"
            ) from exc
