"""What Ricardi needs from anything that carries humans' messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class Inbound:
    """One message addressed to Ricardi, with the mention already stripped."""

    text: str
    # The conversation this belongs to. Slack's thread timestamp, or whatever
    # the transport uses to mean "the same back-and-forth".
    thread: str
    channel: str = ""
    user: str = ""
    # Whether this arrived inside a thread someone already has open, as opposed
    # to starting one. It decides where a refusal can actually be seen: Slack
    # renders an ephemeral message in a thread only to someone viewing that
    # thread, so a refusal to a message that just started one is invisible.
    in_thread: bool = False


OnMessage = Callable[[Inbound], Awaitable[None]]


@runtime_checkable
class Transport(Protocol):
    async def start(self, on_message: OnMessage) -> None: ...

    async def send(self, channel: str, thread: str, text: str) -> None: ...

    async def close(self) -> None: ...
