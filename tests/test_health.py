"""The credential watch: what it says, when it says it, and what it holds back.

The point of this module is to turn a silent failure into a loud one, so the
tests that matter most are the ones about *when* it speaks. A monitor that
reports a single network blip gets muted, and a monitor that stays quiet
because it did not recognise an error is worse than none at all -- silence
from it reads as reassurance.
"""

import logging

import pytest

from ricardi import health
from ricardi.health import CredentialWatch, ProbeFailed, classify, describe, redact

EXPIRED = "API Error: 401 Unauthorized: OAuth token has expired"


class Probe:
    """A probe that fails on demand and counts how often it was asked."""

    def __init__(self, error: BaseException | None = None):
        self.error = error
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


class Posted:
    """Somewhere to announce to, which can also be made to fail."""

    def __init__(self, broken: bool = False):
        self.texts: list[str] = []
        self.broken = broken

    async def __call__(self, text: str) -> None:
        if self.broken:
            raise RuntimeError("not_in_channel")
        self.texts.append(text)


class Ticker:
    """A clock that returns the times it was given, in order."""

    def __init__(self, *values: float):
        self.values = list(values)
        self.last = 0.0

    def __call__(self) -> float:
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class Stop(Exception):
    """Breaks out of run(), which otherwise loops forever."""


class Clockwork:
    """Collects the delays run() asks for, then stops it."""

    def __init__(self, stop_after: int):
        self.delays: list[float] = []
        self._stop_after = stop_after

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self._stop_after:
            raise Stop()


def watch(probe, announce=None, **kw) -> CredentialWatch:
    kw.setdefault("streak", 3)
    return CredentialWatch(probe=probe, announce=announce, **kw)


# --- reading an error ----------------------------------------------------


def test_an_expired_token_is_recognised():
    assert classify(EXPIRED) == "auth"


@pytest.mark.parametrize(
    "text, kind",
    [
        ("Invalid bearer token", "auth"),
        ("authentication_error", "auth"),
        ("Invalid API key - please check", "auth"),
        ("Your credit balance is too low to run this", "billing"),
        ("429 too many requests", "limit"),
        ("rate limit exceeded, retry later", "limit"),
        ("connection reset by peer", ""),
        ("", ""),
    ],
)
def test_classify_sorts_the_kinds_it_knows(text, kind):
    assert classify(text) == kind


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-oat-AbCd1234567890",
        "sk-ant-api03-ZZZZ9999",
        "xoxb-111111-222222-abcdefg",
        "xapp-1-A012B-9999-deadbeef",
    ],
)
def test_credentials_are_masked(secret):
    # This text is posted into a channel, and an authentication error is
    # exactly the kind that quotes back what it was handed.
    out = redact(f"rejected: {secret} is no good")
    assert secret not in out
    assert "[redacted]" in out


def test_describe_keeps_the_stderr_where_the_reason_lives():
    class Process(Exception):
        stderr = "Invalid API key sk-ant-api03-LEAKME9999"

    text = describe(Process("exited with 1"))
    assert "Invalid API key" in text
    assert "LEAKME9999" not in text


def test_describe_truncates_a_wall_of_stderr():
    class Process(Exception):
        stderr = "x" * 5000

    assert len(describe(Process("boom"))) <= health.MAX_ERROR + 3


def test_a_failed_result_message_yields_the_right_advice():
    # The SDK can report a dead credential as a non-success result rather than
    # an exception, so that path has to reach the same conclusion.
    class Result:
        subtype = "error_during_execution"
        api_error_status = 401
        result = "OAuth token has expired"
        errors = None

    text = health._result_text(Result())
    assert "error_during_execution" in text
    assert classify(text) == "auth"


# --- when it speaks ------------------------------------------------------


async def test_a_healthy_probe_says_nothing():
    posted = Posted()
    w = watch(Probe(), posted)
    assert await w.check() is True
    assert posted.texts == []


async def test_a_healthy_check_leaves_a_heartbeat_in_the_log(caplog):
    # On a box nobody is sitting at, one line an hour is how you know it is
    # alive at all, and when it last authenticated.
    w = watch(Probe(), None)
    with caplog.at_level(logging.INFO, logger="ricardi.health"):
        await w.check()
    assert any("credential ok" in r.getMessage() for r in caplog.records)


async def test_one_failure_is_a_blip_and_stays_quiet():
    posted = Posted()
    w = watch(Probe(ProbeFailed(EXPIRED)), posted)
    assert await w.check() is False
    assert posted.texts == []
    assert w.failures == 1


async def test_it_announces_once_the_streak_is_reached():
    posted = Posted()
    w = watch(Probe(ProbeFailed(EXPIRED)), posted)
    for _ in range(3):
        await w.check()
    assert len(posted.texts) == 1
    assert w.announced


async def test_it_does_not_repeat_itself_every_hour():
    # The failure mode this guards against is the channel muting it.
    posted = Posted()
    w = watch(Probe(ProbeFailed(EXPIRED)), posted)
    for _ in range(10):
        await w.check()
    assert len(posted.texts) == 1


async def test_an_unrecognised_error_still_gets_announced():
    # The one that matters. Upstream wording changes, so classification picks
    # the advice and must never decide whether to speak at all.
    posted = Posted()
    probe = Probe(RuntimeError("something nobody has seen before"))
    w = watch(probe, posted, streak=1)
    await w.check()
    assert len(posted.texts) == 1
    assert "something nobody has seen before" in posted.texts[0]
    assert "do not recognise" in posted.texts[0].lower()


async def test_recovery_is_announced_with_how_long_it_was_down():
    posted = Posted()
    probe = Probe(ProbeFailed(EXPIRED))
    w = watch(probe, posted, streak=1, clock=Ticker(100.0, 7300.0))
    await w.check()
    probe.error = None
    assert await w.check() is True
    assert len(posted.texts) == 2
    assert "working again" in posted.texts[1]
    # 7300 - 100 = two hours of a soak nobody can trust.
    assert "2.0h" in posted.texts[1]
    assert not w.announced


async def test_a_recovery_before_the_streak_says_nothing_either_way():
    posted = Posted()
    probe = Probe(ProbeFailed(EXPIRED))
    w = watch(probe, posted)
    await w.check()
    probe.error = None
    await w.check()
    assert posted.texts == []
    assert w.failures == 0


# --- what it says --------------------------------------------------------


async def test_the_alert_never_carries_the_credential():
    posted = Posted()
    leak = "Invalid bearer token sk-ant-oat-SHOULDNOTAPPEAR1234"
    w = watch(Probe(ProbeFailed(leak)), posted, streak=1)
    await w.check()
    assert "SHOULDNOTAPPEAR1234" not in posted.texts[0]
    assert "[redacted]" in posted.texts[0]


async def test_the_auth_alert_says_how_to_fix_it_and_how_not_to():
    posted = Posted()
    w = watch(Probe(ProbeFailed(EXPIRED)), posted, streak=1)
    await w.check()
    text = posted.texts[0]
    assert "setup-token" in text
    # The trap the installer also refuses: a stored login shadows .env.
    assert "takes precedence" in text


async def test_a_rate_limit_is_not_reported_as_a_dead_credential():
    # Telling someone to re-issue a working credential because they hit a
    # usage limit sends them to do the one thing that cannot help.
    posted = Posted()
    w = watch(Probe(ProbeFailed("429 too many requests")), posted, streak=1)
    await w.check()
    text = posted.texts[0]
    assert "clear on its own" in text
    assert "setup-token" not in text
    assert "takes precedence" not in text


async def test_it_says_slack_is_not_the_problem():
    # Otherwise the first guess is always that the bot is down.
    posted = Posted()
    w = watch(Probe(ProbeFailed(EXPIRED)), posted, streak=1)
    await w.check()
    assert "Slack is fine" in posted.texts[0]


async def test_it_logs_even_with_nowhere_to_post(caplog):
    # A deployment with no alert_channel still has the log as its monitor.
    w = watch(Probe(ProbeFailed(EXPIRED)), None, streak=1)
    with caplog.at_level(logging.ERROR, logger="ricardi.health"):
        await w.check()
    assert any("can't reach Claude" in r.getMessage() for r in caplog.records)


async def test_a_broken_announce_does_not_stop_the_watching():
    # The likeliest cause is the bot not being in the channel it was given,
    # which must not take the monitor down with it.
    posted = Posted(broken=True)
    w = watch(Probe(ProbeFailed(EXPIRED)), posted, streak=1)
    assert await w.check() is False
    assert w.announced


# --- the loop ------------------------------------------------------------


async def test_run_checks_before_it_sleeps():
    # A box that came back up with a dead credential should not look healthy
    # for the first hour.
    probe = Probe()
    sleep = Clockwork(stop_after=1)
    w = watch(probe, None, every=3600.0, sleep=sleep)
    with pytest.raises(Stop):
        await w.run()
    assert probe.calls == 1
    assert sleep.delays == [3600.0]


async def test_it_retries_quickly_then_backs_off_once_it_has_told_you():
    probe = Probe(ProbeFailed(EXPIRED))
    sleep = Clockwork(stop_after=3)
    w = watch(probe, None, every=3600.0, retry=60.0, streak=2, sleep=sleep)
    with pytest.raises(Stop):
        await w.run()
    # Fast while the failure is unconfirmed; the ordinary interval afterwards,
    # which is also how recovery gets noticed.
    assert sleep.delays == [60.0, 3600.0, 3600.0]
