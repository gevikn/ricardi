"""The deploy gate, actually executed.

Nothing in CI runs deploy.yml, so until now the only thing checking this
script was a test that grepped it for strings. That is how the last version
shipped green and wrong. These run it, against a stubbed `docker`, and
assert on what it decides.

`sleep` is stubbed too, so the thirty-poll loop finishes at once.
"""

import os

import pytest
from workflow_shell import posix_path, run_step, step_script, unusable_shell

# Posix only, like test_install_posix.py -- `workflow_shell.unusable_shell`
# explains why, and `rotate-credential`'s tests rest on the same answer.
pytestmark = pytest.mark.skipif(
    bool(unusable_shell("jq", "date")),
    reason=unusable_shell("jq", "date") or "shell is usable",
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

    return run_step(
        step_script("deploy.yml", "deploy", step_id="health"),
        at=tmp_path,
        env={
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "DEPLOY_DIR": posix_path(deploy_dir),
            "GITHUB_OUTPUT": posix_path(tmp_path / "github_output"),
            "FAKE_CID": cid,
            "FAKE_STARTED": started,
            "FAKE_STATE": state,
        },
    )


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
