"""The deploy's test gate, run as a program rather than read as YAML.

The gate refuses to ship a commit unless the `tests` workflow concluded
`success` on that exact SHA. The failure worth testing is the one that
happened: a rebase-merge creates a SHA no pull-request check ever ran
against, so the only run that can satisfy the gate is the `push: [main]` run
the merge itself starts -- and for the first minute that run has no
conclusion at all. Reading a run in flight as "no run" turns a healthy deploy
into a refusal, and the refusal says "the tests workflow is 'none'", which
sends you looking for a missing run that is in fact running.

So the step's script is extracted from the workflow and executed here against
a stubbed `curl`, which can hand back a queued run first and a finished one
second. `sleep` is stubbed too, both to keep this fast and so the test can
assert the gate actually waited rather than got lucky.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"
SHA = "fa31246ea6e330c46984773b860323c9989f0ae4"

def _bash() -> str | None:
    r"""A bash that can see Windows paths.

    On Windows, `bash` on PATH is often WSL's, which maps C:\ to /mnt/c and so
    reports "No such file or directory" for a file that is plainly there. Git's
    bash is the one the rest of this project's shell tooling runs under.
    """
    if sys.platform == "win32":
        for candidate in (r"C:\Program Files\Git\bin\bash.exe",
                          r"C:\Program Files\Git\usr\bin\bash.exe"):
            if Path(candidate).exists():
                return candidate
        return None
    return shutil.which("bash")


BASH = _bash()


def _unix(path: Path) -> str:
    """`C:/x/y` as `/c/x/y`, which is what a path inside Git's bash looks like."""
    p = path.as_posix()
    if sys.platform == "win32" and len(p) > 1 and p[1] == ":":
        return f"/{p[0].lower()}{p[2:]}"
    return p

pytestmark = pytest.mark.skipif(
    BASH is None or shutil.which("jq") is None,
    reason="runs the gate's own shell script, which needs bash and jq",
)


def _gate_script() -> str:
    """The gate step's `run:`, with the one workflow expression filled in."""
    workflow = yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["deploy"]["steps"]
    gate = [s for s in steps if s.get("name", "").startswith("Require that the tests")]
    assert len(gate) == 1, "expected exactly one test-gate step in deploy.yml"
    return gate[0]["run"].replace("${{ steps.target.outputs.sha }}", SHA)


def _run(tmp_path: Path, *responses: object) -> subprocess.CompletedProcess:
    """Run the gate with `curl` handing back `responses` in order.

    The last response repeats once exhausted, which is how "it never finishes"
    is expressed: pass a single in-progress run.
    """
    stub = tmp_path / "stub"
    stub.mkdir()
    replies = tmp_path / "replies"
    replies.mkdir()
    for i, body in enumerate(responses, start=1):
        (replies / f"{i}.json").write_text(json.dumps(body), encoding="utf-8")

    (stub / "curl").write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{replies.as_posix()}/count" 2>/dev/null || echo 0)\n'
        "n=$((n + 1))\n"
        f'echo "$n" > "{replies.as_posix()}/count"\n'
        f'f="{replies.as_posix()}/$n.json"\n'
        f'[ -f "$f" ] || f="{replies.as_posix()}/{len(responses)}.json"\n'
        'cat "$f"\n',
        encoding="utf-8",
    )
    # No real waiting, and a record that waiting happened.
    (stub / "sleep").write_text(
        "#!/bin/sh\n"
        f'echo "$1" >> "{(tmp_path / "slept").as_posix()}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    for f in stub.iterdir():
        f.chmod(0o755)

    script = tmp_path / "gate.sh"
    script.write_text(_gate_script(), encoding="utf-8")

    env = dict(os.environ)
    env["GH_TOKEN"] = "not-a-real-token"
    env["GITHUB_REPOSITORY"] = "gevikn/ricardi"
    env["GITHUB_OUTPUT"] = str(tmp_path / "gh-output")
    # as_posix(): on Windows the bash on PATH is Git's, which wants a path
    # with forward slashes -- handed a backslash path it reports "No such
    # file or directory" for a file that is plainly there.
    # PATH is prepended inside the shell, not in `env`. A Windows-form entry
    # ("C:/...", separated by ";") is silently ignored by Git's bash, which
    # converts the inherited PATH on startup -- the stubs looked installed and
    # the real curl ran. Inside bash, PATH is already Unix-form.
    stubs = _unix(stub)
    return subprocess.run(
        [BASH, "-c", f'PATH="{stubs}:$PATH"; exec bash "{script.as_posix()}"'],
        env=env, capture_output=True, text=True,
    )


def _runs(*entries: tuple[str, str | None]) -> dict:
    """A GitHub `actions/runs` body: (status, conclusion) per `tests` run."""
    return {
        "workflow_runs": [
            {
                "name": "tests",
                "status": status,
                "conclusion": conclusion,
                "created_at": f"2026-10-30T20:4{i}:00Z",
            }
            for i, (status, conclusion) in enumerate(entries)
        ]
    }


def _slept(tmp_path: Path) -> int:
    waited = tmp_path / "slept"
    return len(waited.read_text(encoding="utf-8").split()) if waited.exists() else 0


def test_a_finished_green_run_passes_without_waiting(tmp_path):
    r = _run(tmp_path, _runs(("completed", "success")))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _slept(tmp_path) == 0


def test_a_run_still_in_flight_is_waited_for(tmp_path):
    """The rebase-merge case: queued, then running, then green."""
    r = _run(
        tmp_path,
        _runs(("queued", None)),
        _runs(("in_progress", None)),
        _runs(("completed", "success")),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert _slept(tmp_path) >= 2
    assert "success" in r.stdout


def test_a_failed_run_is_refused(tmp_path):
    r = _run(tmp_path, _runs(("completed", "failure")))
    assert r.returncode != 0
    assert "failure" in r.stdout + r.stderr


def test_no_run_at_all_is_refused(tmp_path):
    r = _run(tmp_path, {"workflow_runs": []})
    assert r.returncode != 0
    assert "none" in r.stdout + r.stderr


def test_a_run_that_never_finishes_is_refused_and_says_so(tmp_path):
    r = _run(tmp_path, _runs(("in_progress", None)))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "in_progress" in out
    # The message must not claim there is no run when there is one running.
    assert "is 'none'" not in out


def test_the_newest_run_decides_not_the_first(tmp_path):
    """A re-run after a red one is what the gate should believe."""
    r = _run(tmp_path, _runs(("completed", "failure"), ("completed", "success")))
    assert r.returncode == 0, r.stdout + r.stderr
