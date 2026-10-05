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
    def __init__(self, path: str | os.PathLike[str] | None):
        # None keeps bindings in memory only, which is what the tests want.
        self._path = Path(path) if path else None
        self._bound: dict[str, str] = {}
        if self._path and self._path.exists():
            try:
                self._bound = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # A corrupt binding file must not stop Ricardi answering; the
                # cost is that threads need naming again.
                self._bound = {}

    def get(self, thread: str) -> str:
        return self._bound.get(thread, "")

    def bind(self, thread: str, agent: str) -> None:
        if self._bound.get(thread) == agent:
            return
        self._bound[thread] = agent
        self._flush()

    def _flush(self) -> None:
        _write(self._path, self._bound)


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
