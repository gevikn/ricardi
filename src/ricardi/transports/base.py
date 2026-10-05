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


OnMessage = Callable[[Inbound], Awaitable[None]]


@runtime_checkable
class Transport(Protocol):
    async def start(self, on_message: OnMessage) -> None: ...

    async def send(self, channel: str, thread: str, text: str) -> None: ...

    async def close(self) -> None: ...
