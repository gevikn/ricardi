import pytest

from ricardi.events import Envelope, Event, Said, Thought
from ricardi.transports.base import Transport
from ricardi.transports.local import LocalTransport


def event(body, name="ricardi"):
    return Event(Envelope(1, "2026-11-10T15:04:05Z", name, "t1", "agent"), body)


async def test_an_event_appends_its_rendered_line_in_the_replies_shape():
    transport = LocalTransport()
    transport.bind("t1", "local", "th1")
    await transport.emit(event(Said(text="hello")))
    assert transport.replies == [
        {"channel": "local", "thread": "th1", "text": "*ricardi*\nhello"}]


async def test_an_event_rendering_to_nothing_appends_nothing():
    transport = LocalTransport()
    transport.bind("t1", "local", "th1")
    await transport.emit(event(Thought(text="hmm")))
    assert transport.replies == []


async def test_an_event_for_a_turn_with_nowhere_to_go_is_an_error_not_a_silent_drop():
    with pytest.raises(LookupError):
        await LocalTransport().emit(event(Said(text="hello")))


async def test_an_unbound_turn_has_nowhere_to_go_again():
    # The route table is one entry per message, so it has to be cleared.
    transport = LocalTransport()
    transport.bind("t1", "local", "th1")
    transport.unbind("t1")
    with pytest.raises(LookupError):
        await transport.emit(event(Said(text="hello")))


def test_the_local_transport_is_a_transport():
    assert isinstance(LocalTransport(), Transport)
