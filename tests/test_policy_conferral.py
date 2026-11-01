"""Who may confer what: the escalation check for grant and revoke.

The authority: an owner of that name may confer any tier the name's ceiling
rules permit; a delegate holding the `grant` verb may confer at or below their
own tier; nobody else may confer at all. The check only ever refuses.
"""

from __future__ import annotations

import pytest

from ricardi.grants import GrantStore
from ricardi.policy import Principal, Request, Rule, RulesPolicy, at_most

OWNER = Principal(id="UO", email="owner@example.com", known=True)
OWNER_OF_N1 = OWNER
DELEGATE_READ = Principal(id="UD", email="delegate@example.com", known=True)
KNOWN_A = Principal(id="UA", email="a@example.com", known=True)

# Pre-normalised: RulesPolicy.__init__ does not normalise owners, `load` does.
ROSTER = {
    "UO": "owner@example.com",
    "UD": "delegate@example.com",
    "UA": "a@example.com",
    "UB": "bob@example.com",
}
ALLOW_CONFER = Rule(effect="allow", name="conferring", actions=("grant", "revoke"))
ALLOW_ASK_ONLY = Rule(effect="allow", name="asking", actions=("ask",))


def _policy(tmp_path, rules=(ALLOW_CONFER,)):
    store = GrantStore.open(tmp_path / "g.db")
    store.add(person="delegate@example.com", name="n1", verb="grant", by="t")
    store.add(person="delegate@example.com", name="n1", verb="read", by="t")
    return RulesPolicy(
        rules=list(rules), roster=ROSTER, grants=store,
        owners={"n1": ("owner@example.com",)},
    )


def grant_request(*, by, target, confers, agent="n1"):
    return Request(action="grant", principal=by, agent=agent, target=target, confers=confers)


def revoke_request(*, by, target, agent="n1"):
    return Request(action="revoke", principal=by, agent=agent, target=target)


@pytest.fixture
def policy(tmp_path):
    return _policy(tmp_path)


def test_at_most_refuses_a_non_tier_ceiling_even_for_the_floor():
    # Discriminating input: True without the ceiling half of the guard (0 <= 0).
    assert not at_most("", "admin")


async def test_an_owner_may_confer_write(policy):
    assert (await policy.decide(grant_request(by=OWNER, target="bob@example.com", confers="write"))).allow is True


async def test_a_delegate_may_confer_at_or_below_their_own_tier(policy):
    assert (await policy.decide(grant_request(by=DELEGATE_READ, target="bob@example.com", confers="read"))).allow is True
    d = await policy.decide(grant_request(by=DELEGATE_READ, target="bob@example.com", confers="write"))
    assert d.allow is False and d.rule == "above the grantor"


async def test_someone_with_neither_ownership_nor_the_grant_verb_may_not_confer(policy):
    d = await policy.decide(grant_request(by=KNOWN_A, target="bob@example.com", confers="read"))
    assert d.allow is False and d.rule == "not a grantor"


async def test_an_owner_of_one_name_may_not_confer_on_another(policy):
    d = await policy.decide(grant_request(by=OWNER_OF_N1, agent="n2", target="bob@example.com", confers="read"))
    assert d.allow is False and d.rule == "not a grantor"


async def test_a_delegate_of_one_name_may_not_confer_on_another(policy):
    d = await policy.decide(grant_request(by=DELEGATE_READ, agent="n2", target="bob@example.com", confers="read"))
    assert d.allow is False and d.rule == "not a grantor"


async def test_nobody_may_raise_their_own_tier(policy):
    d = await policy.decide(grant_request(by=DELEGATE_READ, target=DELEGATE_READ.email, confers="write"))
    assert d.allow is False


async def test_a_target_who_is_not_on_the_roster_is_refused(policy):
    d = await policy.decide(grant_request(by=OWNER, target="stranger@example.com", confers="read"))
    assert d.allow is False and d.rule == "target not on the roster"


async def test_authority_is_checked_before_the_target_so_the_roster_does_not_leak(policy):
    on = await policy.decide(grant_request(by=KNOWN_A, target="bob@example.com", confers="read"))
    off = await policy.decide(grant_request(by=KNOWN_A, target="stranger@example.com", confers="read"))
    assert on.rule == off.rule == "not a grantor"
    assert on.reason == off.reason
    # A real grantor still hears the specific problem.
    d = await policy.decide(grant_request(by=OWNER, target="stranger@example.com", confers="read"))
    assert d.rule == "target not on the roster"


async def test_the_target_and_the_name_are_normalised_before_the_check(policy):
    assert (await policy.decide(grant_request(by=OWNER, target=" BOB@Example.com ", confers="read", agent="N1"))).allow is True


async def test_a_roster_entry_is_normalised_too(tmp_path):
    p = RulesPolicy(rules=[ALLOW_CONFER], roster={"UB": " Bob@Example.COM "},
                    owners={"n1": ("owner@example.com",)})
    assert (await p.decide(grant_request(by=OWNER, target="bob@example.com", confers="read"))).allow is True


async def test_a_conferral_that_is_not_a_tier_is_refused(policy):
    for value in ("admin", "exec", "", "READ "):
        assert (await policy.decide(grant_request(by=OWNER, target="bob@example.com", confers=value))).allow is False
    for value in ("admin", "exec", "grant", "", "READ "):
        d = await policy.decide(grant_request(by=OWNER, target="bob@example.com", confers=value))
        assert d.rule == "not a tier", value
    # Distinct from a real tier that outranks a delegate's own.
    d = await policy.decide(grant_request(by=DELEGATE_READ, target="bob@example.com", confers="write"))
    assert d.rule == "above the grantor"


async def test_a_delegate_with_no_tier_cannot_confer_anything(tmp_path):
    p = _policy(tmp_path)
    p.grants.add(person="delegate@example.com", name="n1", verb="read", by="t", granted=False)
    d = await p.decide(grant_request(by=DELEGATE_READ, target="bob@example.com", confers="read"))
    assert d.allow is False and d.rule == "no tier of your own"
    assert d.rule != "above the grantor"


async def test_revoke_is_not_bounded_by_the_grantors_tier(policy):
    assert (await policy.decide(revoke_request(by=DELEGATE_READ, target="bob@example.com"))).allow is True


async def test_revoke_still_needs_authority_and_a_rostered_target(policy):
    d = await policy.decide(revoke_request(by=KNOWN_A, target="bob@example.com"))
    assert d.allow is False and d.rule == "not a grantor"
    d = await policy.decide(revoke_request(by=OWNER, target="stranger@example.com"))
    assert d.allow is False and d.rule == "target not on the roster"


async def test_the_ceiling_still_decides_after_the_authority_check(tmp_path):
    # No rule names action="grant" at all -> refused by the default, with the
    # authority check having allowed nothing on its own.
    p = _policy(tmp_path, rules=(ALLOW_ASK_ONLY,))
    d = await p.decide(grant_request(by=OWNER, target="bob@example.com", confers="read"))
    assert d.allow is False and d.rule == ""


async def test_a_deny_rule_still_beats_an_owner(tmp_path):
    deny = Rule(effect="deny", name="no-write", actions=("grant",), confers=("write",))
    p = _policy(tmp_path, rules=(ALLOW_CONFER, deny))
    d = await p.decide(grant_request(by=OWNER, target="bob@example.com", confers="write"))
    assert d.allow is False and d.rule == "no-write"


async def test_an_unknown_principal_is_refused_as_such(policy):
    stranger = Principal(id="UX", email="owner@example.com", known=False)
    d = await policy.decide(grant_request(by=stranger, target="bob@example.com", confers="read"))
    assert d.allow is False and d.rule == "no roster entry"


async def test_the_check_has_no_allow_of_its_own(tmp_path):
    from ricardi.policy import _conferral_refusal

    p = _policy(tmp_path)
    cases = [
        grant_request(by=by, target=target, confers=confers, agent=agent)
        for by in (OWNER, DELEGATE_READ, KNOWN_A)
        for target in ("bob@example.com", "stranger@example.com", "")
        for confers in ("read", "write", "admin", "")
        for agent in ("n1", "n2")
    ] + [
        revoke_request(by=by, target=target, agent=agent)
        for by in (OWNER, DELEGATE_READ, KNOWN_A)
        for target in ("bob@example.com", "stranger@example.com", "")
        for agent in ("n1", "n2")
    ] + [
        Request(action=a, principal=by, agent=agent, target=target)
        for a in ("reset", "scope")
        for by in (OWNER, DELEGATE_READ, KNOWN_A)
        for target in ("bob@example.com", "stranger@example.com", "")
        for agent in ("n1", "n2")
    ]
    refused = 0
    for r in cases:
        d = _conferral_refusal(p, r)
        assert d is None or d.allow is False
        refused += d is not None
    assert refused > 0


async def test_a_deny_naming_a_target_holds_against_an_unnormalised_target(tmp_path):
    deny = Rule(effect="deny", name="not-bob", actions=("grant", "revoke"), targets=("bob@example.com",))
    p = _policy(tmp_path, rules=(ALLOW_CONFER, deny))
    for raw in ("bob@example.com", "BOB@example.com", " Bob@Example.com ", "  bob@example.com"):
        d = await p.decide(grant_request(by=OWNER, target=raw, confers="read"))
        assert d.allow is False and d.rule == "not-bob", raw
    d = await p.decide(revoke_request(by=OWNER, target=" bob@example.com "))
    assert d.allow is False and d.rule == "not-bob"


async def test_a_deny_naming_an_agent_holds_against_an_unnormalised_name(tmp_path):
    deny = Rule(effect="deny", name="not-n1", actions=("grant",), agents=("n1",))
    p = _policy(tmp_path, rules=(ALLOW_CONFER, deny))
    for raw in ("n1", " n1 ", "N1"):
        d = await p.decide(grant_request(by=OWNER, target="bob@example.com", confers="read", agent=raw))
        assert d.allow is False and d.rule == "not-n1", raw


async def test_reset_and_scope_need_a_grantor_even_when_a_rule_allows_them(tmp_path):
    p = _policy(tmp_path, rules=(Rule(effect="allow", name="all", actions=("reset", "scope")),))
    for verb in ("reset", "scope"):
        for target in ("", "bob@example.com", "stranger@example.com"):
            d = await p.decide(Request(action=verb, principal=KNOWN_A, agent="n1", target=target))
            expected = "not a grantor"  # authority first: no roster leak
            assert d.allow is False and d.rule == expected, (verb, target)
        # A grantor with no (or a rostered) target passes the check.
        assert (await p.decide(Request(action=verb, principal=OWNER, agent="n1"))).allow is True
        d = await p.decide(Request(action=verb, principal=OWNER, agent="n1", target="stranger@example.com"))
        assert d.rule == "target not on the roster"


async def test_a_policy_without_a_store_has_owners_but_no_delegates():
    p = RulesPolicy(rules=[ALLOW_CONFER], roster=ROSTER, owners={"n1": ("owner@example.com",)})
    assert (await p.decide(grant_request(by=OWNER, target="bob@example.com", confers="read"))).allow is True
    d = await p.decide(grant_request(by=DELEGATE_READ, target="bob@example.com", confers="read"))
    assert d.rule == "not a grantor"
