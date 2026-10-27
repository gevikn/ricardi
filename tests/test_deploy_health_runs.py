"""The deploy gate, actually executed.

Nothing in CI runs deploy.yml, so until now the only thing checking this
script was a test that grepped it for strings. That is how the last version
shipped green and wrong. These run it, against a stubbed `docker`, and
assert on what it decides.

`sleep` is stubbed too, so the thirty-poll loop finishes at once.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"

# Posix only, like test_install_posix.py. The script runs on the Linux box
# and on the ubuntu CI job; a Windows developer's Git Bash cannot reach a
# script under AppData from a spawned subprocess, and a half-working harness
# that exits non-zero for its own reasons would pass every "this must fail"
# test without running anything.
pytestmark = pytest.mark.skipif(
    os.name != "posix"
    or not (shutil.which("bash") and shutil.which("jq") and shutil.which("date")),
    reason="needs posix with bash, jq and GNU date -- what the deploy runs on",
)

DOCKER_STUB = """#!/bin/sh
if [ "$1" = "compose" ] && [ "$2" = "ps" ] && [ "$3" = "-q" ]; then
  if [ -n "$FAKE_CID" ]; then echo "$FAKE_CID"; fi
  exit 0
fi
if [ "$1" = "inspect" ]; then echo "$FAKE_STARTED"; exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "ps" ] && [ "$3" = "--format" ]; then
  echo "ricardi $FAKE_STATE"; exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then echo "NAME ricardi"; exit 0; fi
if [ "$1" = "compose" ] && [ "$2" = "logs" ]; then echo "(stub log)"; exit 0; fi
exit 0
"""


def _posix(path) -> str:
    """A path bash will accept, on the runner and on a developer's Windows.

    Git Bash does not understand C:\dir; it wants /c/dir. Without this the
    script is "not found", bash exits non-zero, and every test that asserts a
    non-zero exit passes without having run anything.
    """
    text = Path(path).as_posix()
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def _script() -> str:
    workflow = yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["deploy"]["steps"]:
        if step.get("id") == "health":
            return step["run"]
    raise AssertionError("the deploy has no step with id 'health'")


def run_gate(
    tmp_path,
    *,
    started="2026-10-27T00:50:55.580681197Z",
    marker="{}",
    state="running",
    cid="c0ffee",
):
    """Run the real health step. `marker` of None means no file at all."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text(DOCKER_STUB, encoding="utf-8")
    (binaries / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for stub in binaries.iterdir():
        stub.chmod(0o755)

    deploy_dir = tmp_path / "deploy"
    (deploy_dir / "state").mkdir(parents=True)
    if marker is not None:
        (deploy_dir / "state" / "ready.json").write_text(marker, encoding="utf-8")

    script = tmp_path / "health.sh"
    script.write_text(_script(), encoding="utf-8")

    env = dict(os.environ)
    env.update(
        PATH=str(binaries) + os.pathsep + env["PATH"],
        DEPLOY_DIR=_posix(deploy_dir),
        GITHUB_OUTPUT=_posix(tmp_path / "github_output"),
        FAKE_CID=cid,
        FAKE_STARTED=started,
        FAKE_STATE=state,
    )
    done = subprocess.run(
        ["bash", _posix(script)], env=env, capture_output=True, text=True, timeout=120
    )
    # A harness that cannot start the script would hand every "this must
    # fail" test a non-zero exit for the wrong reason.
    assert "No such file or directory" not in done.stderr, done.stderr
    return done


def marker_at(stamp):
    return '{"ready": true, "connected_at": "%s", "agents": ["ricardi"]}' % stamp


def test_a_marker_from_this_boot_passes(tmp_path):
    done = run_gate(tmp_path, marker=marker_at("2026-10-27T00:50:57Z"))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ready since" in done.stdout


def test_a_marker_from_the_same_second_passes(tmp_path):
    # The process can connect inside the second the container started.
    done = run_gate(tmp_path, marker=marker_at("2026-10-27T00:50:55Z"))
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_marker_from_a_previous_boot_fails(tmp_path):
    # The stale-signal bug, which is what the log grep could never catch.
    done = run_gate(tmp_path, marker=marker_at("2026-10-25T20:50:33Z"))
    assert done.returncode != 0


def test_no_marker_at_all_fails(tmp_path):
    done = run_gate(tmp_path, marker=None)
    assert done.returncode != 0


def test_a_marker_with_no_timestamp_fails(tmp_path):
    # `date -u -d ""` is not an error: it is midnight today. So an empty or
    # missing connected_at reads as a real time, and on a deploy run shortly
    # after midnight UTC it is NEWER than a container started the evening
    # before -- which is the no-op deploy. An empty stamp must count as no
    # stamp, not as 00:00 today.
    done = run_gate(
        tmp_path,
        marker='{"ready": true}',
        started="2026-10-26T23:50:00Z",
    )
    assert done.returncode != 0, done.stdout + done.stderr


def test_an_unparseable_timestamp_fails(tmp_path):
    done = run_gate(tmp_path, marker=marker_at("whenever"), started="2026-10-26T23:50:00Z")
    assert done.returncode != 0, done.stdout + done.stderr


def test_a_container_that_is_not_running_fails(tmp_path):
    done = run_gate(
        tmp_path, marker=marker_at("2026-10-27T00:50:57Z"), state="restarting"
    )
    assert done.returncode != 0


def test_no_container_at_all_fails(tmp_path):
    done = run_gate(tmp_path, marker=marker_at("2026-10-27T00:50:57Z"), cid="")
    assert done.returncode != 0
    assert "No ricardi container" in done.stdout + done.stderr
