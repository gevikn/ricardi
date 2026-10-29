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
from pathlib import Path


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
"""

_COLUMNS = "person, name, verb, granted, by, at"


def normalise(email: str) -> str:
    """The person key. Case and stray whitespace must not make a second person."""
    return email.strip().lower()


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
                    (normalise(person), name, verb, int(granted), by, at),
                )
        except sqlite3.DatabaseError as exc:
            raise GrantError(f"cannot record the grant: {exc}") from exc

    def holds(self, person: str, name: str, verb: str) -> bool:
        row = self._query(
            "SELECT granted FROM grant_log WHERE person = ? AND name = ? AND verb = ?"
            " ORDER BY id DESC LIMIT 1",
            (normalise(person), name, verb),
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
