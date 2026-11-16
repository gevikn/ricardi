import pytest

from ricardi.events import Envelope, Event, Said, Thought
from ricardi.transports.base import Transport
from ricardi.transports.slack import SlackTransport


class _Posting(SlackTransport):
    """Records what `send` was asked to post instead of calling Slack."""

    def __init__(self):
        super().__init__("xoxb-x", "xapp-x")
        self.posted: list[tuple[str, str, str]] = []

    async def send(self, channel, thread, text):
        self.posted.append((channel, thread, text))


def event(body, name="ricardi"):
    return Event(Envelope(1, "2026-11-10T15:04:05Z", name, "t1", "agent"), body)


async def test_the_slack_transport_posts_what_render_returns():
    transport = _Posting()
    transport.bind("ricardi", "C1", "1700.1")
    await transport.emit(event(Said(text="hello")))
    assert transport.posted == [("C1", "1700.1", "*ricardi*\nhello")]


async def test_an_event_rendering_to_nothing_posts_nothing():
    transport = _Posting()
    transport.bind("ricardi", "C1", "1700.1")
    await transport.emit(event(Thought(text="hmm")))
    assert transport.posted == []


async def test_an_event_for_a_name_with_nowhere_to_go_is_an_error_not_a_silent_drop():
    transport = _Posting()
    with pytest.raises(LookupError):
        await transport.emit(event(Said(text="hello")))


def test_the_slack_transport_is_a_transport():
    assert isinstance(SlackTransport("xoxb-x", "xapp-x"), Transport)
