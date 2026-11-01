import pytest

from ricardi.policy import (
    PLANE2_VERBS,
    TIERS,
    Principal,
    Request,
    Rule,
    at_most,
    load,
    tier_rank,
)

KNOWN = Principal(id="U1", email="you@example.com", known=True)

ONE_ASK_ALLOW = '[[rule]]\neffect="allow"\nactions=["ask"]\n'


@pytest.fixture
def written_toml(tmp_path):
    def write(text: str):
        path = tmp_path / "policy.toml"
        path.write_text(text, encoding="utf-8")
        return path

    return write


def test_tier_ordering_is_total_and_absence_is_the_floor():
    assert tier_rank("") == 0 and tier_rank("read") == 1 and tier_rank("write") == 2
    assert at_most("read", "write") and at_most("read", "read")
    assert not at_most("write", "read")
    assert TIERS == ("read", "write")


def test_an_unknown_tier_ranks_below_everything_rather_than_above():
    # "admin" must not out-rank write by sorting later; it is not a tier.
    assert tier_rank("admin") == 0
    assert not at_most("admin", "write")
    # The guard covers the ceiling too, not only the tier.
    assert not at_most("read", "admin")


def test_a_rule_naming_confers_distinguishes_one_conferral_from_another():
    rule = Rule(effect="allow", actions=("grant",), confers=("read",))
    assert rule.matches(Request(action="grant", confers="read", principal=KNOWN))
    assert not rule.matches(Request(action="grant", confers="write", principal=KNOWN))


def test_a_rule_naming_no_new_dimension_still_matches_every_request():
    # The existing semantics: every dimension a rule names must match, and the
    # ones it does not name match all. A rule written before tiers existed must
    # not stop matching now.
    rule = Rule(effect="allow", actions=("ask",))
    assert rule.matches(Request(action="ask", tier="write", principal=KNOWN))


def test_tiers_and_targets_match_like_users_and_confers_like_channels():
    assert Rule(effect="allow", tiers=("write",)).matches(Request(action="tool", tier="write", principal=KNOWN))
    assert Rule(effect="allow", targets=("*@example.com",)).matches(Request(action="grant", target="bob@example.com", principal=KNOWN))
    assert not Rule(effect="allow", confers=("rea*",)).matches(Request(action="grant", confers="read", principal=KNOWN))


def test_the_new_rule_keys_load_from_toml(written_toml):
    policy = load(written_toml('[[rule]]\neffect="allow"\nactions=["grant"]\nconfers=["read"]\ntargets=["*@example.com"]\ntiers=["write"]\n'))
    assert policy.rules[0].confers == ("read",)
    assert policy.rules[0].targets == ("*@example.com",)
    assert policy.rules[0].tiers == ("write",)


def test_the_plane_two_verbs_are_exactly_the_four_named():
    assert PLANE2_VERBS == frozenset({"grant", "revoke", "reset", "scope"})


async def test_an_undesigned_plane_two_verb_denies_by_default(written_toml):
    policy = load(written_toml(ONE_ASK_ALLOW))
    assert (await policy.decide(Request(action="scope", principal=KNOWN, agent="n1"))).allow is False


def test_a_rule_naming_tiers_does_not_match_a_different_tier():
    rule = Rule(effect="allow", tiers=("write",))
    assert not rule.matches(Request(action="tool", tier="read", principal=KNOWN))


def test_tiers_glob_like_users():
    rule = Rule(effect="allow", tiers=("wri*",))
    assert rule.matches(Request(action="tool", tier="write", principal=KNOWN))
    assert not rule.matches(Request(action="tool", tier="read", principal=KNOWN))


def test_a_star_tier_rule_does_not_match_an_empty_tier():
    # _any_glob refuses an empty value, so absence matches no pattern at all.
    rule = Rule(effect="allow", tiers=("*",))
    assert rule.matches(Request(action="tool", tier="read", principal=KNOWN))
    assert not rule.matches(Request(action="tool", tier="", principal=KNOWN))


def test_a_rule_naming_targets_does_not_match_a_different_target():
    rule = Rule(effect="allow", targets=("*@example.com",))
    assert not rule.matches(Request(action="grant", target="bob@other.org", principal=KNOWN))
    assert not rule.matches(Request(action="grant", target="", principal=KNOWN))
