"""The asker's tier: read from the grant log per decision, put on the request.

Reporting only. `GrantStore.tier` judges nothing and `decide` fills a field;
no refusal and no allowance comes from this file's subject.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ricardi.grants import GrantError, GrantStore
from ricardi.policy import Request, Rule, RulesPolicy

ALLOW_ASK = Rule(effect="allow", name="asking", actions=("ask",))
ROSTER = {"U1": "a@b.com"}


def _store(tmp_path):
    return GrantStore.open(tmp_path / "g.db")


def test_the_tier_is_the_highest_verb_held(tmp_path):
    s = _store(tmp_path)
    s.add(person="a@b.com", name="n1", verb="read", by="owner")
    assert s.tier("a@b.com", "n1") == "read"
    s.add(person="a@b.com", name="n1", verb="write", by="owner")
    assert s.tier("a@b.com", "n1") == "write"


def test_a_revoked_write_falls_back_to_read_not_to_nothing(tmp_path):
    s = _store(tmp_path)
    s.add(person="a@b.com", name="n1", verb="read", by="owner")
    s.add(person="a@b.com", name="n1", verb="write", by="owner")
    s.add(person="a@b.com", name="n1", verb="write", by="owner", granted=False)
    assert s.tier("a@b.com", "n1") == "read"


def test_a_revoked_tier_with_nothing_beneath_is_no_tier(tmp_path):
    s = _store(tmp_path)
    s.add(person="a@b.com", name="n1", verb="read", by="owner")
    s.add(person="a@b.com", name="n1", verb="read", by="owner", granted=False)
    assert s.tier("a@b.com", "n1") == ""


def test_a_regranted_tier_is_back(tmp_path):
    s = _store(tmp_path)
    for granted in (True, False, True):
        s.add(person="a@b.com", name="n1", verb="write", by="owner", granted=granted)
    assert s.tier("a@b.com", "n1") == "write"


def test_no_rows_is_no_tier(tmp_path):
    assert _store(tmp_path).tier("a@b.com", "n1") == ""


def test_an_ask_grant_is_not_a_tier(tmp_path):
    s = _store(tmp_path)
    s.add(person="a@b.com", name="n1", verb="ask", by="owner")
    assert s.tier("a@b.com", "n1") == ""


def test_the_tier_is_per_name_and_per_person(tmp_path):
    s = _store(tmp_path)
    s.add(person="a@b.com", name="n1", verb="write", by="owner")
    assert s.tier("a@b.com", "n2") == ""
    assert s.tier("c@d.com", "n1") == ""


def test_the_tier_normalises_person_and_name(tmp_path):
    s = _store(tmp_path)
    s.add(person="a@b.com", name="n1", verb="write", by="owner")
    assert s.tier("  A@B.com ", " N1 ") == "write"


@dataclass(frozen=True)
class Recorder:
    """A rule that records what it was matched against and never matches."""

    seen: list
    effect: str = "allow"
    name: str = "recorder"

    def matches(self, request):
        self.seen.append(request)
        return False


def _policy(store, seen):
    p = RulesPolicy(rules=[ALLOW_ASK], roster=ROSTER, grants=store)
    p.rules = (Recorder(seen),) + p.rules
    return p


def _ask(policy, **kw):
    return Request(action="ask", principal=policy.principal("U1"), agent="n1", **kw)


async def test_decide_puts_the_askers_tier_on_the_request(tmp_path):
    store = _store(tmp_path)
    store.add(person="a@b.com", name="n1", verb="ask", by="o")
    store.add(person="a@b.com", name="n1", verb="write", by="o")
    seen = []
    policy = _policy(store, seen)
    await policy.decide(_ask(policy))
    assert seen[0].tier == "write"


async def test_decide_overwrites_a_tier_the_caller_supplied(tmp_path):
    # A caller-supplied tier is a privilege-escalation path: whatever arrives
    # from outside is discarded and replaced by what the store says.
    store = _store(tmp_path)
    store.add(person="a@b.com", name="n1", verb="ask", by="o")
    store.add(person="a@b.com", name="n1", verb="read", by="o")
    seen = []
    policy = _policy(store, seen)
    await policy.decide(_ask(policy, tier="write"))
    assert seen[0].tier == "read"


async def test_decide_discards_a_supplied_tier_when_the_store_says_none(tmp_path):
    store = _store(tmp_path)
    store.add(person="a@b.com", name="n1", verb="ask", by="o")
    seen = []
    policy = _policy(store, seen)
    await policy.decide(_ask(policy, tier="write"))
    assert seen[0].tier == ""


async def test_decide_discards_a_supplied_tier_with_no_store():
    seen = []
    policy = RulesPolicy(rules=[ALLOW_ASK], roster=ROSTER)
    policy.rules = (Recorder(seen),) + policy.rules
    await policy.decide(_ask(policy, tier="write"))
    assert seen[0].tier == ""


async def test_decide_discards_a_supplied_tier_for_an_unknown_principal(tmp_path):
    seen = []
    policy = _policy(_store(tmp_path), seen)
    await policy.decide(
        Request(action="ask", principal=policy.principal("UNKNOWN"), agent="n1", tier="write")
    )
    assert seen and seen[0].tier == ""


async def test_the_tier_is_read_per_decision_not_cached(tmp_path):
    store = _store(tmp_path)
    store.add(person="a@b.com", name="n1", verb="ask", by="o")
    store.add(person="a@b.com", name="n1", verb="write", by="o")
    seen = []
    policy = _policy(store, seen)
    await policy.decide(_ask(policy))
    store.add(person="a@b.com", name="n1", verb="write", by="o", granted=False)
    await policy.decide(_ask(policy))
    assert [r.tier for r in seen] == ["write", ""]


async def test_a_corrupt_store_raises_rather_than_reporting_no_tier(tmp_path):
    # A tool call, not an ask: the ask path already raises through `holds`, so
    # only the tier read can be what raises here.
    store = _store(tmp_path)
    policy = _policy(store, [])
    store.close()
    request = Request(action="tool", principal=policy.principal("U1"), agent="n1", tool="Read")
    with pytest.raises(GrantError):
        await policy.decide(request)
