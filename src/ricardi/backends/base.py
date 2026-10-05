"""What Ricardi needs from anything that holds Claude Code agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, runtime_checkable

from ricardi.policy import Principal, Resource


@dataclass(frozen=True)
class Caller:
    """Who a turn is being run for, and how to tell them something mid-turn.

    Carried into the backend because a tool decision depends on who asked, not
    only on which agent is running. `notice` posts back into the conversation
    the request came from, which is how an announced command reaches the people
    who agreed to the limits.
    """

    principal: Principal = Principal()
    resource: Resource = Resource()
    notice: Callable[[str], Awaitable[None]] | None = None


@dataclass(frozen=True)
class Agent:
    """One addressable agent, as Slack sees it."""

    name: str
    description: str = ""
    cwd: str = ""
    # "ready" once the backend can take a message, "busy" mid-turn, "down" when
    # the session behind it has gone. Slack shows this in the agent list.
    status: str = "ready"


class BackendError(Exception):
    """The backend could not carry out the request.

    Raised for conditions a human should read in Slack: an unknown agent, a
    session that died, a backend that was never configured. The app turns these
    into a reply rather than a stack trace.
    """


@runtime_checkable
class AgentBackend(Protocol):
    """Holds the agents and carries one message to one of them.

    `ask` returns the agent's final answer. A turn can be long, so the caller is
    expected to tell the human it is working before awaiting.
    """

    async def list_agents(self) -> list[Agent]: ...

    async def ask(self, name: str, text: str, caller: Caller | None = None) -> str: ...

    async def close(self) -> None: ...
