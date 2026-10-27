"""Whether this process is actually serving, in a file the host can read.

The deploy needs to answer one question after a restart: is it up? Reading
that out of the container log turned out not to work. On the box the log
loses its newest lines within minutes, so a gate that greps it either passes
on a line from an earlier boot -- the right answer for the wrong reason, and
indistinguishable from a container that never came up -- or fails on a
container that is perfectly healthy.

A marker in the state directory has none of that in the way. `state/` is
already a bind mount, so the host sees the file the moment the process writes
it, with no log driver, no rotation and no `--since` arithmetic in between.

The deploy compares `connected_at` against the container's `StartedAt`, not
against when the deploy ran: an unchanged image is not recreated, and a gate
that expects something new every time would fail that no-op deploy.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

NAME = "ready.json"

# `date -u -d` parses this, and it carries no fraction and no local offset --
# the two ways a timestamp looks right here and is wrong by hours there.
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def path_for(state_dir: str | os.PathLike[str]) -> Path:
    return Path(state_dir) / NAME


def clear(state_dir: str | os.PathLike[str]) -> None:
    """Say nothing is ready yet.

    Called before anything connects, so the file's presence means ready now
    rather than ready at some point in this container's history. The state
    directory outlives the container, so without this a process that starts
    and never connects would inherit the last one's word for it.
    """
    path_for(state_dir).unlink(missing_ok=True)


def mark_ready(
    state_dir: str | os.PathLike[str], *, where: str, agents: Sequence[str]
) -> Path:
    """Record that this process is serving, atomically.

    The deploy polls this file every couple of seconds while it may be being
    rewritten. Written under a temporary name and renamed into place, so a
    reader sees either the old marker or the new one and never half of one.
    """
    path = path_for(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "ready": True,
            "connected_at": datetime.now(timezone.utc).strftime(STAMP),
            "where": where,
            "agents": list(agents),
            "pid": os.getpid(),
        },
        indent=2,
    )
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(body + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        # A write that failed must not leave a temporary file beside the
        # marker for the next person to wonder about.
        tmp.unlink(missing_ok=True)
    return path
