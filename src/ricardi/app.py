"""Ricardi herself: one turn, from a human's message to an agent's answer."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ricardi import router
from ricardi.backends.base import AgentBackend, BackendError, Caller
from ricardi.events import EventSink, Refused, Said, Stream, new_turn_id
from ricardi.grants import GrantError, GrantStore
from ricardi.health import CredentialWatch, classify, describe
from ricardi.policy import TIERS, ClosedPolicy, Decision, PolicyPoint, Request, Resource
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
        grants: GrantStore | None = None,
        health: CredentialWatch | None = None,
        sink: EventSink | None = None,
    ):
        self.transport = transport
        self.backend = backend
        self.bindings = bindings if bindings is not None else ThreadBindings(None)
        self.ack_after = ack_after
        # No policy means nothing is permitted, not everything.
        self.policy = policy or ClosedPolicy("none was configured")
        # Written to only after `decide` has allowed a grant or revoke, and read
        # only to see which tiers a revoke has to clear. What a person may do is
        # the policy's to say, never this store's.
        self.grants = grants
        # Watches the Claude credential, which fails independently of
        # Slack. None means nobody is watching it.
        self.health = health
        self._watching: asyncio.Task[None] | None = None
        # Where events go. The transport unless something else was named: it is
        # the thing with a channel to show them in, and everything Ricardi says
        # is an event now, so a sink that was not wired would be a mute bot.
        self.sink: EventSink = sink if sink is not None else transport
        self._streams: dict[str, Stream] = {}

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
        """Answer one message. Never raises: Slack gets words, not a traceback.

        One turn id is minted here and carried by every event this message
        causes -- the agent's, the gate's refusals, and Ricardi's own words
        about it. It is what a client ties them together by, and it is how a
        transport knows which thread they belong in: messages are answered
        concurrently, so an answer that found its way home by the agent's name
        would land in whichever thread spoke to that agent most recently.
        """
        turn = new_turn_id()
        try:
            await self._handle(inbound, turn)
        except BackendError as exc:
            await self._emit(inbound, "ricardi", Said(text=str(exc)), turn)
            # The person has their error. The channel has not been told what
            # it means, and only this path knows.
            await self._turn_failed(exc)
        except Exception:
            log.exception("turn failed for thread %s", inbound.thread)
            await self._emit(
                inbound, "ricardi",
                Said(text="Something broke on my side. The box's log has the detail."),
                turn,
            )
        finally:
            # Everything that could emit for this message has been awaited by
            # now, so the route has no further use. Left behind, it would be a
            # table that grows by one entry per message, forever.
            self.transport.unbind(turn)

    async def _handle(self, inbound: Inbound, turn: str) -> None:
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
            await self._emit(inbound, "ricardi", Said(text=router.HELP + extra), turn)
            return

        if command.kind == "bad_flag":
            # Refused rather than ignored: every flag takes something away, so
            # a dropped one runs what the person was trying to prevent.
            known = ", ".join(f"`-{f}`" for f in sorted(router.FLAGS))
            await self._refuse(
                inbound,
                f"I don't know the flag `-{command.text}`, and I won't guess "
                f"at a restriction. I understand {known}.",
                turn,
            )
            return

        if command.kind == "list":
            # The catalogue names the agents and what they can touch, so it is
            # worth gating rather than handing to anyone who asks.
            if not (await self._permit(inbound, "list", turn=turn))[0]:
                return
            await self._emit(inbound, "ricardi", Said(text=await self._agent_list()), turn)
            return

        if command.kind == "who":
            bound = self.bindings.get(inbound.thread)
            await self._emit(
                inbound, "ricardi",
                Said(text=f"This thread is talking to *{bound}*."
                     if bound
                     else "This thread is not talking to anyone yet. Try `list agents`."),
                turn,
            )
            return

        if command.kind in {"bad_grant", "bad_revoke"}:
            shape = router.GRANT_SHAPE if command.kind == "bad_grant" else router.REVOKE_SHAPE
            await self._emit(
                inbound, "ricardi",
                Said(text=f"I couldn't read that. The shape is {shape}, with the "
                          "person picked from Slack's mention list."),
                turn,
            )
            return

        if command.kind in {"grant", "revoke"}:
            await self._confer(inbound, command, turn)
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
            if not (await self._permit(inbound, "ask", agent, turn=turn))[0]:
                return
            self.bindings.bind(inbound.thread, agent, command.flags)
            if not text:
                await self._emit(
                    inbound, "ricardi",
                    Said(text=f"This thread is talking to *{agent}* now. "
                              "Say what you need."),
                    turn,
                )
                return
            await self._relay(inbound, agent, text, turn)
            return

        # A plain message continues whatever this thread was already doing,
        # or -- when there is only one agent to continue with -- starts it.
        bound = self.bindings.get(inbound.thread) or await self._only_agent()
        if not bound:
            await self._emit(
                inbound, "ricardi",
                Said(text="I don't know who that's for. Name an agent with "
                          "`tell <agent> <message>`.\n\n" + await self._agent_list()),
                turn,
            )
            return
        if not (await self._permit(inbound, "ask", bound, turn=turn))[0]:
            return
        # A reply may narrow the thread further; it can never widen it.
        self.bindings.bind(inbound.thread, bound, command.flags)
        await self._relay(inbound, bound, command.text, turn)

    async def _confer(self, inbound: Inbound, command: router.Command, turn: str) -> None:
        """grant / revoke: ask `decide` first, write only if it allows.

        Nothing is decided here. The tier written is the one the command named,
        and it is the request's `agent` that lets `decide` look up the
        grantor's own tier on this name.
        """
        action = command.kind
        person = self._email_for(command.user)
        allowed, _ = await self._permit(
            inbound, action, command.agent, turn=turn, target=person,
            confers=command.tier
        )
        if not allowed:
            return
        by = self._principal(inbound).email
        if self.grants is None:
            await self._emit(
                inbound, "ricardi",
                Said(text=f"I couldn't record that {action}: no grant store is open."),
                turn,
            )
            return
        try:
            name = await self._resolve(command.agent)
        except BackendError:
            # Allowed, but there is nothing by that name to record against:
            # say so, so the channel does not read this as done.
            await self._emit(
                inbound, "ricardi",
                Said(text="Nothing was recorded: I have no agent called "
                          f"*{command.agent}*."),
                turn,
            )
            return
        try:
            if action == "grant":
                self.grants.add(
                    person=person, name=name, verb=command.tier, by=by, granted=True
                )
                await self._emit(
                    inbound, "ricardi",
                    Said(text=f"<@{command.user}> now holds *{command.tier}* on "
                              f"*{name}*. That records the tier; it does not add "
                              "anyone to the channel."),
                    turn,
                )
                return
            # Presence (`ask`) is left alone on purpose: removing someone from
            # the channel is a channel action, and a delegate must not be able
            # to strip a peer's ability to be addressed. Highest first, so a
            # failure part-way leaves the lesser tier standing, not the greater.
            held = [t for t in reversed(TIERS) if self.grants.holds(person, name, t)]
            for tier in held:
                self.grants.add(
                    person=person, name=name, verb=tier, by=by, granted=False
                )
        except GrantError:
            log.exception("%s by %s failed", action, by)
            await self._emit(
                inbound, "ricardi",
                Said(text=f"I couldn't record that {action}, so nothing should be "
                          "assumed changed. The box's log has the detail."),
                turn,
            )
            return
        if not held:
            await self._emit(
                inbound, "ricardi",
                Said(text=f"<@{command.user}> holds no tier on *{name}*, so there "
                          "is nothing to take back."),
                turn,
            )
            return
        await self._emit(
            inbound, "ricardi",
            Said(text=f"<@{command.user}> no longer holds "
                      f"{' or '.join(held)} on *{name}*."),
            turn,
        )

    def _email_for(self, slack_id: str) -> str:
        """The roster's email for a Slack id, or "" for a stranger."""
        return self._principal(Inbound(text="", thread="", channel="", user=slack_id)).email

    async def _turn_failed(self, exc: BackendError) -> None:
        """Let the credential watch count a turn that failed its way.

        Gated on recognising the error, and that gate is load-bearing: an
        unknown agent and an agent that ran out of turns are `BackendError`
        too, and counting either would announce a dead credential because
        somebody mistyped a name. The probe is the opposite -- it alerts on
        anything it does not recognise, because nothing else was asking -- so
        the unrecognised case is left to it.

        Classified on the cause rather than on this exception's own text: the
        backend wraps a failure as "*name* failed: Command failed with exit
        code 1" and the reason lives in the original error's stderr, which
        `describe` reads and this message does not carry.
        """
        if self.health is None:
            return
        cause = exc.__cause__ or exc
        if classify(describe(cause)) not in {"auth", "billing"}:
            return
        await self.health.saw_failure(cause)

    def _stream_for(self, name: str) -> Stream:
        """The stream for one name, made on first use and kept.

        One per name, never one for the app: a box serves several agents, and a
        sequence shared across them would let one project's watcher count
        another's events. Kept rather than made per turn, so `seq` runs on
        across every turn in that name.
        """
        if name not in self._streams:
            self._streams[name] = Stream(name, self.sink)
        return self._streams[name]

    def _route(self, inbound: Inbound, name: str, turn: str) -> Stream:
        """The stream `name`'s events go on, having said where `turn` comes out.

        Two questions, answered in different places on purpose. *Which stream*
        an event belongs to is the name: one sequence per agent, so a client
        watching one project cannot count another's events. *Where it comes
        out* is the turn, because an event carries no channel -- a browser
        client has no use for one -- and a destination kept per name is shared
        by every thread talking to that agent, which is how one conversation's
        answer ends up in another's thread.
        """
        self.transport.bind(turn, inbound.channel, inbound.thread)
        return self._stream_for(name)

    async def _emit(self, inbound: Inbound, actor: str, body: object,
                    turn: str) -> None:
        """Say something, as an event rather than as a string.

        This carries what *Ricardi* says -- help, a binding, a refusal's
        fallback, an error -- never what an agent said: the agent's own `Said`
        events are the record of that, and a second one here would post every
        reply twice.

        Filed under whichever agent the thread is talking to, since her words
        belong to that conversation, or under no name at all when it is talking
        to nobody yet. `turn` is this message's, so a word about a turn is
        posted where the turn is and a client can tie the two together.
        """
        stream = self._route(inbound, self.bindings.get(inbound.thread), turn)
        await stream.emit(actor, body, turn=turn)

    async def _permit(
        self,
        inbound: Inbound,
        action: str,
        agent: str = "",
        *,
        turn: str,
        target: str = "",
        confers: str = "",
    ) -> tuple[bool, Decision]:
        """The inbound enforcement point. Every path to an agent passes here.

        The Decision comes back with the verdict so the refusal's rule and tier
        can reach the stream. Callers must read the first element: the pair
        itself is always truthy.
        """
        request = Request(
            action=action,
            principal=self._principal(inbound),
            resource=Resource(channel_id=inbound.channel),
            agent=agent,
            text=inbound.text,
            target=target,
            confers=confers,
        )
        try:
            decision = await self.policy.decide(request)
        except Exception:
            # Fail closed, and say so rather than going quiet.
            log.exception("policy failed for %s in %s", inbound.user, inbound.channel)
            reason = "I couldn't check whether you're allowed."
            denied = Decision(allow=False, reason=reason, rule="policy error")
            await self._refused(inbound, agent, denied, target, turn)
            return False, denied
        if not decision.allow:
            await self._refused(inbound, agent, decision, target, turn)
            return False, decision
        return True, decision

    async def _refused(
        self, inbound: Inbound, agent: str, decision: Decision, target: str, turn: str
    ) -> None:
        reason = decision.reason or "That isn't permitted."
        # The stream gets the rule and the tier; the person gets the reason
        # alone. Who may see the rule is a renderer's decision, not this one's.
        # An unmatched deny reads "default deny", the same words audit.py logs.
        # On the agent the request named, not on the thread's binding: a
        # refused `tell` must not bind, so there is nothing to read back.
        stream = self._route(inbound, agent, turn)
        await stream.emit(
            "ricardi",
            Refused(rule=decision.rule or "default deny", reason=reason, tier=decision.tier,
                    tool="", target=target),
            turn=turn,
        )
        await self._refuse(inbound, reason, turn)

    def _principal(self, inbound: Inbound):
        resolve = getattr(self.policy, "principal", None)
        if resolve is None:
            from ricardi.policy import Principal

            return Principal(id=inbound.user)
        return resolve(inbound.user)

    async def _refuse(self, inbound: Inbound, text: str, turn: str) -> None:
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
        await self._emit(inbound, "ricardi", Said(text=text), turn)

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

    async def _relay(self, inbound: Inbound, agent: str, text: str, turn: str) -> None:
        # Routed before the turn starts rather than at the first thing Ricardi
        # says: everything inside the turn is emitted by the backend.
        stream = self._route(inbound, agent, turn)
        caller = Caller(
            principal=self._principal(inbound),
            resource=Resource(channel_id=inbound.channel),
            events=stream,
            turn=turn,
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
                await self._emit(
                    inbound, "ricardi",
                    Said(text=f"Passed it to *{agent}*. Still working."),
                    turn,
                )
            await task
        finally:
            # If telling the channel failed, or this turn was cancelled, the
            # agent would otherwise keep working with nobody waiting and its
            # result would surface as an unretrieved task exception.
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        # Nothing is said here about the answer: the turn's own `Said` events
        # already carried it, block by block, as the agent produced them.
        if self.health is not None:
            # An answered turn proves the credential better than any probe,
            # and tells the channel if it had been told otherwise.
            await self.health.saw_success()

    async def _agent_list(self) -> str:
        agents = await self.backend.list_agents()
        if not agents:
            return "I can't reach any agents right now."
        lines = [f"I can reach {len(agents)} agent{'' if len(agents) == 1 else 's'}:"]
        for agent in agents:
            detail = f" - {agent.description}" if agent.description else ""
            lines.append(f"- *{agent.name}*{detail} ({agent.status})")
        return "\n".join(lines)
