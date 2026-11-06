"""`ricardi check`: the readout, and what it is allowed to fail on.

§9's complaint was that paranoia is achievable and not verifiable. This is the
command that answers, and the thing it has to get right is the exit code: a
gate that fails on a correct configuration gets removed from the deploy, and
then nothing is verified at all.

So `--strict` counts the wholeness family -- defects -- and leaves the posture
family as a readout, because the shipped example deliberately sits off the
tightest setting. `--strict-posture` is for an operator who has decided their
dial is at the top and wants it held there.
"""

from __future__ import annotations

import pytest

from ricardi.cli import main


POLICY = """
[limits]
turns_per_minute = 0
turns_per_hour = 0

[[identity]]
slack = "U00000000000"
email = "you@example.com"

[[rule]]
name    = "you, asking"
effect  = "allow"
actions = ["ask", "list"]
users   = ["you@example.com"]
"""


def world(tmp_path, *, policy_body=POLICY, budget=2.0, turns=60):
    """A catalogue and a policy that both load, with a state directory."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    agents = tmp_path / "agents.toml"
    agents.write_text(
        f'[ricardi]\nstate_dir = "{state.as_posix()}"\n\n'
        f'[[agent]]\nname = "solo"\ncwd = "{repo.as_posix()}"\n'
        f"max_budget_usd = {budget}\nmax_turns = {turns}\n",
        encoding="utf-8",
    )
    policy = tmp_path / "policy.toml"
    policy.write_text(policy_body, encoding="utf-8")
    return agents, policy, state


def check(agents, policy, *flags: str) -> int:
    return main(["--env", "nope", "--agents", str(agents), "--policy", str(policy),
                 "check", *flags])


def granted(state, person="you@example.com", name="solo", verb="ask"):
    from ricardi.grants import GrantStore

    store = GrantStore.open(state / "grants.db")
    try:
        store.add(person=person, name=name, verb=verb, by="admin@example.com")
    finally:
        store.close()


@pytest.fixture(autouse=True)
def _no_ambient_credential(monkeypatch, tmp_path):
    """A real credential in this developer's environment must not decide a test.

    HOME is pointed at an empty directory for the same reason: the stored-login
    assertion reads one, and finding the author's own would make the suite
    answer differently on the box than it does here.
    """
    for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(empty))
    monkeypatch.setenv("USERPROFILE", str(empty))


def test_a_whole_and_credentialled_world_is_quiet(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    agents, policy, state = world(tmp_path)
    granted(state)
    assert check(agents, policy, "--strict") == 0
    out = capsys.readouterr().out
    assert "0 wholeness finding(s)" in out


def test_both_families_are_always_headed_even_when_empty(tmp_path, monkeypatch, capsys):
    """A family that printed nothing must say it found nothing.

    The repo's standing trap: a glob over nothing is a green run that checked
    nothing, and a silent section reads the same as a section that did not run.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    agents, policy, state = world(tmp_path)
    granted(state)
    assert check(agents, policy) == 0
    out = capsys.readouterr().out
    assert "posture" in out and "wholeness" in out
    assert out.count("nothing to report") == 2


def test_a_wholeness_finding_is_printed(tmp_path, capsys):
    agents, policy, state = world(tmp_path)  # no grants at all
    assert check(agents, policy) == 0
    out = capsys.readouterr().out
    assert "name-without-grants" in out and "solo" in out


def test_a_wholeness_finding_fails_under_strict(tmp_path):
    """The exit code is the whole point: a deploy reads that and nothing else."""
    agents, policy, _ = world(tmp_path)
    assert check(agents, policy, "--strict") == 1


def test_a_wholeness_finding_does_not_fail_without_strict(tmp_path):
    agents, policy, _ = world(tmp_path)
    assert check(agents, policy) == 0


def test_a_posture_note_does_not_fail_under_strict(tmp_path, monkeypatch, capsys):
    """The decision this command turns on.

    The shipped example allows commands by argv shape and points a read rule at
    the directory above every checkout. Both are deliberate, and both are
    posture notes -- so if `--strict` counted them, the gate would fail on a
    correct configuration and be deleted from the deploy within a week.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    loose = POLICY + """
[[rule]]
name    = "anyone may read anything"
effect  = "allow"
actions = ["tool"]
tools   = ["Read"]
"""
    agents, policy, state = world(tmp_path, policy_body=loose)
    granted(state)
    assert check(agents, policy, "--strict") == 0
    out = capsys.readouterr().out
    assert "allow-without-users" in out
    assert "0 wholeness finding(s)" in out


def test_a_posture_note_fails_under_strict_posture(tmp_path, monkeypatch):
    """For an operator who has decided the dial is at the top."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    loose = POLICY + """
[[rule]]
name    = "anyone may read anything"
effect  = "allow"
actions = ["tool"]
tools   = ["Read"]
"""
    agents, policy, state = world(tmp_path, policy_body=loose)
    granted(state)
    assert check(agents, policy, "--strict", "--strict-posture") == 1


def test_strict_posture_alone_still_fails_on_posture(tmp_path, monkeypatch):
    """Not a modifier on --strict: an operator who wants only the dial held."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    loose = POLICY + """
[[rule]]
name    = "anyone may read anything"
effect  = "allow"
actions = ["tool"]
tools   = ["Read"]
"""
    agents, policy, state = world(tmp_path, policy_body=loose)
    granted(state)
    assert check(agents, policy, "--strict-posture") == 1


def test_an_inert_identity_does_not_fail_a_gate(tmp_path, monkeypatch, capsys):
    """The same lesson as posture, inside the wholeness family.

    Somebody added to the roster before anyone grants them anything is in the
    correct state -- it is the normal order of a new teammate's first day. §16
    words this assertion as "reported as inert" for that reason, so it is
    printed and counted as a note, and a deploy still lands.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    two = POLICY + """
[[identity]]
slack = "U11111111111"
email = "them@example.com"
"""
    agents, policy, state = world(tmp_path, policy_body=two)
    granted(state)
    assert check(agents, policy, "--strict") == 0
    out = capsys.readouterr().out
    assert "inert-identity" in out and "them@example.com" in out
    assert "0 wholeness finding(s) and 1 wholeness note(s)" in out


def test_a_missing_ceiling_is_a_posture_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    agents, policy, state = world(tmp_path, budget=0, turns=0)
    granted(state)
    assert check(agents, policy, "--strict") == 0
    assert "missing-ceiling" in capsys.readouterr().out


# ------------------------------------------- what could not be checked


def test_a_catalogue_that_cannot_be_read_is_reported(tmp_path, capsys):
    """And nothing claims to have been checked, which is the failure mode:
    a report that says zero findings because it never got to the file."""
    _, policy, _ = world(tmp_path)
    assert check(tmp_path / "nope.toml", policy) == 0
    out = capsys.readouterr().out
    assert "not checked" in out
    assert "No agent catalogue" in out
    assert "finding(s)" not in out


def test_a_catalogue_that_cannot_be_read_fails_under_strict(tmp_path):
    """An assertion that did not run must not read as one that passed."""
    _, policy, _ = world(tmp_path)
    assert check(tmp_path / "nope.toml", policy, "--strict") == 1


def test_a_policy_that_cannot_be_read_is_reported(tmp_path, capsys):
    agents, _, _ = world(tmp_path)
    broken = tmp_path / "broken.toml"
    broken.write_text("[[rule]]\nname = ", encoding="utf-8")
    assert check(agents, broken, "--strict") == 1
    assert "not checked" in capsys.readouterr().out


def test_the_shipped_example_catalogue_is_reported_not_crashed(tmp_path, capsys):
    """The obvious first thing to try, from a fresh clone.

    `agents.example.toml` points at `~/repos/shared`, which does not exist on a
    machine that has not set it up -- so the honest answer is the loader's
    refusal, not a traceback.
    """
    assert check("agents.example.toml", "policy.example.toml") == 0
    assert "not checked" in capsys.readouterr().out


def test_a_grant_store_that_cannot_be_opened_blocks_only_its_assertions(
    tmp_path, capsys
):
    """The posture family does not read the store, so it still answers.

    No credential is set here on purpose: the credential assertion is in the
    wholeness family and needs no store, so its finding is the evidence that
    the family ran rather than being skipped wholesale.
    """
    agents, policy, state = world(tmp_path)
    (state / "grants.db").mkdir()  # a directory where the database should be
    assert check(agents, policy, "--strict") == 1
    out = capsys.readouterr().out
    assert "not checked" in out
    assert "grant store" in out
    # The assertions that do not need the store still ran and are reported.
    assert "credential" in out
    # The ones that do must not be reported as clean.
    assert "name-without-grants" not in out


def test_a_skipped_assertion_alone_fails_a_gate(tmp_path, monkeypatch):
    """Nothing else is wrong here, which is the point.

    A credential is set and the policy and catalogue load, so the only reason
    to fail is that four assertions could not run. A gate asks "is this
    configuration verified", and "we could not look" is not a yes. Written
    after a mutation that deleted this exit survived every other test: the
    store-unreadable case above also has a credential finding, which was
    deciding the exit code and masking this.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    agents, policy, state = world(tmp_path)
    granted(state)
    (state / "grants.db").unlink()
    (state / "grants.db").mkdir()
    assert check(agents, policy, "--strict") == 1


# ------------------------------------------- the dial, read off the example


def test_the_shipped_policy_reports_the_two_positions_section_nine_names(
    tmp_path, monkeypatch, capsys
):
    """Pinned because these are the example's deliberate choices, not bugs.

    If a later edit to `policy.example.toml` quietly tightens or loosens either
    one, this is where it shows up -- and if the readout stops naming them, the
    report has stopped describing the file.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    repo = tmp_path / "repos" / "shared"
    repo.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    agents = tmp_path / "agents.toml"
    agents.write_text(
        f'[ricardi]\nstate_dir = "{state.as_posix()}"\n\n'
        f'[[agent]]\nname = "shared"\ncwd = "{repo.as_posix()}"\n'
        "max_budget_usd = 2.0\nmax_turns = 60\n",
        encoding="utf-8",
    )
    assert check(agents, "policy.example.toml") == 0
    out = capsys.readouterr().out
    # An allow that decides by argv shape (H4), and a read rule bounded above
    # the name's own tree.
    assert "allow-by-argv-shape" in out
    assert "allow-outside-roots" in out


def test_flags_are_accepted_after_the_subcommand_too(tmp_path, monkeypatch, capsys):
    """Where people type them, as with `grants list --state-dir X`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    agents, policy, state = world(tmp_path)
    granted(state)
    assert main(["--env", "nope", "check", "--agents", str(agents),
                 "--policy", str(policy), "--strict"]) == 0
    assert "0 wholeness finding(s)" in capsys.readouterr().out


def test_the_state_directory_can_be_overridden(tmp_path, monkeypatch, capsys):
    """The same escape hatch `grants` has: a store somewhere other than the
    one the catalogue derives."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    agents, policy, _ = world(tmp_path)
    elsewhere = tmp_path / "elsewhere-state"
    elsewhere.mkdir()
    granted(elsewhere)
    assert main(["--env", "nope", "--agents", str(agents), "--policy", str(policy),
                 "check", "--state-dir", str(elsewhere), "--strict"]) == 0
    assert "0 wholeness finding(s)" in capsys.readouterr().out


def test_no_credential_is_a_wholeness_finding(tmp_path, capsys):
    """It is not a dial position: without one, every turn fails."""
    agents, policy, state = world(tmp_path)
    granted(state)
    assert check(agents, policy, "--strict") == 1
    out = capsys.readouterr().out
    assert "credential" in out


def test_a_credential_value_is_never_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-do-not-print-me")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oat-do-not-print-me")
    agents, policy, state = world(tmp_path)
    granted(state)
    check(agents, policy)
    out = capsys.readouterr().out
    assert "both" in out.lower()
    assert "do-not-print-me" not in out
