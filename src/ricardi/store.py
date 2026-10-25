"""What survives a restart: thread bindings, and each agent's session.

Which agent each Slack thread is talking to.

A thread is the natural unit of conversation in Slack and the natural unit of
session in Claude Code, so Ricardi binds one to the other and keeps the
binding on disk. Surviving a restart matters: the humans in the channel will
carry on in the same thread afterwards and expect to be understood.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class ThreadBindings:
    """Which agent a thread is talking to, and what it was asked to give up.

    The flags stick to the thread rather than the message. Someone opens a
    thread with `-ro` and leaves; the next person replies without it. Reading
    that reply as "no flags" would hand back exactly what the first person was
    trying to prevent, so a flag stays until it is lifted on purpose.
    """

    def __init__(self, path: str | os.PathLike[str] | None):
        # None keeps bindings in memory only, which is what the tests want.
        self._path = Path(path) if path else None
        self._bound: dict[str, dict] = {}
        if self._path and self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A corrupt binding file must not stop Ricardi answering; the
                # cost is that threads need naming again.
                raw = {}
            self._bound = {t: _entry(v) for t, v in (raw or {}).items()}

    def get(self, thread: str) -> str:
        return self._bound.get(thread, {}).get("agent", "")

    def flags(self, thread: str) -> tuple[str, ...]:
        return tuple(self._bound.get(thread, {}).get("flags", ()))

    def bind(self, thread: str, agent: str, flags: tuple[str, ...] | None = None) -> None:
        """Bind, and narrow. `flags=None` leaves the thread's flags alone.

        New flags are unioned with the ones already there rather than replacing
        them, because every flag subtracts: adding one is always safe, and
        dropping one is the thing that needs saying out loud. See `lift`.
        """
        before = self._bound.get(thread, {})
        held = tuple(before.get("flags", ()))
        now = tuple(sorted(set(held) | set(flags or ())))
        if before.get("agent") == agent and held == now:
            return
        self._bound[thread] = {"agent": agent, "flags": list(now)}
        self._flush()

    def lift(self, thread: str) -> None:
        """Drop the thread's flags. The only way they come off."""
        entry = self._bound.get(thread)
        if not entry or not entry.get("flags"):
            return
        entry["flags"] = []
        self._flush()

    def _flush(self) -> None:
        _write(self._path, self._bound)


def _entry(value) -> dict:
    """One binding, from either storage format.

    The file used to be a flat {thread: agent}. Refusing to read it would
    orphan every live conversation on the deploy that added flags.
    """
    if isinstance(value, str):
        return {"agent": value, "flags": []}
    if isinstance(value, dict):
        flags = value.get("flags")
        return {
            "agent": str(value.get("agent", "")),
            "flags": [str(f) for f in flags] if isinstance(flags, list) else [],
        }
    return {"agent": "", "flags": []}


class SessionIds:
    """Which Claude Code session each agent is holding.

    Without this an agent's conversation ends when Ricardi does. The transcript
    survives on disk either way, but the id needed to resume it lived only in
    memory, so a restart quietly started every agent over with no history --
    looking fine, and losing everything the channel had told it.
    """

    def __init__(self, path: str | os.PathLike[str] | None):
        self._path = Path(path) if path else None
        self._ids: dict[str, str] = {}
        if self._path and self._path.exists():
            try:
                self._ids = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A corrupt file costs the agents their memory, not their
                # ability to answer.
                self._ids = {}

    def get(self, agent: str) -> str:
        return self._ids.get(agent, "")

    def remember(self, agent: str, session_id: str) -> None:
        if not session_id or self._ids.get(agent) == session_id:
            return
        self._ids[agent] = session_id
        _write(self._path, self._ids)

    def forget(self, agent: str) -> None:
        if self._ids.pop(agent, None) is not None:
            _write(self._path, self._ids)


def _write(path: Path | None, body: dict[str, str]) -> None:
    """Replace a small JSON file atomically.

    Through a temporary file so a crash mid-write leaves the previous contents
    intact rather than a half-written file that fails to parse on restart --
    which is exactly when it would be read.
    """
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
