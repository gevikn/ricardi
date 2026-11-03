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


# --- the read tier ----------------------------------------------------------

import inspect

from ricardi.policy import WRITING_TOOLS, _tier_refusal

ALLOW_ALL = '[[rule]]\neffect="allow"\n'


def _tool(tool: str, tier: str, **kw) -> Request:
    return Request(action="tool", principal=KNOWN, agent="n1", tier=tier, tool=tool,
                   tool_input=kw.get("tool_input", {"file_path": "/workspace/n1/a.py"}))


async def test_a_reader_may_not_edit_or_write(written_toml):
    policy = load(written_toml(ALLOW_ALL))
    for tool in ("Write", "Edit", "NotebookEdit"):
        d = await policy.decide(_tool(tool, "read"))
        assert d.allow is False
        assert "read" in d.reason
        assert d.rule == "tier"


async def test_a_reader_still_gets_the_reading_tools_and_the_shell(written_toml):
    policy = load(written_toml(ALLOW_ALL))
    for tool in ("Read", "Grep", "Glob", "Bash"):
        assert (await policy.decide(_tool(tool, "read"))).allow is True


async def test_a_writer_may_write(written_toml):
    policy = load(written_toml(ALLOW_ALL))
    assert (await policy.decide(_tool("Write", "write"))).allow is True


async def test_no_tier_row_leaves_the_rules_to_decide(written_toml):
    # Backward compatibility, and deliberate: absence of a tier row means
    # "unconstrained by tier", not "none". Enforcing none belongs with the
    # channel-to-name binding, where an invitee without a tier exists.
    policy = load(written_toml(ALLOW_ALL))
    assert (await policy.decide(_tool("Write", ""))).allow is True


async def test_a_storeless_policy_still_refuses_a_caller_supplied_read(written_toml):
    # `grants=None` leaves a caller's tier alone on purpose (it is the mode a
    # deployment that has not opted in runs, and the mode these tests use). The
    # refusal must fire there too, so "only ever subtracts" holds in both modes.
    policy = load(written_toml(ALLOW_ALL))
    assert policy.grants is None
    d = await policy.decide(_tool("Write", "read"))
    assert d.allow is False and d.rule == "tier"


async def test_an_unrecognised_tier_is_left_to_the_rules_not_treated_as_read(written_toml):
    # The rules allow Write, so only the tier could refuse it. "admin" is not
    # "read"; it must not be constrained by a refusal that is read's alone.
    policy = load(written_toml(ALLOW_ALL))
    assert _tier_refusal(_tool("Write", "admin")) is None
    assert (await policy.decide(_tool("Write", "admin"))).allow is True


async def test_a_writer_is_not_refused_where_the_rules_allow(written_toml):
    # Same allowing policy as the reader's refusal: the one difference is the tier.
    policy = load(written_toml(ALLOW_ALL))
    assert (await policy.decide(_tool("Write", "read"))).allow is False
    assert (await policy.decide(_tool("Write", "write"))).allow is True


async def test_a_denying_rule_still_beats_a_writer(written_toml):
    policy = load(written_toml("""
[[rule]]
effect = "allow"
[[rule]]
effect = "deny"
tools = ["Write"]
"""))
    assert (await policy.decide(_tool("Write", "write"))).allow is False


async def test_an_unlisted_writing_tool_is_refused_by_default_for_both_tiers(written_toml):
    # The backstop WRITING_TOOLS rests on: a tool outside the set matches no
    # allow rule naming `tools`, so the default deny refuses it with no rule
    # named. A tools-less allow rule added later would make this fail.
    policy = load(written_toml("""
[[rule]]
effect = "allow"
actions = ["ask"]
[[rule]]
effect = "allow"
actions = ["tool"]
tools = ["Read", "Write", "Edit", "Bash"]
"""))
    for tool in ("Patch", "ApplyPatch", "TodoWrite"):
        for tier in ("read", "write"):
            d = await policy.decide(_tool(tool, tier))
            assert d.allow is False and d.rule == "", (tool, tier, d)


async def test_the_tier_check_is_case_insensitive_like_the_rules(written_toml):
    policy = load(written_toml(ALLOW_ALL))
    for tool in ("write", "EDIT", "notebookedit"):
        d = await policy.decide(_tool(tool, "read"))
        assert d.allow is False and d.rule == "tier", tool


def test_the_writing_tools_are_the_four_named():
    assert WRITING_TOOLS == frozenset({"Write", "Edit", "NotebookEdit", "MultiEdit"})


async def test_a_reader_is_refused_multiedit_even_under_a_tools_less_allow(written_toml):
    policy = load(written_toml(ALLOW_ALL))
    d = await policy.decide(_tool("MultiEdit", "read"))
    assert d.allow is False and d.rule == "tier"


def test_the_tier_comparison_is_case_insensitive_too():
    for tier in ("READ", "Read"):
        d = _tier_refusal(_tool("Write", tier))
        assert d is not None and d.rule == "tier", tier


def test_the_tier_refusal_cannot_allow_anything():
    # Structural, like _flag_refusal: there is no allow=True to reach.
    assert "allow=True" not in inspect.getsource(_tier_refusal)
    assert _tier_refusal(_tool("Write", "write")) is None
    assert _tier_refusal(_tool("Read", "read")) is None
    assert _tier_refusal(Request(action="ask", principal=KNOWN, tier="read", tool="Write")) is None


# -- a tiers rule is inert without a store, and that failed open ----------

# The policy the issue measured: tools allowed broadly, writing denied to
# anyone holding `read`. Without a store `request.tier` is always "", and
# `_any_glob` returns False for an empty value -- even against `*` -- so the
# deny could never match and the write went through the allow, audited as the
# allow. A dead allow merely refuses; a dead deny fails open.
TIERED = (
    '[[rule]]\n'
    'name="tools for the team"\n'
    'effect="allow"\n'
    'actions=["tool"]\n'
    'users=["*@example.com"]\n'
    '\n'
    '[[rule]]\n'
    'name="readers may not write"\n'
    'effect="deny"\n'
    'actions=["tool"]\n'
    'tools=["Write","Edit"]\n'
    'tiers=["read"]\n'
)


async def test_a_tiers_rule_without_a_store_refuses_rather_than_widening(written_toml):
    policy = load(written_toml(TIERED))
    assert policy.grants is None
    d = await policy.decide(_tool("Write", ""))
    assert d.allow is False


async def test_the_refusal_says_which_rule_needs_the_store(written_toml):
    policy = load(written_toml(TIERED))
    d = await policy.decide(_tool("Write", ""))
    assert "readers may not write" in d.reason
    assert d.rule == "tiers-without-a-store"


async def test_a_policy_with_no_tiers_rule_still_decides_storelessly(written_toml):
    # The mode every deployment that has not opted in runs. It must decide
    # exactly as before, which is what makes the guard safe to add.
    policy = load(written_toml(ALLOW_ALL))
    assert policy.grants is None
    assert (await policy.decide(_tool("Write", ""))).allow is True


async def test_the_same_policy_decides_normally_once_a_store_is_open(
    written_toml, tmp_path
):
    from ricardi.grants import GrantStore

    policy = load(written_toml(TIERED))
    store = GrantStore.open(tmp_path / "grants.db")
    try:
        policy.grants = store
        # Nobody holds `read`, so the deny does not apply and the allow stands.
        assert (await policy.decide(_tool("Write", ""))).allow is True
        store.add(person=KNOWN.email, name="n1", verb="read", by="t")
        refused = await policy.decide(_tool("Write", ""))
        assert refused.allow is False
        assert refused.rule != "tiers-without-a-store"
    finally:
        store.close()
