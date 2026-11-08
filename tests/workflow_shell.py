"""Running a workflow's own shell, for the tests that execute what we ship.

Two workflows have steps worth executing rather than grepping: the deploy's
health gate and the credential rotation. Both decide something consequential
in shell embedded in YAML, where a typo is invisible until the day it runs.

Each of the three things they need is a trap somebody has already fallen
into, which is why they live here once rather than in both files:

* **Whether this environment can run them at all.** `which bash` says a shell
  exists; it does not say that a shell this process starts can reach a file
  this process just wrote. Windows answers the first yes and the second no,
  and a sandbox can do the same on Linux.
* **A path that shell will accept.** Git Bash wants `/c/dir`, not `C:\\dir`.
* **Noticing that the script never started.** `bash: no such file` exits
  non-zero, which hands every "this must refuse" test a pass it did not earn.
  That is not a hypothetical: it is how the first draft of each of these
  harnesses behaved.
"""

import os
import shutil
import subprocess
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def unusable_shell(*tools: str) -> str:
    """Why a workflow's shell cannot be run here, or "" when it can.

    Posix only. A Windows developer's Git Bash cannot reach a script under
    AppData from a spawned subprocess, and a half-working harness that exits
    non-zero for its own reasons is worse than no harness: every test that
    asserts a refusal passes without running anything.
    """
    if os.name != "posix":
        return "needs posix -- a spawned Git Bash cannot reach the test's own files"
    absent = [name for name in ("bash", *tools) if shutil.which(name) is None]
    if absent:
        return f"needs {', '.join(absent)} -- what the workflow itself runs on"
    return ""


def posix_path(path) -> str:
    """A path bash will accept, on the runner and on a developer's Windows.

    Git Bash does not understand C:\\dir; it wants /c/dir. Without this the
    script is "not found", bash exits non-zero, and every test that asserts a
    non-zero exit passes without having run anything.
    """
    text = Path(path).as_posix()
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def step_script(workflow: str, job: str, *, step_id: str = "", name: str = "") -> str:
    """One step's shipped `run:` body, by its id or its name.

    Looked up rather than indexed: a step inserted above the one under test
    must not silently move the test onto a different script. Nothing is
    retyped here -- a copy of the script would pass while the shipped one
    rotted, which is the whole reason these tests exist.
    """
    parsed = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    steps = parsed["jobs"][job]["steps"]
    for step in steps:
        if (step_id and step.get("id") == step_id) or (
            name and step.get("name") == name
        ):
            return step["run"]
    raise AssertionError(
        f"{workflow} job '{job}' has no step with "
        f"{'id ' + step_id if step_id else 'name ' + name!r}; "
        f"found ids {[s.get('id') for s in steps]} "
        f"and names {[s.get('name') for s in steps]}"
    )


def run_step(script: str, *, at: Path, env: dict[str, str], timeout: int = 120):
    """Run a step's script from `at`, with `env` added to this process's own."""
    path = at / "step.sh"
    # Newline-explicit: a CRLF script is a syntax error to bash, and the
    # working tree on a Windows developer's machine is CRLF.
    path.write_text(script, newline="\n", encoding="utf-8")
    whole = dict(os.environ)
    whole.update(env)
    done = subprocess.run(
        ["bash", posix_path(path)],
        env=whole,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    # Anchored on the script's own name: a step may legitimately report some
    # other file missing, and swallowing that would hide a real finding.
    assert f"{path.name}: No such file" not in done.stderr, done.stderr
    return done
