"""The grant store is only worth anything if startup opens it.

grants.py and the check in RulesPolicy.decide are tested on their own. What is
easy to get wrong is the wiring: a store nobody opens leaves the check dead, and
a corrupt one that is shrugged off leaves everyone denied with no explanation.
"""

import asyncio
import contextlib

import pytest

from ricardi import cli, readiness
from ricardi.grants import GrantError, GrantStore

# Unqualified: tests/ has no __init__.py, so pytest's prepend import mode has
# already put this directory on sys.path. Going through a `tests.` package
# would need the repo root there too, where `requests/` shadows the package of
# the same name.
from test_cli_readiness import _args


async def _until_ready(state):
    marker = readiness.path_for(state)
    for _ in range(250):
        if marker.exists():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_startup_refuses_on_a_corrupt_grant_store(tmp_path):
    # state/ shrugs off a corrupt threads.json and sessions.json; this one must
    # not. Serving with no grants denies everyone and looks like a policy bug.
    state, args = _args(tmp_path)
    (state / "grants.db").write_bytes(b"this is not a sqlite database" * 100)

    # Bounded: a startup that ignores the file would otherwise serve forever.
    with pytest.raises(GrantError, match="grant store"):
        await asyncio.wait_for(cli._serve(args), timeout=5)

    assert not readiness.path_for(state).exists()


async def test_startup_succeeds_with_no_grant_file_at_all(tmp_path):
    # Absence is not corruption: it starts, and denies every ask until seeded.
    state, args = _args(tmp_path)
    assert not (state / "grants.db").exists()

    task = asyncio.create_task(cli._serve(args))
    try:
        assert await _until_ready(state), "a missing grant store stopped startup"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_main_reports_a_corrupt_grant_store_and_exits_nonzero(tmp_path, capsys):
    state, args = _args(tmp_path)
    (state / "grants.db").write_bytes(b"not a database" * 100)

    code = cli.main(["--agents", args.agents, "--policy", args.policy,
                     "--env", args.env, "--transport", "local", "--backend", "fake",
                     "--port", "0"])

    assert code != 0
    assert "grant store" in capsys.readouterr().err


async def test_the_policy_started_with_is_wired_to_the_store(tmp_path):
    state, args = _args(tmp_path)
    seen = {}
    real = cli.RateLimited

    def spy(inner, limits):
        seen["rules"] = inner
        return real(inner, limits)

    task = None
    try:
        cli.RateLimited = spy
        task = asyncio.create_task(cli._serve(args))
        assert await _until_ready(state)
    finally:
        cli.RateLimited = real
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert isinstance(seen["rules"].grants, GrantStore)
