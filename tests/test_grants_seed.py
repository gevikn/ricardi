from __future__ import annotations

from ricardi.grants import GrantStore, seed_from_policy
from ricardi.policy import Rule, RulesPolicy

ROSTER = {"U1": "a@b.com", "U2": "c@d.com"}
NAMES = ["shared", "other"]


def _allow(*agents: str, actions=("ask",), users=()) -> Rule:
    return Rule(name="r", effect="allow", actions=actions, agents=agents, users=users)


def test_every_roster_member_is_granted_ask_on_the_names_their_rules_allow(tmp_path):
    policy = RulesPolicy(rules=[_allow("shared")], roster=ROSTER)
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, policy, names=NAMES, by="admin@b.com") == 2
    assert s.holds("a@b.com", "shared", "ask") is True
    assert s.holds("c@d.com", "shared", "ask") is True


def test_seeding_is_idempotent(tmp_path):
    # Running the deploy step twice must not double the history or change holds.
    policy = RulesPolicy(rules=[_allow("shared")], roster=ROSTER)
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, policy, names=NAMES, by="admin@b.com") == 2
    assert seed_from_policy(s, policy, names=NAMES, by="admin@b.com") == 0
    assert len(s.history()) == 2
    assert s.holds("a@b.com", "shared", "ask") is True


def test_a_name_no_rule_allows_is_not_granted(tmp_path):
    policy = RulesPolicy(
        rules=[
            _allow("shared"),
            Rule(name="d", effect="deny", actions=("ask",), agents=("secret",)),
            _allow("tools-only", actions=("tool",)),
        ],
        roster=ROSTER,
    )
    s = GrantStore.open(tmp_path / "g.db")
    seed_from_policy(s, policy, names=NAMES, by="admin@b.com")
    assert s.holds("a@b.com", "secret", "ask") is False
    assert s.holds("a@b.com", "tools-only", "ask") is False
    assert s.holds("a@b.com", "unlisted", "ask") is False


def test_an_allow_naming_no_agents_seeds_every_catalogue_name(tmp_path):
    # Such a rule already permits every name that exists today, so enumerating
    # them reproduces current reach. A wildcard would also cover names added
    # later, which must not be handed to everyone.
    policy = RulesPolicy(rules=[_allow()], roster=ROSTER)
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, policy, names=NAMES, by="admin@b.com") == 4
    assert {g.name for g in s.history()} == set(NAMES)
    assert s.holds("a@b.com", "*", "ask") is False
    assert s.holds("a@b.com", "added-later", "ask") is False


def test_an_allow_naming_no_agents_seeds_nothing_for_an_empty_catalogue(tmp_path):
    policy = RulesPolicy(rules=[_allow()], roster=ROSTER)
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, policy, names=[], by="admin@b.com") == 0
    assert s.history() == ()


def test_an_allow_naming_no_actions_or_agents_seeds_every_catalogue_name(tmp_path):
    # An empty `actions` matches every action in Rule.matches, ask included, so
    # skipping it would lock out people the rule lets in today.
    policy = RulesPolicy(rules=[_allow(actions=())], roster=ROSTER)
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, policy, names=NAMES, by="admin@b.com") == 4
    for email in ("a@b.com", "c@d.com"):
        for name in NAMES:
            assert s.holds(email, name, "ask") is True


def test_a_rule_naming_agents_is_not_widened_to_the_catalogue(tmp_path):
    policy = RulesPolicy(rules=[_allow("shared")], roster={"U1": "a@b.com"})
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, policy, names=NAMES, by="admin@b.com") == 1
    assert s.holds("a@b.com", "other", "ask") is False


def test_a_glob_in_agents_is_not_a_name_and_is_not_granted(tmp_path):
    policy = RulesPolicy(rules=[_allow("proj-*", "shared")], roster=ROSTER)
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, policy, names=NAMES, by="admin@b.com") == 2
    assert {g.name for g in s.history()} == {"shared"}


def test_a_rule_scoped_to_users_grants_only_those_users(tmp_path):
    policy = RulesPolicy(rules=[_allow("shared", users=("a@b.com",))], roster=ROSTER)
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, policy, names=NAMES, by="admin@b.com") == 1
    assert s.holds("c@d.com", "shared", "ask") is False


def test_a_revoked_grant_is_not_resurrected_by_reseeding(tmp_path):
    # The deploy step may run again after an admin has revoked someone. Seeding
    # is a migration, not a sync: it must not quietly undo a deliberate revoke.
    policy = RulesPolicy(rules=[_allow("shared")], roster={"U1": "a@b.com"})
    s = GrantStore.open(tmp_path / "g.db")
    seed_from_policy(s, policy, names=NAMES, by="admin@b.com")
    s.add(person="a@b.com", name="shared", verb="ask", by="admin@b.com", granted=False)
    assert seed_from_policy(s, policy, names=NAMES, by="admin@b.com") == 0
    assert s.holds("a@b.com", "shared", "ask") is False


def test_emails_are_normalised_and_duplicates_collapse(tmp_path):
    policy = RulesPolicy(
        rules=[_allow("shared")], roster={"U1": " A@B.com ", "U2": "a@b.com"}
    )
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, policy, names=NAMES, by="admin@b.com") == 1
    assert s.holds("a@b.com", "shared", "ask") is True


def test_seeding_an_empty_roster_writes_nothing_and_says_so(tmp_path):
    s = GrantStore.open(tmp_path / "g.db")
    assert seed_from_policy(s, RulesPolicy(), names=NAMES, by="admin@b.com") == 0
    assert s.history() == ()
