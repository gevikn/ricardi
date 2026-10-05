"""How often one person may set an agent working."""

import pytest

from ricardi.limits import Limits, RateLimited
from ricardi.policy import Principal, Request, Resource, Rule, RulesPolicy


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


def limited(per_minute=0, per_hour=0, clock=None):
    return RateLimited(open_rules(), Limits(per_minute, per_hour), now=clock or Clock())


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


# -- the declared form ----------------------------------------------------
def test_limits_load_from_the_policy_file(tmp_path):
    from ricardi import policy as P

    path = tmp_path / "policy.toml"
    path.write_text(
        "[limits]\nturns_per_minute = 3\nturns_per_hour = 30\n\n"
        '[[rule]]\neffect = "allow"\n',
        encoding="utf-8",
    )
    assert P.load_limits(path) == (3, 30)


def test_no_limits_table_means_no_ceiling(tmp_path):
    from ricardi import policy as P

    path = tmp_path / "policy.toml"
    path.write_text('[[rule]]\neffect = "allow"\n', encoding="utf-8")
    assert P.load_limits(path) == (0, 0)


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

    per_minute, per_hour = P.load_limits("policy.example.toml")
    assert per_minute > 0 and per_hour > 0
