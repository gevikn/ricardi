"""How often one person may set an agent working."""

import pytest

from ricardi.limits import Limits, RateLimited
from ricardi.policy import Decision, Principal, Request, Resource, Rule, RulesPolicy


class Clock:
    """A hand-wound clock, so the tests do not wait for real minutes."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, seconds):
        self.t += seconds


def open_rules():
    return RulesPolicy([Rule(effect="allow", name="everything")], roster={"U1": "a@b.c"})


def ask(who="U1", action="ask"):
    return Request(
        action=action,
        principal=Principal(id=who, email=who.lower() + "@example.com", known=True),
        resource=Resource(channel_id="C1"),
        agent="scout",
    )


def limited(per_minute=0, per_hour=0, clock=None, **plane2):
    return RateLimited(
        open_rules(), Limits(per_minute, per_hour, **plane2), now=clock or Clock()
    )


class AlwaysAllows:
    """An inner that permits everything, to isolate the counting.

    `open_rules()` cannot stand in for this on a plane-2 verb: a `grant`
    reaches the conferral authority check before any ceiling, and with no
    owner and no target it is refused there. A limit test built on that would
    pass because the rules said no, never exercising the count -- which is how
    a dead guard looks exactly like a live one.
    """

    async def decide(self, request):
        return Decision(allow=True, reason="", rule="stub allow")


def counting(clock=None, **limits):
    return RateLimited(AlwaysAllows(), Limits(**limits), now=clock or Clock())


async def test_without_limits_nothing_is_counted():
    policy = limited()
    for _ in range(50):
        assert (await policy.decide(ask())).allow is True


async def test_a_burst_is_refused_once_the_minute_is_full():
    clock = Clock()
    policy = limited(per_minute=3, clock=clock)
    for _ in range(3):
        assert (await policy.decide(ask())).allow is True
    refused = await policy.decide(ask())
    assert refused.allow is False
    assert refused.rule == "rate limit"
    assert "minute" in refused.reason


async def test_the_refusal_says_roughly_when_to_try_again():
    clock = Clock()
    policy = limited(per_minute=2, clock=clock)
    await policy.decide(ask())
    await policy.decide(ask())
    refused = await policy.decide(ask())
    assert "seconds" in refused.reason or "minutes" in refused.reason


async def test_the_window_slides_so_waiting_works():
    clock = Clock()
    policy = limited(per_minute=2, clock=clock)
    await policy.decide(ask())
    await policy.decide(ask())
    assert (await policy.decide(ask())).allow is False
    clock.tick(61)
    assert (await policy.decide(ask())).allow is True


async def test_the_hourly_ceiling_holds_past_the_minute():
    clock = Clock()
    policy = limited(per_hour=5, clock=clock)
    for _ in range(5):
        clock.tick(120)  # well clear of any per-minute window
        assert (await policy.decide(ask())).allow is True
    clock.tick(120)
    refused = await policy.decide(ask())
    assert refused.allow is False
    assert "hour" in refused.reason


async def test_an_hour_later_the_count_has_cleared():
    clock = Clock()
    policy = limited(per_hour=2, clock=clock)
    await policy.decide(ask())
    clock.tick(10)
    await policy.decide(ask())
    assert (await policy.decide(ask())).allow is False
    clock.tick(3601)
    assert (await policy.decide(ask())).allow is True


async def test_people_are_counted_separately():
    clock = Clock()
    policy = limited(per_minute=1, clock=clock)
    assert (await policy.decide(ask("U1"))).allow is True
    assert (await policy.decide(ask("U1"))).allow is False
    # Someone else's accident is not yours.
    assert (await policy.decide(ask("U2"))).allow is True


async def test_tool_calls_inside_a_turn_do_not_count():
    """A turn already permitted may use many tools; that is one turn."""
    clock = Clock()
    policy = limited(per_minute=1, clock=clock)
    assert (await policy.decide(ask())).allow is True
    for _ in range(20):
        assert (await policy.decide(ask(action="tool"))).allow is True


async def test_listing_the_agents_does_not_count():
    clock = Clock()
    policy = limited(per_minute=1, clock=clock)
    for _ in range(10):
        assert (await policy.decide(ask(action="list"))).allow is True
    assert (await policy.decide(ask())).allow is True


async def test_a_refused_request_does_not_use_up_an_allowance():
    """Being denied by the rules should not also cost you a turn."""
    clock = Clock()
    closed = RulesPolicy([Rule(effect="deny", name="no")], roster={"U1": "a@b.c"})
    policy = RateLimited(closed, Limits(per_minute=1), now=clock)
    for _ in range(5):
        assert (await policy.decide(ask())).allow is False
    # The rules still refuse, but not for being over a limit.
    assert (await policy.decide(ask())).rule != "rate limit"


async def test_the_roster_and_rules_still_show_through():
    policy = limited(per_minute=1)
    assert policy.principal("U1").email == "a@b.c"
    assert policy.principal("U-nobody").known is False
    assert len(policy.rules) == 1
    assert policy.roster == {"U1": "a@b.c"}


# -- changing access is also traffic --------------------------------------
# `grant` and `revoke` append to a store nothing prunes, so a client in a
# retry loop writes unbounded rows into the one file here that is deliberately
# never truncated. These bound that. See issue #55.
async def test_a_grant_loop_is_refused():
    clock = Clock()
    policy = counting(grants_per_minute=3, clock=clock)
    for _ in range(3):
        assert (await policy.decide(ask(action="grant"))).allow is True
    refused = await policy.decide(ask(action="grant"))
    assert refused.allow is False
    assert refused.rule == "rate limit"


async def test_every_plane2_verb_is_counted():
    """Derived from PLANE2_VERBS, so a new verb cannot skip the ceiling."""
    from ricardi.policy import PLANE2_VERBS

    for verb in sorted(PLANE2_VERBS):
        policy = counting(grants_per_minute=1, clock=Clock())
        assert (await policy.decide(ask(action=verb))).allow is True, verb
        second = await policy.decide(ask(action=verb))
        assert second.allow is False, verb
        assert second.rule == "rate limit", verb


async def test_grants_and_asks_do_not_share_an_allowance():
    """Changing access a few times must not cost you the turns you came for."""
    clock = Clock()
    policy = counting(per_minute=2, grants_per_minute=2, clock=clock)
    for _ in range(2):
        assert (await policy.decide(ask(action="grant"))).allow is True
    assert (await policy.decide(ask(action="grant"))).allow is False
    # The ask allowance is untouched by the grants above.
    for _ in range(2):
        assert (await policy.decide(ask())).allow is True


async def test_the_grant_refusal_does_not_talk_about_running_turns():
    clock = Clock()
    policy = counting(grants_per_minute=1, clock=clock)
    await policy.decide(ask(action="grant"))
    refused = await policy.decide(ask(action="grant"))
    assert "run" not in refused.reason
    assert "access" in refused.reason


async def test_plane2_is_bounded_without_any_configuration():
    """A guard that only fires once configured is the #54 shape of bug.

    The ask ceilings default to off deliberately -- an unlimited ask is
    bounded by money and by patience. An unlimited `grant` is bounded by
    neither, and writes to the file nothing truncates, so this one defaults on.
    """
    policy = counting(clock=Clock())
    rules = [
        (await policy.decide(ask(action="grant"))).rule for _ in range(200)
    ]
    assert "rate limit" in rules


async def test_an_explicit_zero_turns_the_grant_ceiling_off():
    clock = Clock()
    policy = counting(grants_per_minute=0, grants_per_hour=0, clock=clock)
    for _ in range(50):
        assert (await policy.decide(ask(action="grant"))).allow is True


# -- the declared form ----------------------------------------------------
def test_limits_load_from_the_policy_file(tmp_path):
    from ricardi import policy as P

    path = tmp_path / "policy.toml"
    path.write_text(
        "[limits]\nturns_per_minute = 3\nturns_per_hour = 30\n\n"
        '[[rule]]\neffect = "allow"\n',
        encoding="utf-8",
    )
    loaded = P.load_limits(path)
    # Every key names a Limits field, so the caller can splat it.
    assert Limits(**loaded).per_minute == 3
    assert Limits(**loaded).per_hour == 30


def test_the_grant_ceilings_are_declarable(tmp_path):
    from ricardi import policy as P

    path = tmp_path / "policy.toml"
    path.write_text(
        "[limits]\ngrants_per_minute = 2\ngrants_per_hour = 9\n\n"
        '[[rule]]\neffect = "allow"\n',
        encoding="utf-8",
    )
    loaded = P.load_limits(path)
    assert Limits(**loaded).grants_per_minute == 2
    assert Limits(**loaded).grants_per_hour == 9


def test_an_unset_grant_ceiling_keeps_the_default(tmp_path):
    from ricardi import policy as P

    path = tmp_path / "policy.toml"
    path.write_text(
        "[limits]\nturns_per_minute = 3\n\n[[rule]]\neffect = \"allow\"\n",
        encoding="utf-8",
    )
    loaded = P.load_limits(path)
    # Absent, so the dataclass default stands -- not read as "no ceiling".
    assert "grants_per_minute" not in loaded
    assert Limits(**loaded).grants_per_minute == Limits().grants_per_minute
    assert Limits(**loaded).grants_per_hour == Limits().grants_per_hour
    assert Limits(**loaded).grants_per_minute > 0


def test_a_misspelled_limits_key_is_still_refused(tmp_path):
    """The #57 guard must learn the new keys without losing the old refusal."""
    from ricardi import policy as P

    path = tmp_path / "policy.toml"
    path.write_text(
        "[limits]\ngrants_per_minut = 2\n\n[[rule]]\neffect = \"allow\"\n",
        encoding="utf-8",
    )
    with pytest.raises(P.PolicyError, match="unknown key"):
        P.load_limits(path)


def test_no_limits_table_means_no_ceiling(tmp_path):
    from ricardi import policy as P

    path = tmp_path / "policy.toml"
    path.write_text('[[rule]]\neffect = "allow"\n', encoding="utf-8")
    loaded = P.load_limits(path)
    assert loaded == {}
    assert Limits(**loaded).any is False


def test_a_negative_limit_is_refused(tmp_path):
    from ricardi import policy as P

    path = tmp_path / "policy.toml"
    path.write_text(
        "[limits]\nturns_per_hour = -1\n\n[[rule]]\neffect = \"allow\"\n",
        encoding="utf-8",
    )
    with pytest.raises(P.PolicyError, match="negative"):
        P.load_limits(path)


def test_the_shipped_example_sets_a_ceiling():
    from ricardi import policy as P

    loaded = Limits(**P.load_limits("policy.example.toml"))
    assert loaded.per_minute > 0 and loaded.per_hour > 0
    assert loaded.grants_per_minute > 0 and loaded.grants_per_hour > 0
