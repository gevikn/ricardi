"""What the deploy is allowed to believe when it says "it is up".

The gate used to grep the container log for `connected to Slack` with an
unbounded `--tail`. Two things were wrong with that, and only the first is
obvious:

1. `--tail` is unbounded in time, so a container that was restarted rather
   than recreated keeps its old output and the gate passes on a line from a
   previous deploy. It passes for the wrong reason, and a container that
   never came up looks identical to one that did.
2. On the box the log loses its newest lines within minutes, so anchoring
   the window on the current boot -- the obvious fix -- fails a container
   that is perfectly healthy, and a failed gate fires the rollback.

So the gate reads a file the process writes instead. These tests are the
only thing standing between that decision and someone reaching for `grep`
again, because nothing in CI runs this workflow.
"""

from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"


@pytest.fixture
def workflow():
    return yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))


@pytest.fixture
def health(workflow):
    for step in workflow["jobs"]["deploy"]["steps"]:
        if step.get("id") == "health":
            return step["run"]
    raise AssertionError("the deploy has no step with id 'health'")


def test_the_gate_reads_the_readiness_marker(health):
    assert "state/ready.json" in health


def test_the_gate_is_anchored_on_the_container_not_the_deploy(health):
    # Anchoring on when the deploy ran would close the stale-line hole and
    # open the opposite one: compose correctly leaves an unchanged container
    # alone, so a no-op deploy produces nothing new and would fail.
    assert "StartedAt" in health


def test_the_gate_does_not_decide_on_log_output():
    # The whole file: a grep for this string anywhere means the decision
    # crept back into the log, wherever it was written.
    assert "connected to Slack" not in DEPLOY.read_text(encoding="utf-8")


def test_a_failing_deploy_still_shows_a_human_the_log(health):
    # Unreliable as a decision, still the first thing a person wants to read.
    assert "logs" in health


def test_a_failed_gate_still_puts_it_back(workflow):
    steps = workflow["jobs"]["deploy"]["steps"]
    rollback = [s for s in steps if "rollback" in (s.get("run") or "")]
    assert rollback, "nothing rolls back, so a bad deploy just stays"
    assert any("steps.health.outcome" in (s.get("if") or "") for s in rollback)
