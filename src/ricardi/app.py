"""Ricardi herself: one turn, from a human's message to an agent's answer."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ricardi import router
from ricardi.backends.base import AgentBackend, BackendError, Caller
from ricardi.health import CredentialWatch
from ricardi.policy import ClosedPolicy, PolicyPoint, Request, Resource
from ricardi.store import ThreadBindings
from ricardi.transports.base import Inbound, Transport

log = logging.getLogger("ricardi")


class Ricardi:
    def __init__(
        self,
        transport: Transport,
        backend: AgentBackend,
        bindings: ThreadBindings | None = None,
        ack_after: float = 4.0,
        policy: PolicyPoint | None = None,
        health: CredentialWatch | None = None,
    ):
        self.transport = transport
        self.backend = backend
        self.bindings = bindings if bindings is not None else ThreadBindings(None)
        self.ack_after = ack_after
        # No policy means nothing is permitted, not everything.
        self.policy = policy or ClosedPolicy("none was configured")
        # Watches the Claude credential, which fails independently of
        # Slack. None means nobody is watching it.
        self.health = health
        self._watching: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.transport.start(self.handle)
        # After the transport, not before: the first check runs straight
        # away and its alert has nowhere to go until Slack is connected.
        if self.health is not None:
            self._watching = asyncio.create_task(self.health.run())

    async def close(self) -> None:
        if self._watching is not None:
            # Stopped first, so it cannot try to post through a transport
            # that is already closing.
            self._watching.cancel()
            with contextlib.suppress(BaseException):
                await self._watching
            self._watching = None
        await self.transport.close()
        await self.backend.close()

    async def handle(self, inbound: Inbound) -> None:
        """Answer one message. Never raises: Slack gets words, not a traceback."""
        try:
            await self._handle(inbound)
        except BackendError as exc:
            await self._say(inbound, str(exc))
        except Exception:
            log.exception("turn failed for thread %s", inbound.thread)
            await self._say(
                inbound, "Something broke on my side. The box's log has the detail."
            )

    async def _handle(self, inbound: Inbound) -> None:
        command = router.parse(inbound.text)

        if command.kind in {"help", "empty"}:
            # Help is where someone learns what to type, so it is the one
            # place the optional name has to be said out loud.
            only = await self._only_agent()
            extra = (
                f"\n\nThere is only *{only}* to talk to, so you can "
                f"skip the name and just ask."
                if only
                else ""
            )
            await self._say(inbound, router.HELP + extra)
            return

        if command.kind == "bad_flag":
            # Refused rather than ignored: every flag takes something away, so
            # a dropped one runs what the person was trying to prevent.
            known = ", ".join(f"`-{f}`" for f in sorted(router.FLAGS))
            await self._refuse(
                inbound,
                f"I don't know the flag `-{command.text}`, and I won't guess "
                f"at a restriction. I understand {known}.",
            )
            return

        if command.kind == "list":
            # The catalogue names the agents and what they can touch, so it is
            # worth gating rather than handing to anyone who asks.
            if not await self._permit(inbound, "list"):
                return
            await self._say(inbound, await self._agent_list())
            return

        if command.kind == "who":
            bound = self.bindings.get(inbound.thread)
            await self._say(
                inbound,
                f"This thread is talking to *{bound}*."
                if bound
                else "This thread is not talking to anyone yet. Try `list agents`.",
            )
            return

        if command.kind == "tell":
            text = command.text
            try:
                agent = await self._resolve(command.agent)
            except BackendError:
                # With one agent there was nowhere else this could have been
                # going, so the name was part of the sentence rather than
                # addressing: "tell me what version we are". Send it as typed.
                agent = await self._only_agent()
                if not agent:
                    raise
                text = command.raw
            # Decided before binding: a refused request must not leave the
            # thread quietly pointed at an agent it may not use.
            if not await self._permit(inbound, "ask", agent):
                return
            self.bindings.bind(inbound.thread, agent, command.flags)
            if not text:
                await self._say(
                    inbound,
                    f"This thread is talking to *{agent}* now. Say what you need.",
                )
                return
            await self._relay(inbound, agent, text)
            return

        # A plain message continues whatever this thread was already doing,
        # or -- when there is only one agent to continue with -- starts it.
        bound = self.bindings.get(inbound.thread) or await self._only_agent()
        if not bound:
            await self._say(
                inbound,
                "I don't know who that's for. Name an agent with "
                "`tell <agent> <message>`.\n\n" + await self._agent_list(),
            )
            return
        if not await self._permit(inbound, "ask", bound):
            return
        # A reply may narrow the thread further; it can never widen it.
        self.bindings.bind(inbound.thread, bound, command.flags)
        await self._relay(inbound, bound, command.text)

    async def _permit(self, inbound: Inbound, action: str, agent: str = "") -> bool:
        """The inbound enforcement point. Every path to an agent passes here."""
        request = Request(
            action=action,
            principal=self._principal(inbound),
            resource=Resource(channel_id=inbound.channel),
            agent=agent,
            text=inbound.text,
        )
        try:
            decision = await self.policy.decide(request)
        except Exception:
            # Fail closed, and say so rather than going quiet.
            log.exception("policy failed for %s in %s", inbound.user, inbound.channel)
            await self._refuse(inbound, "I couldn't check whether you're allowed.")
            return False
        if not decision.allow:
            await self._refuse(inbound, decision.reason or "That isn't permitted.")
            return False
        return True

    def _principal(self, inbound: Inbound):
        resolve = getattr(self.policy, "principal", None)
        if resolve is None:
            from ricardi.policy import Principal

            return Principal(id=inbound.user)
        return resolve(inbound.user)

    async def _refuse(self, inbound: Inbound, text: str) -> None:
        """Tell one person no, without announcing it to the channel.

        A refusal to a message that just started a thread goes to the channel,
        not into that thread: Slack shows an ephemeral message only to someone
        already viewing the thread it was posted in, so the other way round it
        is delivered, correct, and invisible -- which looks exactly like a bot
        that is down, on the first message anyone ever sends.
        """
        private = getattr(self.transport, "send_private", None)
        if private is not None:
            where = inbound.thread if inbound.in_thread else ""
            await private(inbound.channel, where, inbound.user, text)
            return
        await self._say(inbound, text)

    async def _only_agent(self) -> str:
        """The agent there is no point naming, or "" when there is a choice.

        Naming an agent says which repository is meant. With one in the
        catalogue that question has a single answer, and asking it back is
        ceremony -- agents are named after their repository, so where the
        repository shares the bot's name it reads `@ricardi tell ricardi`.

        This picks who, never whether: every caller still goes through the
        policy check before anything reaches the agent.
        """
        agents = await self.backend.list_agents()
        return agents[0].name if len(agents) == 1 else ""

    async def _resolve(self, name: str) -> str:
        """Match an agent name the way a human typed it."""
        agents = await self.backend.list_agents()
        for agent in agents:
            if agent.name.lower() == name.lower():
                return agent.name
        known = ", ".join(a.name for a in agents) or "none"
        raise BackendError(f"I have no agent called *{name}*. I can reach: {known}.")

    async def _relay(self, inbound: Inbound, agent: str, text: str) -> None:
        async def notice(line: str) -> None:
            await self._say(inbound, f"_{agent}_ `{line}`")

        caller = Caller(
            principal=self._principal(inbound),
            resource=Resource(channel_id=inbound.channel),
            notice=notice,
            # From the thread, not from this message: a flag someone set on
            # the way in holds for whoever replies next.
            flags=self.bindings.flags(inbound.thread),
        )
        task = asyncio.create_task(self.backend.ask(agent, text, caller))
        try:
            # Quiet for a short turn, and honest about a long one. Slack reads
            # silence as a failure well before an agent considers itself slow.
            done, _ = await asyncio.wait({task}, timeout=self.ack_after)
            if not done:
                await self._say(inbound, f"Passed it to *{agent}*. Still working.")
            answer = await task
        finally:
            # If telling the channel failed, or this turn was cancelled, the
            # agent would otherwise keep working with nobody waiting and its
            # result would surface as an unretrieved task exception.
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        await self._say(inbound, f"*{agent}*\n{answer}".rstrip())

    async def _agent_list(self) -> str:
        agents = await self.backend.list_agents()
        if not agents:
            return "I can't reach any agents right now."
        lines = [f"I can reach {len(agents)} agent{'' if len(agents) == 1 else 's'}:"]
        for agent in agents:
            detail = f" - {agent.description}" if agent.description else ""
            lines.append(f"- *{agent.name}*{detail} ({agent.status})")
        return "\n".join(lines)

    async def _say(self, inbound: Inbound, text: str) -> None:
        await self.transport.send(inbound.channel, inbound.thread, text)
