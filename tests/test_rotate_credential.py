"""The rotation workflow's own shell, run against a fake box.

Rotating a credential rewrites `/opt/ricardi/.env` -- a file no deploy will
replace and nobody can get at without a session on the box, which is the
whole reason this workflow exists. A rewrite that drops the Slack tokens, or
mangles a key with a `&` in it, is therefore not a failed workflow but an
outage that needs hands on the machine it was built to avoid touching.

So these tests execute the steps exactly as shipped: the YAML is parsed, the
`run:` script is handed to bash with a temporary DEPLOY_DIR, and the file it
leaves behind is read back. Nothing is retyped here, which is the point -- a
copy of the script would pass while the shipped one rotted.

Docker is stubbed, the way `test_deploy_health_runs.py` stubs it, so the step
that reads the key back out of the running container is tested here rather
than first on the box.
"""

import hashlib
import os
from pathlib import Path

import pytest
from workflow_shell import posix_path, run_step, step_script, unusable_shell

REFUSE = "Refuse a configuration this key cannot win"
WRITE = "Write it, keeping a way back"
HEALTH = "Is it up, and is it holding the new key"

# awk because the rewrite is an awk program -- the one place a wrong answer
# corrupts a credential rather than failing a workflow -- and jq, date and
# sha256sum because the health step is the deploy's readiness check plus a
# digest comparison.
pytestmark = pytest.mark.skipif(
    bool(unusable_shell("awk", "jq", "date", "sha256sum")),
    reason=unusable_shell("awk", "jq", "date", "sha256sum") or "shell is usable",
)


def _run(step_name: str, box: Path, **env: str):
    """Run one shipped step with the inputs the runner would give it."""
    inputs = {
        "DEPLOY_DIR": posix_path(box),
        "GITHUB_OUTPUT": posix_path(box / "gh-output"),
    }
    inputs.update(env)
    return run_step(
        step_script("rotate-credential.yml", "rotate", name=step_name),
        at=box,
        env=inputs,
    )


# A .env in the shape install-service.sh leaves behind: the Slack tokens that
# must survive, a commented example line that must not be counted as the key,
# and the key itself in the middle rather than at either end.
ENV_WITH_KEY = """\
SLACK_APP_TOKEN=xapp-old
SLACK_BOT_TOKEN=xoxb-old
# ANTHROPIC_API_KEY=
ANTHROPIC_API_KEY=sk-ant-old
# RICARDI_STATE_DIR=./state
"""

ENV_WITHOUT_KEY = """\
SLACK_APP_TOKEN=xapp-old
SLACK_BOT_TOKEN=xoxb-old
"""


@pytest.fixture
def box(tmp_path: Path) -> Path:
    (tmp_path / "claude-config").mkdir()
    (tmp_path / ".env").write_text(ENV_WITH_KEY, newline="\n", encoding="utf-8")
    return tmp_path


def test_a_healthy_env_is_accepted(box: Path):
    done = _run(REFUSE, box)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ANTHROPIC_API_KEY lines in .env: 1" in done.stdout


def test_a_missing_env_is_refused_rather_than_created(box: Path):
    (box / ".env").unlink()
    done = _run(REFUSE, box)
    assert done.returncode == 1
    assert "never installed" in done.stdout


def test_an_oauth_token_in_the_same_file_is_refused(box: Path):
    # install-service.sh refuses both at once, and so must this: the one that
    # wins is not the one the person rotating thinks they changed.
    with (box / ".env").open("a", newline="\n") as handle:
        handle.write("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-x\n")
    done = _run(REFUSE, box)
    assert done.returncode == 1
    assert "conflict" in done.stdout


def test_a_commented_or_empty_oauth_line_is_not_a_conflict(box: Path):
    with (box / ".env").open("a", newline="\n") as handle:
        handle.write("# CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-x\nCLAUDE_CODE_OAUTH_TOKEN=\n")
    done = _run(REFUSE, box)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_stored_login_is_refused_because_it_would_swallow_the_rotation(box: Path):
    (box / "claude-config" / ".credentials.json").write_text("{}", encoding="utf-8")
    done = _run(REFUSE, box)
    assert done.returncode == 1
    assert "takes precedence" in done.stdout


def test_the_stored_login_is_looked_for_where_env_says_it_is_mounted(box: Path):
    # compose mounts ${RICARDI_CLAUDE_DIR:-./claude-config} at /config/claude.
    # Checking only the default would miss a shadowing login on every box that
    # overrode it -- and report success.
    elsewhere = box / "somewhere-else"
    elsewhere.mkdir()
    (elsewhere / ".credentials.json").write_text("{}", encoding="utf-8")
    with (box / ".env").open("a", newline="\n") as handle:
        handle.write(f"RICARDI_CLAUDE_DIR={elsewhere}\n")
    done = _run(REFUSE, box)
    assert done.returncode == 1
    assert "takes precedence" in done.stdout


def test_two_key_lines_are_refused_rather_than_guessed(box: Path):
    with (box / ".env").open("a", newline="\n") as handle:
        handle.write("ANTHROPIC_API_KEY=sk-ant-other\n")
    done = _run(REFUSE, box)
    assert done.returncode == 1
    assert "will not guess" in done.stdout


def test_no_key_line_is_refused_unless_adding_one_was_asked_for(box: Path):
    (box / ".env").write_text(ENV_WITHOUT_KEY, newline="\n", encoding="utf-8")
    refused = _run(REFUSE, box)
    assert refused.returncode == 1
    assert "allow_add=true" in refused.stdout

    allowed = _run(REFUSE, box, ALLOW_ADD="true")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr


def test_the_key_is_replaced_and_everything_else_survives(box: Path):
    done = _run(WRITE, box, NEW_KEY="sk-ant-new")
    assert done.returncode == 0, done.stdout + done.stderr

    after = (box / ".env").read_text(encoding="utf-8").splitlines()
    assert after == [
        "SLACK_APP_TOKEN=xapp-old",
        "SLACK_BOT_TOKEN=xoxb-old",
        "# ANTHROPIC_API_KEY=",
        "ANTHROPIC_API_KEY=sk-ant-new",
        "# RICARDI_STATE_DIR=./state",
    ]


def test_the_previous_file_is_kept_as_the_way_back(box: Path):
    _run(WRITE, box, NEW_KEY="sk-ant-new")
    assert (box / ".env.bak").read_text(encoding="utf-8") == ENV_WITH_KEY
    assert not (box / ".env.next").exists(), "the temporary file outlived the rename"


def test_the_write_step_reports_that_it_wrote(box: Path):
    # The rollback step is gated on this output rather than on the existence
    # of .env.bak, so that a run which failed before writing cannot restore a
    # previous rotation's file.
    _run(WRITE, box, NEW_KEY="sk-ant-new")
    assert "rewrote=yes" in (box / "gh-output").read_text(encoding="utf-8")


def test_a_key_is_added_when_the_file_had_none(box: Path):
    (box / ".env").write_text(ENV_WITHOUT_KEY, newline="\n", encoding="utf-8")
    done = _run(WRITE, box, NEW_KEY="sk-ant-new")
    assert done.returncode == 0, done.stdout + done.stderr
    after = (box / ".env").read_text(encoding="utf-8").splitlines()
    assert after[-1] == "ANTHROPIC_API_KEY=sk-ant-new"
    assert after[:2] == ["SLACK_APP_TOKEN=xapp-old", "SLACK_BOT_TOKEN=xoxb-old"]


@pytest.mark.parametrize(
    "key",
    [
        "sk-ant-with&ampersand",
        "sk-ant-with$dollar",
        "sk-ant-with\\backslash",
        'sk-ant-with"quote',
        "sk-ant-with spaces",
        "sk-ant-with#hash",
    ],
)
def test_a_key_with_shell_or_awk_punctuation_in_it_lands_verbatim(box: Path, key: str):
    # `&` is the substitution character in awk's sub(), `$` and `\` are the
    # shell's, and a key written with any of them mangled is a box that cannot
    # answer and cannot be reached. The value travels through ENVIRON for
    # exactly this reason; this is the assertion that it stays intact.
    done = _run(WRITE, box, NEW_KEY=key)
    assert done.returncode == 0, done.stdout + done.stderr
    after = (box / ".env").read_text(encoding="utf-8").splitlines()
    assert f"ANTHROPIC_API_KEY={key}" in after


def test_the_key_is_never_printed_by_the_step_that_writes_it(box: Path):
    # A workflow log is readable by anyone who can read the repository, and
    # this one is public.
    done = _run(WRITE, box, NEW_KEY="sk-ant-do-not-print-me")
    assert "do-not-print-me" not in done.stdout + done.stderr


# --------------------------------------------------------------------------
# The health step: readiness, and then the question readiness cannot answer.

# Its own stub rather than the deploy's: this is the only step in either
# workflow that calls `docker compose exec`, and what that returns is the
# thing under test.
DOCKER_STUB = """#!/bin/sh
if [ "$1" = "compose" ] && [ "$2" = "ps" ] && [ "$3" = "-q" ]; then
  if [ -n "$FAKE_CID" ]; then echo "$FAKE_CID"; fi
  exit 0
fi
if [ "$1" = "inspect" ]; then echo "$FAKE_STARTED"; exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "ps" ] && [ "$3" = "--format" ]; then
  echo "ricardi $FAKE_STATE"; exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "exec" ]; then
  printf %s "$FAKE_CONTAINER_KEY" | sha256sum; exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "logs" ]; then echo "(stub log)"; exit 0; fi
exit 0
"""

STARTED = "2026-11-08T19:50:55.580681197Z"


def _marker(stamp: str) -> str:
    return '{"ready": true, "connected_at": "%s", "agents": ["ricardi"]}' % stamp


def _run_health(
    box: Path,
    *,
    key: str = "sk-ant-new",
    in_container: str | None = None,
    marker: str | None = _marker("2026-11-08T19:50:57Z"),
    state: str = "running",
    cid: str = "c0ffee",
):
    """Run the shipped health step. `in_container` defaults to `key`."""
    binaries = box / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text(DOCKER_STUB, encoding="utf-8")
    # Stubbed so the thirty-poll loop finishes at once.
    (binaries / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for stub in binaries.iterdir():
        stub.chmod(0o755)

    (box / "state").mkdir()
    if marker is not None:
        (box / "state" / "ready.json").write_text(marker, encoding="utf-8")

    return _run(
        HEALTH,
        box,
        PATH=str(binaries) + os.pathsep + os.environ["PATH"],
        NEW_KEY=key,
        FAKE_CID=cid,
        FAKE_STARTED=STARTED,
        FAKE_STATE=state,
        FAKE_CONTAINER_KEY=key if in_container is None else in_container,
    )


def test_a_container_holding_the_key_we_wrote_passes(box: Path):
    done = _run_health(box)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "holds the new key" in done.stdout
    assert "ok=yes" in (box / "gh-output").read_text(encoding="utf-8")


def test_a_container_holding_a_different_key_fails(box: Path):
    # What a container that was never really recreated looks like -- and the
    # reason --force-recreate is not trusted to have worked.
    done = _run_health(box, key="sk-ant-new", in_container="sk-ant-old")
    assert done.returncode != 0
    assert "not holding the key we just wrote" in done.stdout


def test_a_container_holding_nothing_fails(box: Path):
    # An env_file that did not reach the process at all.
    done = _run_health(box, in_container="")
    assert done.returncode != 0


def test_a_marker_from_a_previous_boot_fails_before_the_key_is_compared(box: Path):
    # The same staleness the deploy gate refuses: a ready.json left by the
    # container this one replaced says nothing about this one.
    done = _run_health(box, marker=_marker("2026-11-01T10:00:00Z"))
    assert done.returncode != 0
    assert "readiness marker" in done.stdout


def test_no_marker_at_all_fails(box: Path):
    assert _run_health(box, marker=None).returncode != 0


def test_a_container_that_is_not_running_fails(box: Path):
    assert _run_health(box, state="restarting").returncode != 0


def test_no_container_at_all_fails(box: Path):
    done = _run_health(box, cid="")
    assert done.returncode != 0
    assert "No ricardi container" in done.stdout


def test_neither_the_key_nor_its_digest_reaches_the_log(box: Path):
    # The comparison is the whole point of the step, and a digest printed to
    # a public repository's log lets someone confirm a guessed key.
    key = "sk-ant-do-not-print-me"
    done = _run_health(box, key=key)
    output = done.stdout + done.stderr
    assert "do-not-print-me" not in output
    assert hashlib.sha256(key.encode()).hexdigest() not in output
