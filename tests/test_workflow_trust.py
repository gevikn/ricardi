"""The one rule that lets this repository go public with a runner attached.

A self-hosted runner executes whatever the triggering workflow file says, as
its own user. That is survivable only while every trigger that can reach it
comes from someone with write access. `pull_request` from a fork does not.

Approval settings are not the control people think they are: the "first-time
contributor" exemption lets anyone through once they have had a single commit
merged, so an attacker lands a typo fix and waits. The control that works is
structural -- no untrusted event may name a self-hosted runner -- and it is
checkable, so it is checked here rather than remembered.
"""

from pathlib import Path

import pytest
import yaml  # a hard import: importorskip would make this guard vanish quietly

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


# Events only someone with write access to this repository can cause. A fork
# can open a pull request; it cannot push to our branches or dispatch a
# workflow here. Allowlisted rather than blocklisted, so an event nobody
# thought about is untrusted by default.
TRUSTED_EVENTS = frozenset({
    "workflow_dispatch",
    "push",
    "release",
    "schedule",
    "workflow_run",
    "merge_group",
})


def _events(workflow: dict) -> set[str]:
    """The triggers, however they were spelled.

    `on` is read by YAML 1.1 as the boolean True, so a workflow parsed with
    safe_load has a True key and no "on" key. Miss that and this guard
    silently finds no triggers and passes everything.
    """
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, (list, tuple)):
        return {str(e) for e in on}
    if isinstance(on, dict):
        return {str(e) for e in on}
    return set()


def _reaches_our_hardware(job: dict) -> bool:
    """Does this job land on a self-hosted runner, by any spelling?

    `runs-on` may be a string, a list of labels, or an expression pointing at
    a matrix that holds the label -- so the whole shape is searched rather
    than any one field. Every self-hosted runner carries "self-hosted"
    implicitly, so one word covers them all.
    """
    import json

    blob = json.dumps(
        {"runs-on": job.get("runs-on"), "strategy": job.get("strategy")}, default=str
    )
    return "self-hosted" in blob.lower()


def problems(name: str, workflow: dict) -> list[str]:
    """Every way this workflow could put untrusted code on our own hardware."""
    found: list[str] = []
    events = _events(workflow)

    if "pull_request_target" in events:
        found.append(
            f"{name}: triggers on pull_request_target, which runs with this "
            f"repository's secrets. Checking out a fork's head under it is the "
            f"'pwn request' pattern. Use pull_request."
        )

    untrusted = sorted(events - TRUSTED_EVENTS)
    if untrusted:
        for job_name, job in (workflow.get("jobs") or {}).items():
            if _reaches_our_hardware(job or {}):
                found.append(
                    f"{name}: job '{job_name}' runs on a self-hosted runner, but "
                    f"the workflow triggers on {untrusted}, which an outsider can "
                    f"cause. Move the job to a GitHub-hosted runner, or restrict "
                    f"the workflow to {sorted(TRUSTED_EVENTS)}."
                )
    return found


def _wf(on, runs_on="ubuntu-latest"):
    return {"on": on, "jobs": {"j": {"runs-on": runs_on, "steps": []}}}


@pytest.mark.parametrize(
    "on",
    [
        "pull_request",
        ["pull_request"],
        {"pull_request": None},
        {"workflow_dispatch": None, "pull_request": {"branches": ["main"]}},
        {"issue_comment": {"types": ["created"]}},
    ],
)
def test_an_untrusted_trigger_on_a_self_hosted_job_is_a_problem(on):
    found = problems("x.yml", _wf(on, ["self-hosted", "ricardi-home"]))
    assert found, "a fork-triggerable event reached the box and nothing complained"


@pytest.mark.parametrize(
    "on",
    [
        "workflow_dispatch",
        {"workflow_dispatch": None},
        {"push": {"branches": ["main"]}},
        {"workflow_dispatch": None, "push": {"branches": ["main"]}},
        {"schedule": [{"cron": "0 3 * * *"}]},
    ],
)
def test_a_trusted_trigger_on_a_self_hosted_job_is_fine(on):
    assert problems("x.yml", _wf(on, ["self-hosted", "ricardi-home"])) == []


def test_an_untrusted_trigger_on_a_hosted_runner_is_fine():
    # This is the whole point of the split: fork PRs are welcome, on
    # GitHub's hardware, which is free for public repositories.
    assert problems("x.yml", _wf({"pull_request": None}, "ubuntu-latest")) == []


def test_a_matrix_expression_still_counts_as_self_hosted():
    wf = {
        "on": {"pull_request": None},
        "jobs": {
            "j": {
                "strategy": {"matrix": {"os": ["ubuntu-latest", "self-hosted"]}},
                "runs-on": "${{ matrix.os }}",
                "steps": [],
            }
        },
    }
    assert problems("x.yml", wf), "a self-hosted label hidden in a matrix slipped through"


def test_pull_request_target_is_flagged_wherever_it_appears():
    # Runs with the base repo's secrets. Checking out the fork's head under it
    # is the "pwn request" pattern, and it is never what we want here.
    assert problems("x.yml", _wf({"pull_request_target": None}, "ubuntu-latest"))


def test_every_shipped_workflow_is_clean():
    """The guard itself. This is the assertion that has to keep passing."""
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert files, "no workflows found -- the guard is checking nothing"
    found: list[str] = []
    for path in files:
        found += problems(path.name, yaml.safe_load(path.read_text(encoding="utf-8")))
    assert found == [], "\n".join(found)


# -- the health check must look only at this container's own output -------

def _deploy_steps() -> list[dict]:
    wf = yaml.safe_load((WORKFLOWS / "deploy.yml").read_text(encoding="utf-8"))
    return [s for job in wf["jobs"].values() for s in job["steps"]]


def _health_script() -> str:
    for step in _deploy_steps():
        if step.get("id") == "health":
            return step["run"]
    pytest.fail("deploy.yml has no step with id 'health'")


def test_the_health_check_does_not_accept_output_from_before_this_container():
    """A stale success line must not be able to satisfy the check.

    `docker compose logs --tail N` is unbounded in time. A container that is
    restarted rather than recreated keeps its log file, so a "connected to
    Slack" from an earlier deploy stays within the last N lines and a restart
    that failed to connect reads as healthy -- and the rollback never fires.

    Twice already a deploy has gone green on a line it did not produce.
    """
    for line in _health_script().splitlines():
        if "connected to Slack" in line and "docker compose logs" in line:
            assert "--since" in line, (
                "the grepped log window is unbounded in time: " + line.strip()
            )
            break
    else:
        pytest.fail("no line greps the logs for the readiness marker")


def test_the_log_window_is_anchored_to_the_container_not_the_deploy():
    """Anchoring on the deploy's own start time would break a no-op deploy.

    When nothing changed, `compose up` leaves the running container alone and
    produces no new output, so a window starting at the deploy would be empty
    and a correct deploy would fail. The container's own StartedAt is right in
    both cases: fresh for a restart, original for a container left untouched.
    """
    assert "StartedAt" in _health_script(), (
        "the --since anchor must come from the container's StartedAt"
    )
