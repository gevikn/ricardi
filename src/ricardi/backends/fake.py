"""An agent backend with no Claude behind it.

This exists so the whole path -- transport, router, bindings, replies -- can be
tested for correctness without spending tokens or needing the Agent SDK
installed. The live backend is covered separately, and sparingly.
"""

from __future__ import annotations

import asyncio

from ricardi.backends.base import Agent, AgentBackend, BackendError, Caller


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

    async def ask(self, name: str, text: str, caller: Caller | None = None) -> str:
        known = {a.name for a in self._agents}
        if name not in known:
            raise BackendError(f"I have no agent called {name}.")
        self.asked.append((name, text))
        if self._delay:
            await asyncio.sleep(self._delay)
        return f"[{name}] {text}"

    async def close(self) -> None:
        return None
