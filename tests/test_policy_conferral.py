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


async def test_the_target_check_comes_before_the_authority_check(policy):
    d = await policy.decide(grant_request(by=KNOWN_A, target="stranger@example.com", confers="read"))
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
    assert d.allow is False and d.rule == "above the grantor"


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


async def test_the_check_has_no_allow_of_its_own():
    from ricardi.policy import _conferral_refusal

    p = RulesPolicy(rules=[], roster=ROSTER, owners={"n1": ("owner@example.com",)})
    assert _conferral_refusal(p, grant_request(by=OWNER, target="bob@example.com", confers="read")) is None


async def test_a_policy_without_a_store_has_owners_but_no_delegates():
    p = RulesPolicy(rules=[ALLOW_CONFER], roster=ROSTER, owners={"n1": ("owner@example.com",)})
    assert (await p.decide(grant_request(by=OWNER, target="bob@example.com", confers="read"))).allow is True
    d = await p.decide(grant_request(by=DELEGATE_READ, target="bob@example.com", confers="read"))
    assert d.rule == "not a grantor"
