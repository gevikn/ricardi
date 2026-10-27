"""With one agent in the catalogue, addressing it is ceremony.

Naming the agent is how the asker says which repository they mean. When the
catalogue holds exactly one, that question has a single answer and insisting
on it reads as a stutter -- agents are named after their repository, and on
the deployment that prompted this the repository shares the bot's name, so
every question began `@ricardi tell ricardi ...`.

So with one agent, anything that is not a command goes to it. With a choice,
the old insistence stands, because then the name carries information and
guessing would send work to the wrong checkout.
"""

import pytest

from conftest import TESTER, open_policy

from ricardi.app import Ricardi
from ricardi.backends.base import Agent
from ricardi.backends.fake import FakeBackend
from ricardi.policy import RulesPolicy
from ricardi.store import ThreadBindings
from ricardi.transports.base import Inbound


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


def message(text, thread="t1"):
    return Inbound(text=text, thread=thread, channel="c1", user="u1")


def wire(agents, policy=None):
    transport = Recorder()
    backend = FakeBackend(agents)
    bindings = ThreadBindings(None)
    app = Ricardi(
        transport=transport,
        backend=backend,
        bindings=bindings,
        ack_after=30.0,
        policy=policy if policy is not None else open_policy(),
    )
    return app, transport, backend, bindings


SOLO = [Agent(name="ricardi", description="this repository")]
TWO = [Agent(name="ricardi", description="this one"), Agent(name="utils", description="that one")]


async def test_a_plain_message_reaches_the_only_agent():
    app, _, backend, _ = wire(SOLO)
    await app.handle(message("what version are we on?"))
    assert backend.asked == [("ricardi", "what version are we on?")]


async def test_a_plain_message_still_needs_a_name_when_there_is_a_choice():
    # The whole point of the name is to pick a checkout. Two checkouts, two
    # answers, and guessing would run the work in the wrong one.
    app, transport, backend, _ = wire(TWO)
    await app.handle(message("what version are we on?"))
    assert backend.asked == []
    assert "Name an agent" in transport.last


async def test_a_name_that_is_not_the_agent_still_reaches_it_intact():
    # "tell me what version we are" parses as an agent called "me". With one
    # agent there is nowhere else it could have been going, and the message
    # arrives as typed rather than with the misread name chopped off it.
    app, _, backend, _ = wire(SOLO)
    await app.handle(message("tell me what version we are"))
    assert backend.asked == [("ricardi", "tell me what version we are")]


async def test_an_unknown_name_is_still_an_error_when_there_is_a_choice():
    app, transport, backend, _ = wire(TWO)
    await app.handle(message("tell nobody what version we are"))
    assert backend.asked == []
    assert "no agent called" in transport.last


async def test_the_only_agent_is_still_asked_of_policy():
    # The fallback picks who, never whether. A closed policy still refuses.
    closed = RulesPolicy(rules=[], roster={"u1": TESTER})
    app, transport, backend, _ = wire(SOLO, policy=closed)
    await app.handle(message("what version are we on?"))
    assert backend.asked == []
    assert transport.sent, "refused in silence, which looks like a bot that is down"


async def test_the_fallback_binds_the_thread():
    # Otherwise every reply in the thread would re-derive the agent, and the
    # moment a second repository is added the thread would change hands.
    app, _, _, bindings = wire(SOLO)
    await app.handle(message("what version are we on?"))
    assert bindings.get("t1") == "ricardi"


async def test_a_flag_still_applies_through_the_fallback():
    app, _, backend, bindings = wire(SOLO)
    await app.handle(message("-ro what version are we on?"))
    assert backend.asked == [("ricardi", "what version are we on?")]
    assert bindings.flags("t1") == ("ro",)


@pytest.mark.parametrize("text", ["list agents", "help", "who"])
async def test_the_commands_still_mean_themselves(text):
    # The fallback must not swallow the small command surface: "help" is help,
    # not a question for the agent.
    app, _, backend, _ = wire(SOLO)
    await app.handle(message(text))
    assert backend.asked == []


async def test_help_says_the_name_is_optional_when_it_is():
    # Help is where someone finds out what to type. Telling them to name an
    # agent, when there is one agent and naming it is optional, is the same
    # ceremony in the one place meant to remove it.
    app, transport, _, _ = wire(SOLO)
    await app.handle(message("help"))
    assert "ricardi" in transport.last
    assert "just ask" in transport.last.lower()


async def test_help_does_not_say_that_when_there_is_a_choice():
    app, transport, _, _ = wire(TWO)
    await app.handle(message("help"))
    assert "just ask" not in transport.last.lower()
