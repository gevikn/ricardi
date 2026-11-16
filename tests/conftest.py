"""Shared helpers.

Ricardi denies everything without a policy, which is the right default and
makes every test that isn't about policy need to say so explicitly. That is a
feature: a test passing an open policy is visibly choosing to.
"""

from ricardi.events import Event
from ricardi.policy import Rule, RulesPolicy
from ricardi.render import render

TESTER = "tester@example.com"


class RendersEvents:
    """The event half of a fake transport, for one that already has `send`.

    The same two steps both real transports take -- look the name's
    destination up, render, post what is not empty -- so a fake records what a
    person in the channel would actually have seen rather than what the app
    handed over.
    """

    def __init__(self) -> None:
        super().__init__()
        # Every event this transport was given, rendered or not.
        self.emitted: list[Event] = []
        self._routes: dict[str, tuple[str, str]] = {}

    def bind(self, turn: str, channel: str, thread: str) -> None:
        self._routes[turn] = (channel, thread)

    def unbind(self, turn: str) -> None:
        self._routes.pop(turn, None)

    async def emit(self, event: Event) -> None:
        self.emitted.append(event)
        # Indexed, not `.get`: an event with nowhere to go is a wiring bug,
        # exactly as it is in slack.py and local.py.
        channel, thread = self._routes[event.envelope.turn]
        text = render(event)
        if text:
            await self.send(channel, thread, text)


def open_policy(roster: dict[str, str] | None = None) -> RulesPolicy:
    """Permits everything, for tests about something other than policy."""
    return RulesPolicy(
        rules=[Rule(effect="allow", name="everything, in tests")],
        roster=roster or {"u1": TESTER, "tester": TESTER},
    )
