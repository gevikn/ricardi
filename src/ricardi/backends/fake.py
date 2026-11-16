"""An agent backend with no Claude behind it.

This exists so the whole path -- transport, router, bindings, replies -- can be
tested for correctness without spending tokens or needing the Agent SDK
installed. The live backend is covered separately, and sparingly.
"""

from __future__ import annotations

import asyncio

from ricardi.backends.base import Agent, AgentBackend, BackendError, Caller
from ricardi.events import Said, TurnEnded, TurnStarted, new_turn_id


class FakeBackend(AgentBackend):
    def __init__(self, agents: list[Agent] | None = None, delay: float = 0.0):
        self._agents = agents or [
            Agent(name="scout", description="reads code, never writes"),
            Agent(name="deployer", description="ships branches to stage"),
        ]
        self._delay = delay
        # Every (agent, text) this backend was asked, in order. Tests assert on
        # it to prove routing landed where it should.
        self.asked: list[tuple[str, str]] = []

    async def list_agents(self) -> list[Agent]:
        return list(self._agents)

    def _answer(self, name: str, text: str) -> str:
        """What this agent would have said. Overridden to make a quiet one."""
        return f"[{name}] {text}"

    async def ask(self, name: str, text: str, caller: Caller | None = None) -> str:
        known = {a.name for a in self._agents}
        if name not in known:
            raise BackendError(f"I have no agent called {name}.")
        self.asked.append((name, text))

        # The same shape a real turn has: it opens, it says what it has to say,
        # it closes. Without it this fake would be the one path through the
        # system where a turn reaches the channel as a string, which is
        # precisely what the tests built on it are there to check.
        stream = caller.events if caller else None
        turn = (caller.turn if caller else "") or new_turn_id()

        async def emit(actor: str, body: object) -> None:
            if stream is not None:
                await stream.emit(actor, body, turn=turn)

        await emit("person", TurnStarted(text=text))
        if self._delay:
            await asyncio.sleep(self._delay)
        answer = self._answer(name, text)
        # No `Said` for an agent that said nothing, as in the SDK backend: an
        # empty answer is how a renderer knows the turn was a quiet one.
        if answer:
            await emit("agent", Said(text=answer))
        await emit("agent", TurnEnded(outcome="done", answer=answer))
        return answer

    async def close(self) -> None:
        return None
