"""The marker is only worth anything if the thing that serves writes it.

readiness.py is unit-tested on its own. What is easy to get wrong, and
invisible when you do, is the wiring: a deploy gate that reads a file nobody
writes fails every deploy, and it fails identically to a container that
never came up.
"""

import argparse
import asyncio
import contextlib
import json

import pytest

from ricardi import cli, policy as policy_mod, readiness

POLICY = """
[limits]
turns_per_minute = 0
turns_per_hour = 0

# A policy with no rules at all is refused at load, so this is the smallest
# one that starts. It grants nothing: no identity is on the roster.
[[rule]]
name    = "nobody, in tests"
effect  = "allow"
actions = ["ask"]
users   = ["nobody@example.com"]
"""


def _args(tmp_path, *, policy_body=POLICY):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)

    agents = tmp_path / "agents.toml"
    agents.write_text(
        "[ricardi]\n"
        "ack_after = 1.0\n"
        f'state_dir = "{state.as_posix()}"\n'
        "\n"
        "[[agent]]\n"
        'name = "solo"\n'
        f'cwd = "{repo.as_posix()}"\n',
        encoding="utf-8",
    )
    policy = tmp_path / "policy.toml"
    policy.write_text(policy_body, encoding="utf-8")

    return state, argparse.Namespace(
        agents=str(agents),
        policy=str(policy),
        audit="",
        transport="local",
        backend="fake",
        port=0,
        host="127.0.0.1",
        env=str(tmp_path / ".env"),
        verbose=False,
    )


async def test_the_marker_appears_once_it_is_serving(tmp_path):
    state, args = _args(tmp_path)
    task = asyncio.create_task(cli._serve(args))
    try:
        marker = readiness.path_for(state)
        for _ in range(250):
            if marker.exists():
                break
            await asyncio.sleep(0.02)
        assert marker.exists(), "serving, with nothing to tell the deploy so"
        body = json.loads(marker.read_text(encoding="utf-8"))
        assert body["ready"] is True
        assert body["agents"] == ["solo"]
        assert body["where"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_stale_marker_does_not_survive_a_startup_that_fails(tmp_path):
    # The state directory outlives the container. A process that starts and
    # never gets as far as serving must not inherit the last one's word for
    # it -- that is the stale-signal bug, moved from the log into a file.
    state, args = _args(tmp_path, policy_body="this is not toml = = =")
    readiness.mark_ready(state, where="Slack", agents=["from-the-last-boot"])

    with pytest.raises(policy_mod.PolicyError):
        await cli._serve(args)

    assert not readiness.path_for(state).exists()
