"""The two families `ricardi check` asserts, one predicate at a time.

Each predicate is a pure function over loaded config, so these tests read the
same answer the command prints and the startup warnings log. That sharing is
the point: two copies of a safety assertion is one copy that goes stale.

Posture findings are not defects -- they are dial positions, and the shipped
example deliberately sits off the tightest setting. Wholeness findings are
defects: something configured does not hang together. The tests say which is
which, because the exit code depends on it.
"""

from __future__ import annotations

from pathlib import Path

from ricardi import checks
from ricardi.config import AgentSpec, Settings
from ricardi.grants import Grant
from ricardi.policy import Rule, RulesPolicy


ROSTER = {"U1": "you@example.com", "U2": "them@example.com"}


def agent(name="shared", cwd="/repos/shared", roots=None, budget=5.0, turns=40):
    return AgentSpec(
        name=name,
        cwd=cwd,
        roots=tuple(roots) if roots is not None else (cwd,),
        max_budget_usd=budget,
        max_turns=turns,
    )


def settings(*agents):
    return Settings(agents=agents or (agent(),))


def grant(person="you@example.com", name="shared", verb="ask"):
    return Grant(person=person, name=name, verb=verb, granted=True,
                 by="admin@example.com", at="2026-11-06T00:00:00+00:00")


# ================================================= family A: posture


def test_an_allow_naming_no_users_is_reported():
    """The dimension whose absence grants the most: everyone, in any channel."""
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="reads", actions=("tool",),
                    tools=("Read",), path_under="/repos/shared")],
        roster=ROSTER,
    )
    assert checks.allows_without_users(policy) == ["reads"]


def test_an_allow_naming_users_is_not_reported():
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="reads", actions=("tool",),
                    tools=("Read",), users=("you@example.com",))],
        roster=ROSTER,
    )
    assert checks.allows_without_users(policy) == []


def test_a_deny_naming_no_users_is_not_reported():
    """A deny that names nobody covers everybody, which is the safe direction."""
    policy = RulesPolicy(
        rules=[Rule(effect="deny", name="never", actions=("tool",))], roster=ROSTER
    )
    assert checks.allows_without_users(policy) == []


def test_an_allow_bounded_above_the_names_own_roots_is_reported():
    """`~/repos` covers every checkout, which is what the example ships.

    Reported as the dial position it is: the rule reaches outside the tree of
    the name that asks, so a question to one agent can read another's files.
    """
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="reads", actions=("tool",),
                    tools=("Read",), users=("you@example.com",),
                    path_under="/repos")],
        roster=ROSTER,
    )
    (found,) = checks.allows_outside_their_roots(policy, settings())
    assert found[0] == "reads"
    assert "/repos" in found[1]


def test_an_allow_bounded_inside_the_names_roots_is_not_reported():
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="writes", actions=("tool",),
                    tools=("Write",), users=("you@example.com",),
                    agents=("shared",), path_under="/repos/shared")],
        roster=ROSTER,
    )
    assert checks.allows_outside_their_roots(policy, settings()) == []


def test_an_allow_with_no_path_bound_at_all_is_reported():
    """No `path_under` is not a loose bound, it is no bound: every path the
    process can open, which is the one the comments in the example warn about."""
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="reads", actions=("tool",),
                    tools=("Read",), users=("you@example.com",))],
        roster=ROSTER,
    )
    (found,) = checks.allows_outside_their_roots(policy, settings())
    assert found[0] == "reads"
    assert "no path_under" in found[1]


def test_a_shell_allow_is_not_asked_for_a_path_bound():
    """A command names no path, so `path_under` could never be satisfied.

    Asking it for one would report every correctly written shell rule, which is
    how a report stops being read.
    """
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="commands", actions=("tool",),
                    tools=("shell",), users=("you@example.com",),
                    command_matches=("pytest*",))],
        roster=ROSTER,
    )
    assert checks.allows_outside_their_roots(policy, settings()) == []


def test_an_allow_matching_argv_shapes_is_reported():
    """H4: `command_matches` is sound as a deny and unsound as an allow.

    `ls *` matches `ls -a && anything-else`, so an allow built on it grants
    whatever follows the pattern it recognised.
    """
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="commands", actions=("tool",),
                    tools=("shell",), users=("you@example.com",),
                    command_matches=("pytest*",))],
        roster=ROSTER,
    )
    assert checks.allows_matching_commands(policy) == ["commands"]


def test_a_deny_matching_argv_shapes_is_not_reported():
    policy = RulesPolicy(
        rules=[Rule(effect="deny", name="never", actions=("tool",),
                    command_matches=("*git push*",))],
        roster=ROSTER,
    )
    assert checks.allows_matching_commands(policy) == []


def test_a_name_whose_commands_nothing_narrows_is_reported():
    """An allow grants shell and no deny shapes it: anything may run."""
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="commands", actions=("tool",),
                    tools=("shell",), users=("you@example.com",),
                    agents=("shared",))],
        roster=ROSTER,
    )
    assert checks.names_with_unbounded_commands(policy, settings()) == ["shared"]


def test_a_name_whose_commands_a_deny_shapes_is_not_reported():
    """What the shipped example does: allow by shape, deny the known-bad shapes."""
    policy = RulesPolicy(
        rules=[
            Rule(effect="allow", name="commands", actions=("tool",),
                 tools=("shell",), users=("you@example.com",), agents=("shared",)),
            Rule(effect="deny", name="never", actions=("tool",),
                 command_matches=("*git push*",)),
        ],
        roster=ROSTER,
    )
    assert checks.names_with_unbounded_commands(policy, settings()) == []


def test_a_name_with_no_shell_allow_at_all_is_not_reported():
    """Nothing can run, which is tighter than any deny. The quietest posture
    must not read as the loosest one."""
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="reads", actions=("tool",),
                    tools=("Read",), users=("you@example.com",))],
        roster=ROSTER,
    )
    assert checks.names_with_unbounded_commands(policy, settings()) == []


def test_a_name_without_a_budget_or_a_turn_ceiling_is_reported():
    """Zero means no ceiling in agents.toml, deliberately -- so on a machine
    left running it is a decision, and the readout is where it gets noticed."""
    found = checks.names_without_ceilings(settings(agent(budget=0.0, turns=0)))
    assert found == [("shared", "max_budget_usd and max_turns")]


def test_a_name_missing_only_one_ceiling_says_which():
    (one,) = checks.names_without_ceilings(settings(agent(budget=5.0, turns=0)))
    assert one == ("shared", "max_turns")


def test_a_name_with_both_ceilings_is_not_reported():
    assert checks.names_without_ceilings(settings(agent())) == []


def test_an_unnamed_rule_is_reported_by_position():
    """A finding about "(unnamed)" is a finding nobody can act on."""
    policy = RulesPolicy(
        rules=[
            Rule(effect="allow", name="reads", actions=("tool",)),
            Rule(effect="deny", actions=("tool",)),
        ],
        roster=ROSTER,
    )
    assert checks.unnamed_rules(policy) == [2]


def test_named_rules_are_not_reported():
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="reads", actions=("tool",))], roster=ROSTER
    )
    assert checks.unnamed_rules(policy) == []


# ================================================= family B: wholeness


def test_a_name_with_no_grant_row_is_reported():
    """A grant row is required to address a name, so a name without one is
    configured and unreachable -- the half-applied state §11 warns about."""
    assert checks.names_without_grants(("shared", "scout"), [grant(name="shared")]) \
        == ["scout"]


def test_a_name_whose_grant_is_held_by_someone_is_not_reported():
    assert checks.names_without_grants(("shared",), [grant()]) == []


def test_the_name_in_a_grant_row_is_matched_regardless_of_case():
    """Every other name comparison here folds case; one that did not would
    report a working grant as missing."""
    assert checks.names_without_grants(("Shared",), [grant(name="shared")]) == []


def test_a_grant_for_a_name_that_is_not_in_the_catalogue_is_reported():
    """The other direction: a row nothing can use, usually a typo or a name
    that was removed from the catalogue and left in the store."""
    assert checks.grants_for_unknown_names(("shared",), [grant(name="scuot")]) \
        == ["scuot"]


def test_a_grant_for_a_known_name_is_not_reported():
    assert checks.grants_for_unknown_names(("shared",), [grant()]) == []


def test_an_unknown_name_is_reported_once_however_many_rows_it_has():
    rows = [grant(name="scuot", verb="ask"), grant(name="scuot", verb="read")]
    assert checks.grants_for_unknown_names(("shared",), rows) == ["scuot"]


def test_a_rule_naming_an_agent_the_catalogue_does_not_declare_is_reported():
    """A rule about nothing, and if it was a deny then what it meant to refuse
    is allowed. The same class as a grant row for a name that does not exist."""
    policy = RulesPolicy(
        rules=[Rule(effect="deny", name="not here", actions=("tool",),
                    agents=("shrared",))],
        roster=ROSTER,
    )
    assert checks.dead_rules(policy, ("shared",)) == ["not here"]


def test_a_rule_naming_a_glob_that_matches_a_name_is_live():
    policy = RulesPolicy(
        rules=[Rule(effect="deny", name="all of them", actions=("tool",),
                    agents=("shar*",))],
        roster=ROSTER,
    )
    assert checks.dead_rules(policy, ("shared",)) == []


def test_a_rule_naming_no_agents_is_not_dead():
    """It reaches every name, which is the breadth other assertions report."""
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="everywhere", actions=("tool",))],
        roster=ROSTER,
    )
    assert checks.dead_rules(policy, ("shared",)) == []


def test_an_empty_catalogue_kills_the_rules_that_name_a_name_and_no_others():
    """Handed no names, only a rule that asked for one is dead.

    `config.load` refuses a catalogue with no agents, so the command cannot
    reach this -- but the predicate is callable on its own, and the distinction
    is the whole reason the `rule.agents` guard is there rather than left to
    `_applies_to`. Written after a mutation deleting that guard survived.
    """
    policy = RulesPolicy(
        rules=[
            Rule(effect="allow", name="everywhere", actions=("tool",)),
            Rule(effect="deny", name="somewhere", actions=("tool",),
                 agents=("shared",)),
        ],
        roster=ROSTER,
    )
    assert checks.dead_rules(policy, ()) == ["somewhere"]


def test_a_granted_person_who_is_not_on_the_roster_is_reported():
    """`decide` refuses an unknown principal before any conferral check, so
    this grant can never be used by the person it names."""
    policy = RulesPolicy(rules=[], roster=ROSTER)
    assert checks.granted_people_off_roster(policy, [grant(person="who@example.com")]) \
        == ["who@example.com"]


def test_a_granted_person_on_the_roster_is_not_reported():
    policy = RulesPolicy(rules=[], roster=ROSTER)
    assert checks.granted_people_off_roster(policy, [grant()]) == []


def test_a_roster_identity_holding_nothing_is_reported_as_inert():
    """Not a defect on its own -- a new teammate is inert until granted -- but
    it is the difference between "added" and "added and able to ask"."""
    policy = RulesPolicy(rules=[], roster=ROSTER)
    assert checks.inert_roster_identities(policy, [grant()]) == ["them@example.com"]


def test_a_roster_identity_holding_a_grant_is_not_inert():
    policy = RulesPolicy(rules=[], roster={"U1": "you@example.com"})
    assert checks.inert_roster_identities(policy, [grant()]) == []


def test_a_revoked_row_does_not_keep_a_person_from_being_inert():
    """`current()` is what gets passed in, so a superseded row is already gone.
    Pinned anyway: passing `history()` here would silently invert the answer."""
    policy = RulesPolicy(rules=[], roster={"U1": "you@example.com"})
    assert checks.inert_roster_identities(policy, []) == ["you@example.com"]


# ------------------------------------------------- the credential


def test_no_credential_anywhere_is_reported(tmp_path):
    """Existence only: nothing here reads a value, and no value is printed."""
    (found,) = checks.credentials({}, tmp_path / "nohome")
    assert "no credential" in found.lower()


def test_an_api_key_alone_is_not_reported(tmp_path):
    assert checks.credentials({"ANTHROPIC_API_KEY": "sk-whatever"},
                              tmp_path / "nohome") == []


def test_two_credentials_in_the_environment_are_reported(tmp_path):
    (found,) = checks.credentials(
        {"ANTHROPIC_API_KEY": "sk-a", "CLAUDE_CODE_OAUTH_TOKEN": "oat-b"},
        tmp_path / "nohome",
    )
    assert "both" in found.lower()


def test_a_stored_login_beside_an_environment_credential_is_reported(tmp_path):
    """The one that bites: the stored login wins, so the env credential is
    ignored until that login stops refreshing, weeks later."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    (found,) = checks.credentials({"ANTHROPIC_API_KEY": "sk-a"}, home)
    assert "stored login" in found.lower()
    assert "takes precedence" in found.lower()


def test_a_stored_login_alone_is_not_reported(tmp_path):
    """A signed-in home is a legitimate answer where the service keeps it --
    install-service.sh says so for a Linux user unit. Not this box's posture,
    and not this command's business to refuse."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    assert checks.credentials({}, home) == []


def test_an_empty_environment_value_is_no_credential(tmp_path):
    """`ANTHROPIC_API_KEY=` in a .env reads as set and holds nothing."""
    (found,) = checks.credentials({"ANTHROPIC_API_KEY": "  "}, tmp_path / "nohome")
    assert "no credential" in found.lower()


def test_the_credential_value_is_never_in_the_finding(tmp_path):
    """The one assertion that matters most: a report people paste into Slack."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    for found in checks.credentials(
        {"ANTHROPIC_API_KEY": "sk-secret-value", "CLAUDE_CODE_OAUTH_TOKEN": "oat-secret"},
        home,
    ):
        assert "sk-secret-value" not in found
        assert "oat-secret" not in found


# ================================================= the two reports


def test_the_posture_report_names_its_family_and_assertion():
    policy = RulesPolicy(
        rules=[Rule(effect="allow", name="reads", actions=("tool",), tools=("Read",))],
        roster=ROSTER,
    )
    found = checks.posture(policy, settings())
    assert found
    assert {f.family for f in found} == {"posture"}
    assert all(f.assertion and f.text for f in found)
    # Every one of them, with no exception: a dial position must not be able to
    # fail a deploy, and this is the assertion that keeps a seventh call site
    # from quietly acquiring the default.
    assert not any(f.gating for f in found)


def test_only_the_inert_identity_is_a_non_gating_wholeness_finding():
    """The rest are defects, and a deploy should refuse to land on them."""
    policy = RulesPolicy(
        rules=[Rule(effect="deny", name="not here", actions=("tool",),
                    agents=("shrared",))],
        roster=ROSTER,
        owners={"shared": ("typo@example.com",)},
    )
    found = checks.wholeness(
        settings(agent(name="scout")), policy,
        [grant(name="scuot", person="who@example.com")],
        env={}, home=Path("/nowhere-at-all"),
    )
    assert {f.assertion for f in found if not f.gating} == {"inert-identity"}


def test_a_tight_posture_reports_nothing():
    """Reachable, not hypothetical: this is what the dial at its tightest is."""
    policy = RulesPolicy(
        rules=[
            Rule(effect="allow", name="reads", actions=("tool",), tools=("Read",),
                 users=("you@example.com",), agents=("shared",),
                 path_under="/repos/shared"),
            Rule(effect="allow", name="asking", actions=("ask", "list"),
                 users=("you@example.com",)),
        ],
        roster=ROSTER,
    )
    assert checks.posture(policy, settings()) == []


def test_the_wholeness_report_covers_every_assertion_in_the_family():
    """One report, so a new predicate that nobody wired in shows up here."""
    policy = RulesPolicy(
        rules=[Rule(effect="deny", name="not here", actions=("tool",),
                    agents=("shrared",))],
        roster=ROSTER,
        owners={"shared": ("typo@example.com",)},
    )
    found = checks.wholeness(
        settings(agent(name="scout")), policy,
        [grant(name="scuot", person="who@example.com")],
        env={}, home=Path("/nowhere-at-all"),
    )
    assert {f.family for f in found} == {"wholeness"}
    slugs = {f.assertion for f in found}
    assert slugs == {
        "dead-rule", "name-without-grants", "grant-for-unknown-name",
        "granted-person-off-roster", "inert-identity", "unrostered-owner",
        "credential",
    }


def test_a_store_that_could_not_be_read_skips_its_assertions_rather_than_answering():
    """None is not an empty store.

    Answering "every name is ungranted" because a database would not open sends
    somebody to re-seed a store that is fine. The assertions that do not touch
    it still answer, and the caller reports what was skipped.
    """
    policy = RulesPolicy(rules=[], roster=ROSTER,
                         owners={"shared": ("typo@example.com",)})
    found = checks.wholeness(
        settings(), policy, None, env={}, home=Path("/nowhere-at-all")
    )
    slugs = {f.assertion for f in found}
    assert slugs == {"unrostered-owner", "credential"}


def test_a_whole_world_reports_nothing(tmp_path):
    policy = RulesPolicy(rules=[], roster={"U1": "you@example.com"},
                         owners={"shared": ("you@example.com",)})
    assert checks.wholeness(
        settings(), policy, [grant()],
        env={"ANTHROPIC_API_KEY": "sk-whatever"}, home=tmp_path / "nohome",
    ) == []
