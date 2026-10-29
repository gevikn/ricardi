"""Grants at the decision point: a name can only be addressed with a grant row.

The check only ever subtracts. It sits after the deny pass and the roster
refusal so the audit keeps saying which of those fired, and before the allow
pass so no grant can produce an allow the rules would not have.
"""

from __future__ import annotations

import pytest

from ricardi.grants import GrantError, GrantStore
from ricardi.policy import Request, Rule, RulesPolicy

ALLOW_ASK = Rule(effect="allow", name="asking", actions=("ask",))
ALLOW_TOOL = Rule(effect="allow", name="tooling", actions=("tool",), tools=("Read",))
ROSTER = {"U1": "a@b.com"}


def _store(tmp_path):
    return GrantStore.open(tmp_path / "g.db")


def _ask(policy, uid="U1", agent="shared"):
    return Request(action="ask", principal=policy.principal(uid), agent=agent)


async def test_an_ask_without_a_grant_row_is_refused(tmp_path):
    # Absence is not corruption, and no row means no access (spec 15.4).
    policy = RulesPolicy(rules=[ALLOW_ASK], roster=ROSTER, grants=_store(tmp_path))
    d = await policy.decide(_ask(policy))
    assert d.allow is False
    assert d.rule == "no grant"
    assert d.reason == policy._default_reason(_ask(policy))


async def test_an_ask_with_a_grant_row_is_allowed(tmp_path):
    store = _store(tmp_path)
    store.add(person="A@B.com", name="shared", verb="ask", by="admin")
    policy = RulesPolicy(rules=[ALLOW_ASK], roster=ROSTER, grants=store)
    d = await policy.decide(_ask(policy))
    assert d.allow is True
    assert d.rule == "asking"


async def test_a_grant_for_one_name_does_not_cover_another(tmp_path):
    store = _store(tmp_path)
    store.add(person="a@b.com", name="shared", verb="ask", by="admin")
    policy = RulesPolicy(rules=[ALLOW_ASK], roster=ROSTER, grants=store)
    d = await policy.decide(_ask(policy, agent="other"))
    assert d.allow is False
    assert d.rule == "no grant"


async def test_a_revoked_grant_is_refused(tmp_path):
    store = _store(tmp_path)
    store.add(person="a@b.com", name="shared", verb="ask", by="admin")
    store.add(person="a@b.com", name="shared", verb="ask", by="admin", granted=False)
    policy = RulesPolicy(rules=[ALLOW_ASK], roster=ROSTER, grants=store)
    assert (await policy.decide(_ask(policy))).rule == "no grant"


async def test_a_grant_cannot_widen_what_policy_denies(tmp_path):
    # Subtract only: a deny rule plus a grant row is still a refusal, and the
    # audit records the named deny rather than the grant check.
    store = _store(tmp_path)
    store.add(person="a@b.com", name="shared", verb="ask", by="admin")
    deny = Rule(effect="deny", name="the named deny", actions=("ask",))
    policy = RulesPolicy(rules=[ALLOW_ASK, deny], roster=ROSTER, grants=store)
    d = await policy.decide(_ask(policy))
    assert d.allow is False
    assert d.rule == "the named deny"


async def test_a_denied_ask_without_a_grant_is_still_audited_as_the_deny(tmp_path):
    deny = Rule(effect="deny", name="the named deny", actions=("ask",))
    policy = RulesPolicy(rules=[ALLOW_ASK, deny], roster=ROSTER, grants=_store(tmp_path))
    assert (await policy.decide(_ask(policy))).rule == "the named deny"


async def test_a_grant_cannot_allow_what_no_rule_allows(tmp_path):
    store = _store(tmp_path)
    store.add(person="a@b.com", name="shared", verb="ask", by="admin")
    policy = RulesPolicy(rules=[], roster=ROSTER, grants=store)
    d = await policy.decide(_ask(policy))
    assert d.allow is False
    assert d.rule == ""


async def test_a_grant_for_someone_not_on_the_roster_does_nothing(tmp_path):
    # Review Focus 1: principal.known is false, so the unknown-asker refusal
    # still fires first and the grant never resurrects them.
    store = _store(tmp_path)
    store.add(person="stranger@x.com", name="shared", verb="ask", by="admin")
    policy = RulesPolicy(rules=[ALLOW_ASK], roster=ROSTER, grants=store)
    d = await policy.decide(_ask(policy, uid="UX"))
    assert d.allow is False
    assert d.rule == "no roster entry"


async def test_a_tool_call_is_unaffected_by_grants(tmp_path):
    # Step 2 grants cover the "ask" action only; plane-1 tool rules are step 3.
    policy = RulesPolicy(rules=[ALLOW_TOOL], roster=ROSTER, grants=_store(tmp_path))
    d = await policy.decide(Request(
        action="tool", principal=policy.principal("U1"), agent="shared",
        tool="Read", tool_input={"file_path": "x"},
    ))
    assert d.allow is True
    assert d.rule == "tooling"


async def test_no_grant_store_behaves_as_before():
    policy = RulesPolicy(rules=[ALLOW_ASK], roster=ROSTER)
    assert (await policy.decide(_ask(policy))).allow is True


async def test_an_unreadable_store_raises_rather_than_reading_as_no_grant(tmp_path):
    # Folded into "no grant" it would be audited as nobody ever being let in;
    # raised, the audit and app layers record a policy error with its text.
    store = _store(tmp_path)
    store.add(person="a@b.com", name="shared", verb="ask", by="admin")
    policy = RulesPolicy(rules=[ALLOW_ASK], roster=ROSTER, grants=store)
    store.close()
    with pytest.raises(GrantError):
        await policy.decide(_ask(policy))
