"""The two rules that let this repository go public with a runner attached.

A self-hosted runner executes whatever the triggering workflow file says, as
its own user. That is survivable only while every trigger that can reach it
comes from someone with write access. `pull_request` from a fork does not.

Approval settings are not the control people think they are: the "first-time
contributor" exemption lets anyone through once they have had a single commit
merged, so an attacker lands a typo fix and waits. The control that works is
structural -- no untrusted event may name a self-hosted runner -- and it is
checkable, so it is checked here rather than remembered.

The second rule arrived with the first secret this repository ever held. A
public repository with no secrets cannot leak one, and that property was
worth something; spending it bought credential rotation without a shell on
the box. What replaces it is also structural: a long-lived credential reaches
the runner only when a person with write access presses a button, and it
travels in the environment rather than in the text of a script.
"""

import json
import re
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


# The credentials this project puts on the box, under the names .env gives
# them. Each one is a long-lived bearer token for an account we spend: there
# is no scope to reduce and no short expiry to wait out, so the only control
# is how few places it can appear.
CREDENTIAL_SECRETS = frozenset({
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_BOT_TOKEN",
})

# `${{ secrets.NAME }}`, with the whitespace GitHub tolerates inside it.
_SECRET_REF = re.compile(r"\$\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _credentials_in(fragment) -> set[str]:
    """Which of the box's credentials this fragment of the workflow names.

    The whole shape is searched rather than any one field: a reference is
    just text, and it is equally a reference in `env`, in `with`, in an `if`
    or inside a nested matrix.
    """
    named = set(_SECRET_REF.findall(json.dumps(fragment, default=str)))
    return named & CREDENTIAL_SECRETS


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

    # The second rule. `workflow_dispatch` alone -- not TRUSTED_EVENTS, which
    # is the answer to a different question. A push or a schedule is trusted
    # in the sense that no outsider causes it, and still the wrong way for an
    # account credential to arrive somewhere: nobody decided, so nobody is
    # watching the run that placed it.
    referenced = sorted(_credentials_in(workflow))
    if referenced:
        automatic = sorted(events - {"workflow_dispatch"})
        if automatic:
            found.append(
                f"{name}: references {referenced} but triggers on {automatic}, so "
                f"the credential reaches a runner with nobody pressing anything. "
                f"Restrict the workflow to workflow_dispatch."
            )
        for job_name, job in (workflow.get("jobs") or {}).items():
            for index, step in enumerate((job or {}).get("steps") or [], start=1):
                in_script = sorted(_credentials_in((step or {}).get("run")))
                if in_script:
                    found.append(
                        f"{name}: job '{job_name}' step {index} interpolates "
                        f"{in_script} into its own script text. `${{{{ }}}}` is "
                        f"substituted before the shell exists, so the value lands "
                        f"in the script on disk and in whatever echoes it. Pass it "
                        f"through `env:` and read it as a variable."
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


def _credential_wf(on, steps, env=None, runs_on=("self-hosted", "ricardi-home")):
    job = {"runs-on": list(runs_on), "steps": list(steps)}
    if env is not None:
        job["env"] = env
    return {"on": on, "jobs": {"j": job}}


ENV_STEP = [{"name": "use it", "env": {"KEY": "${{ secrets.ANTHROPIC_API_KEY }}"},
             "run": 'curl -H "x-api-key: $KEY" https://api.anthropic.com/v1/models'}]


@pytest.mark.parametrize(
    "on",
    [
        {"workflow_dispatch": None, "push": {"branches": ["main"]}},
        {"push": {"branches": ["main"]}},
        {"schedule": [{"cron": "0 3 * * *"}]},
        {"workflow_run": {"workflows": ["tests"]}},
    ],
)
def test_a_credential_on_a_trigger_nobody_pressed_is_a_problem(on):
    # Every one of these is a trusted event -- they pass the first rule. The
    # second rule is narrower on purpose: putting the account's credential on
    # the box is not something that should happen because a branch moved.
    found = problems("x.yml", _credential_wf(on, ENV_STEP))
    assert found, f"{on} can place the credential with nobody deciding to"


def test_a_credential_behind_a_button_press_alone_is_fine():
    assert problems("x.yml", _credential_wf({"workflow_dispatch": None}, ENV_STEP)) == []


def test_a_credential_interpolated_into_a_run_body_is_a_problem():
    # `${{ }}` is substituted before the shell sees it, so the value is baked
    # into the script file on disk and into anything that echoes the command.
    # Through `env:` the shell gets a variable, and `set -x` prints its name.
    steps = [{"run": 'curl -H "x-api-key: ${{ secrets.ANTHROPIC_API_KEY }}" https://x'}]
    found = problems("x.yml", _credential_wf({"workflow_dispatch": None}, steps))
    assert found, "the credential was pasted into a script and nothing complained"


def test_a_credential_in_a_workflow_level_env_block_is_fine():
    wf = _credential_wf({"workflow_dispatch": None}, [{"run": 'echo "${#KEY}"'}])
    wf["env"] = {"KEY": "${{ secrets.ANTHROPIC_API_KEY }}"}
    assert problems("x.yml", wf) == []


def test_the_guarded_set_is_every_credential_env_example_documents():
    """What counts as a credential is decided by .env.example, not by this list.

    Without this, the parametrised test below proves nothing: it draws its
    cases from CREDENTIAL_SECRETS, so deleting a name from that set deletes
    the test that would have noticed. A mutation run found exactly that.

    Pinning it to the file that documents what the box holds also catches the
    other direction -- a credential added to .env.example later is unguarded
    until somebody adds it here, and this fails until they do.
    """
    text = (WORKFLOWS.parent.parent / ".env.example").read_text(encoding="utf-8")
    # Assignments, commented or not: the two optional credentials ship
    # commented out. Prose mentioning a name is not an assignment, so the `=`
    # is doing real work here.
    declared = set(re.findall(r"(?m)^#?\s*([A-Z][A-Z0-9_]*)=", text))
    credentials = {
        name for name in declared if name.endswith(("_TOKEN", "_KEY", "_SECRET"))
    }
    assert credentials == set(CREDENTIAL_SECRETS)


@pytest.mark.parametrize("name", sorted(CREDENTIAL_SECRETS))
def test_every_credential_the_box_holds_is_guarded_not_just_the_api_key(name):
    # The Slack tokens have the same rotation problem and will travel the same
    # road. A rule that named one secret would quietly not cover them.
    steps = [{"env": {"K": "${{ secrets." + name + " }}"}, "run": "true"}]
    found = problems("x.yml", _credential_wf({"push": {"branches": ["main"]}}, steps))
    assert found, f"{name} reached the runner on a push and nothing complained"


def test_a_secret_that_is_not_one_of_the_boxs_credentials_is_not_restricted():
    # The rule is about bearer credentials for accounts this project spends,
    # not about secrets in general. A coverage upload token on a push is fine.
    steps = [{"env": {"T": "${{ secrets.CODECOV_TOKEN }}"}, "run": "true"}]
    on = {"push": {"branches": ["main"]}}
    assert problems("x.yml", _credential_wf(on, steps)) == []


def test_every_shipped_workflow_is_clean():
    """The guard itself. This is the assertion that has to keep passing."""
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert files, "no workflows found -- the guard is checking nothing"
    found: list[str] = []
    for path in files:
        found += problems(path.name, yaml.safe_load(path.read_text(encoding="utf-8")))
    assert found == [], "\n".join(found)
