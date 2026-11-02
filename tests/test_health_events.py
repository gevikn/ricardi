"""A real turn is a credential check, and it is the cheapest one there is.

The watch's probe costs a fraction of a cent an hour and answers up to an hour
late. Every question anyone asks answers the same thing for free, the moment it
happens. These tests pin which failures are allowed to count -- because a
`BackendError` is also what a typo in an agent name produces, and a monitor
that fires on a typo is a monitor somebody mutes.
"""

import pytest

from conftest import open_policy

from ricardi.app import Ricardi
from ricardi.backends.base import Agent, BackendError
from ricardi.health import CredentialWatch
from ricardi.store import ThreadBindings
from ricardi.transports.base import Inbound

EXPIRED = "API Error: 401 Unauthorized: OAuth token has expired"


class Recorder:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def start(self, on_message):
        self.on_message = on_message

    async def send(self, channel, thread, text):
        self.sent.append((channel, thread, text))

    async def close(self):
        return None

    @property
    def last(self) -> str:
        return self.sent[-1][2] if self.sent else ""


class Backend:
    """Two agents, one of which fails in whatever way the test chose.

    Two rather than one on purpose: with a single agent in the catalogue an
    unrecognised name is read as part of the sentence rather than as
    addressing, so the unknown-agent error -- the one that must not count --
    only exists when there is a choice.
    """

    def __init__(self, error: BaseException | None = None):
        self.error = error

    async def list_agents(self):
        return [
            Agent(name="ricardi", description="this repository"),
            Agent(name="utils", description="the scripts repo"),
        ]

    async def ask(self, name, text, caller=None):
        if name != "ricardi":
            raise BackendError(f"I have no agent called *{name}*.")
        if self.error is not None:
            raise self.error
        return "ok"

    async def close(self):
        return None


class Posted:
    def __init__(self):
        self.texts: list[str] = []

    async def __call__(self, text: str) -> None:
        self.texts.append(text)


class Probe:
    """Never used here. A probe call in these tests would be a bug."""

    def __init__(self):
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


def message(text, thread="t1"):
    return Inbound(text=text, thread=thread, channel="c1", user="u1")


@pytest.fixture
def wired():
    """An app whose single agent fails however the test says, plus a watch."""

    def build(error=None, streak=1):
        transport = Recorder()
        posted = Posted()
        probe = Probe()
        watch = CredentialWatch(probe=probe, announce=posted, streak=streak)
        app = Ricardi(
            transport=transport,
            backend=Backend(error),
            bindings=ThreadBindings(None),
            ack_after=30.0,
            policy=open_policy(),
            health=watch,
        )
        return app, transport, watch, posted, probe

    return build


async def test_an_expired_credential_in_a_real_turn_tells_the_channel(wired):
    app, transport, watch, posted, probe = wired(BackendError(f"*ricardi* failed: {EXPIRED}"))
    await app.handle(message("tell ricardi hello"))
    # The asker still gets their error, in their thread, first.
    assert "OAuth token has expired" in transport.last
    # And the channel gets the diagnosis the asker's error does not carry.
    assert len(posted.texts) == 1
    assert "setup-token" in posted.texts[0]
    assert watch.announced
    assert probe.calls == 0


async def test_the_reason_hidden_in_stderr_still_counts(wired):
    # The SDK's wrapper is terse -- "Command failed with exit code 1" -- and
    # the reason is in stderr. Classifying only the wrapper would miss every
    # real auth failure, which is the whole point of this path.
    cause = RuntimeError("Command failed with exit code 1")
    cause.stderr = EXPIRED
    app, transport, watch, posted, _ = wired(
        BackendError("*ricardi* failed: Command failed with exit code 1")
    )
    app.backend.error.__cause__ = cause
    await app.handle(message("tell ricardi hello"))
    assert watch.announced
    assert "setup-token" in posted.texts[0]


async def test_an_unknown_agent_is_not_a_credential_failure(wired):
    app, transport, watch, posted, _ = wired()
    await app.handle(message("tell nobody hello"))
    assert "no agent called" in transport.last
    assert posted.texts == []
    assert watch.failures == 0


async def test_an_agent_that_stopped_early_is_not_a_credential_failure(wired):
    # A turn that ran out of turns says nothing about the credential.
    app, transport, watch, posted, _ = wired(
        BackendError("*ricardi* stopped early (it ran out of turns).")
    )
    await app.handle(message("tell ricardi hello"))
    assert posted.texts == []
    assert watch.failures == 0


async def test_an_answered_turn_counts_as_a_check(wired):
    app, transport, watch, posted, _ = wired()
    await app.handle(message("tell ricardi hello"))
    assert watch.failures == 0
    # Recorded, so the timer can skip its next probe.
    assert watch.last_ok is not None


async def test_an_answer_after_an_alert_announces_the_recovery(wired):
    app, transport, watch, posted, _ = wired()
    await watch.saw_failure(BackendError(EXPIRED))
    assert watch.announced
    await app.handle(message("tell ricardi hello"))
    assert not watch.announced
    assert "working again" in posted.texts[-1]


async def test_no_watch_configured_changes_nothing(wired):
    transport = Recorder()
    app = Ricardi(
        transport=transport,
        backend=Backend(BackendError(f"*ricardi* failed: {EXPIRED}")),
        bindings=ThreadBindings(None),
        ack_after=30.0,
        policy=open_policy(),
    )
    await app.handle(message("tell ricardi hello"))
    assert "OAuth token has expired" in transport.last


async def test_a_billing_failure_in_a_real_turn_counts(wired):
    # Nobody is coming to fix this by waiting, and the channel is where the
    # person who can top the account up is reading.
    app, transport, watch, posted, _ = wired(
        BackendError("*ricardi* failed: Your credit balance is too low")
    )
    await app.handle(message("tell ricardi hello"))
    assert watch.announced
    assert "top up" in posted.texts[0]


async def test_a_rate_limited_turn_is_left_to_the_probe(wired):
    # A usage limit hit by real traffic is self-inflicted and transient, and
    # it is the asker who is in the best position to know that. If it is in
    # fact sustained, no turn succeeds, the window goes stale and the probe
    # reports it on the ordinary interval.
    app, transport, watch, posted, _ = wired(
        BackendError("*ricardi* failed: 429 rate limit exceeded")
    )
    await app.handle(message("tell ricardi hello"))
    assert posted.texts == []
    assert watch.failures == 0
