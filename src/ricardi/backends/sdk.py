"""Agents that Ricardi owns, run through the Claude Agent SDK.

The SDK drives sessions it starts: each agent here is a `claude` process that
Ricardi spawned and holds open, so the answer comes straight back on the same
call. That is the whole reason this is the supported back end -- nothing about
it is undocumented, and no session belonging to anyone else is touched.

The consequence worth knowing: these are child processes. If Ricardi dies, they
die with her. The transcripts survive on disk, and `SessionIds` keeps each
agent's session id beside them, so a restart resumes the same conversation
rather than starting the agent over. Constructed without that store the ids
live only in memory, and a restart silently loses every agent's history.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ricardi.backends.base import Agent, AgentBackend, BackendError, Caller
from ricardi.config import AgentSpec
from ricardi.events import (
    Refused,
    Said,
    Stream,
    Thought,
    ToolFinished,
    TurnEnded,
    TurnStarted,
    new_turn_id,
    tool_output,
    tool_started,
)
from ricardi.policy import PolicyPoint, Request
from ricardi.store import SessionIds

log = logging.getLogger("ricardi.sdk")

# A turn that never ends would leave Slack silent forever. Better to say so.
DEFAULT_TURN_TIMEOUT = 600.0

# How long the PreToolUse gate may take. The same as the SDK's own default, so
# this changes no behaviour -- the point is that the deadline on the one check
# that enforces everything is now a number this project chose and a test holds,
# rather than one inherited from a dependency that could move. The in-process
# rules engine answers in microseconds; a decision point that makes a network
# call is what this bound is really for.
GATE_TIMEOUT = 60.0

# Result subtypes that mean a ceiling was reached rather than something broke.
_CUT_SHORT = {
    "error_max_budget_usd": "this turn reached its spending limit",
    "error_max_turns": "this turn reached its step limit",
}


def assemble(said: list[str]) -> str:
    """Everything the agent said this turn, as one answer.

    Every block, not just the last. An agent that works in steps often ends on
    "Done." with the substance several blocks earlier, so keeping only the
    closing text loses the answer. Verbosity is the lesser fault.

    Whole, and never cut: this is what `TurnEnded` carries, and a client with
    room for all of it should get all of it. A channel with a ceiling makes its
    own cut, in `render.py`. An agent that said nothing answers with "", which
    is what the wire contract calls an answer with none -- the sentence a
    person reads for one of those is a renderer's to choose.
    """
    return "\n\n".join(b.strip() for b in said if b and b.strip())


def _result_text(content: object) -> str:
    """What a tool returned, as the one string the stream carries.

    The SDK hands back either a string or the API's list of typed parts. Only
    the text parts have anything a reader can show: an image part would put a
    megabyte of base64 into an event nobody could render.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part["text"] for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


@dataclass
class _Live:
    spec: AgentSpec
    client: object | None = None
    # Claude Code's own id for the conversation, captured from each result so a
    # restart can resume it rather than start the agent over.
    session_id: str = ""
    busy: bool = False
    # What this agent has cost since Ricardi started, for the log.
    spent: float = 0.0
    # Who the current turn is for. The tool gate reads it, since a tool
    # decision depends on who asked as much as on which agent is running.
    caller: Caller | None = None
    # Why the last turn ended badly, if it did. Not a "down" state: the session
    # is dropped on a fault and the next message reconnects and resumes, so the
    # agent really is ready to take one.
    last_fault: str = ""
    # One turn at a time per agent: a session is a conversation, and two
    # overlapping prompts would interleave into nonsense.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SdkBackend(AgentBackend):
    def __init__(
        self,
        specs: tuple[AgentSpec, ...] | list[AgentSpec],
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
        policy: PolicyPoint | None = None,
        sessions: SessionIds | None = None,
    ):
        if not specs:
            raise ValueError("SdkBackend needs at least one agent")
        self._live = {spec.name: _Live(spec=spec) for spec in specs}
        self._turn_timeout = turn_timeout
        # Without a policy every tool is refused. An agent that can run anything
        # because nobody configured limits is the failure this exists to stop.
        self._policy = policy
        # Remembered across restarts, so an agent picks its conversation back up
        # instead of starting over with no history.
        self._sessions = sessions if sessions is not None else SessionIds(None)
        for name, live in self._live.items():
            live.session_id = self._sessions.get(name)

    async def list_agents(self) -> list[Agent]:
        out: list[Agent] = []
        for live in self._live.values():
            if live.busy:
                status = "busy"
            elif live.last_fault:
                status = f"ready, last turn {live.last_fault}"
            else:
                status = "ready"
            out.append(
                Agent(
                    name=live.spec.name,
                    description=live.spec.description,
                    cwd=live.spec.cwd,
                    status=status,
                )
            )
        return out

    async def ask(self, name: str, text: str, caller: Caller | None = None) -> str:
        live = self._live.get(name)
        if live is None:
            raise BackendError(f"I have no agent called *{name}*.")

        async with live.lock:
            live.busy = True
            live.caller = caller
            try:
                client = await self._connect(live)
                # The deadline is inside `_one_turn`, not around this await: a
                # turn has to be able to tell its own timeout from a cancel
                # somebody else asked for, and from out here the two look the
                # same. `TimeoutError` still arrives here, so nothing a caller
                # of `ask` can see has changed.
                return await self._one_turn(live, client, text)
            except asyncio.TimeoutError:
                # The session is still alive but we have stopped listening, so
                # it cannot be trusted to be at a turn boundary. Drop it; the
                # next message resumes from the transcript.
                await self._drop(live, "timed out")
                raise BackendError(
                    f"*{name}* ran past {int(self._turn_timeout)}s without "
                    "finishing. I let it go; ask again to pick up where it got to."
                ) from None
            except BackendError:
                raise
            except Exception as exc:
                log.exception("agent %s failed", name)
                await self._drop(live, type(exc).__name__)
                raise BackendError(f"*{name}* failed: {exc}") from exc
            finally:
                live.busy = False
                live.caller = None

    async def close(self) -> None:
        for live in self._live.values():
            await self._drop(live, "")

    # -- the session ------------------------------------------------------
    async def _connect(self, live: _Live) -> object:
        if live.client is not None:
            return live.client
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        except ImportError as exc:
            raise BackendError(
                "The Claude Agent SDK isn't installed on this box. "
                "Install ricardi with the 'sdk' extra."
            ) from exc

        options = self._options(live)
        client = ClaudeSDKClient(options=ClaudeAgentOptions(**options))
        await client.connect()
        live.client = client
        live.last_fault = ""
        log.info("agent %s connected in %s", live.spec.name, live.spec.cwd)
        return client

    def _options(self, live: _Live) -> dict[str, object]:
        """Everything the session is started with. Separate so it is testable.

        A connection cannot be made in a test without spending money, but what
        it would have been started with is exactly the part worth asserting.
        """
        from claude_agent_sdk import HookMatcher

        spec = live.spec
        options: dict[str, object] = {"cwd": spec.cwd}
        if spec.system_prompt:
            options["system_prompt"] = spec.system_prompt
        # Ceilings the SDK enforces itself, so a turn that runs away stops at a
        # number someone chose rather than when a person notices.
        if spec.max_budget_usd > 0:
            options["max_budget_usd"] = spec.max_budget_usd
        if spec.max_turns > 0:
            options["max_turns"] = spec.max_turns

        # The enforcement point. A PreToolUse hook fires for every tool call,
        # which can_use_tool does not: the SDK auto-approves anything an
        # allowed_tools entry covers *before* consulting that callback, so a
        # gate built on it silently never runs for the tools you listed. Nothing
        # is passed here that could auto-approve, and settings files are not
        # loaded, so this hook is the only thing that can grant a tool.
        #
        # The timeout is passed rather than inherited. Left unset the SDK
        # applies its own default, so the deadline on the one check that
        # enforces everything would be a number nobody here chose -- and what
        # the CLI does when a hook exceeds it is not established. See
        # SECURITY.md, "What fails closed, and what does not".
        options["hooks"] = {
            "PreToolUse": [
                HookMatcher(hooks=[self._gate(live)], timeout=GATE_TIMEOUT)
            ]
        }
        options["setting_sources"] = []
        # Resuming keeps an agent's memory across a Ricardi restart. The
        # session id comes from the transcript on this same box.
        if live.session_id:
            options["resume"] = live.session_id
        return options

    def _gate(self, live: _Live):
        """The PreToolUse hook: every tool call, checked before it runs.

        A denial carries a reason the model reads, so a refused agent adapts
        instead of flailing at the same wall.
        """

        def decided(allow: bool, reason: str) -> dict:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow" if allow else "deny",
                    "permissionDecisionReason": reason,
                }
            }

        async def gate(payload: dict, tool_use_id: str | None, context: object):
            tool = str(payload.get("tool_name") or "")
            tool_input = dict(payload.get("tool_input") or {})
            caller = live.caller or Caller()

            # Observation only: every call below sits beside a decision, never
            # in front of one, and a gate with no sink skips it entirely.
            async def emit(body: object) -> None:
                if caller.events is not None:
                    await caller.events.emit("ricardi", body, turn=caller.turn)

            if self._policy is None:
                reason = "No policy is configured, so no tool is permitted."
                await emit(Refused(rule="no policy", reason=reason, tier="",
                                   tool=tool, target=""))
                return decided(False, reason)
            request = Request(
                action="tool",
                principal=caller.principal,
                resource=caller.resource,
                agent=live.spec.name,
                tool=tool,
                tool_input=dict(tool_input or {}),
                flags=caller.flags,
                # Passed on every tool call, so a rule that forgot to bound its
                # paths cannot let this name reach another name's tree.
                agent_roots=live.spec.roots,
            )
            try:
                decision = await self._policy.decide(request)
            except Exception:
                # A policy that errors must not become a policy that permits.
                log.exception("policy failed on %s for %s", tool, live.spec.name)
                reason = "I couldn't check whether that's permitted, so I didn't run it."
                await emit(Refused(rule="policy error", reason=reason, tier="",
                                   tool=tool, target=""))
                return decided(False, reason)

            if not decision.allow:
                log.info("denied %s for %s: %s", tool, live.spec.name, decision.reason)
                # An unmatched deny reads "default deny", the same words
                # audit.py has always logged: an operator comparing a client's
                # view against the audit log must see one wording, not two.
                await emit(Refused(rule=decision.rule or "default deny",
                                   reason=decision.reason,
                                   tier=decision.tier, tool=tool, target=""))
                return decided(False, decision.reason)

            # No event for an allowed call: `_one_turn` emits a ToolStarted for
            # every one of them, under the SDK's own tool_use_id, and that is
            # the announcement now that a transport renders the stream. One
            # here as well would show a client two starts for one call.
            #
            # The `announce` obligation is therefore nothing this reads. It is
            # still parsed and validated by `policy.py`, because policy files
            # in the field carry it and a restart that failed validation on a
            # running box would be a worse outcome than an obligation that
            # describes what every call already does.
            return decided(True, "")

        return gate

    async def _one_turn(self, live: _Live, client: object, text: str) -> str:
        """Run one turn, and report it as it happens.

        The answer this returns is the whole turn flattened to a string, which
        is all Slack can hold. The stream is the same turn unflattened, for a
        client that can show a tool call while it is still running.
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        stream: Stream | None = live.caller.events if live.caller else None
        # The caller's id when it has one, so the gate's events and the turn's
        # are one story; minted only when nobody named the turn.
        turn = (live.caller.turn if live.caller else "") or new_turn_id()

        async def emit(body: object, actor: str = "agent") -> None:
            if stream is not None:
                await stream.emit(actor, body, turn=turn)

        said: list[str] = []
        # tool_use_id -> (tool, when it began). The call names the tool and the
        # result does not, and neither is timed, so the pairing is kept here.
        running: dict[str, tuple[str, float]] = {}
        # What the turn ended as, decided on each way out of the block below.
        outcome = "done"
        answer = ""

        try:
            # The turn's own deadline. Held here rather than around `ask`'s
            # await so that an expiry arrives as `TimeoutError` and anything
            # else that cancels this task -- a shutdown, a relay that left
            # early -- arrives as a bare `CancelledError`. Told apart, they
            # close the turn with different outcomes; from outside they are one
            # cancellation, and every restart would report a timeout that never
            # happened.
            async with asyncio.timeout(self._turn_timeout):
                # Inside the try, not before it: a sink that suspends -- which
                # is every real transport -- can be cancelled here, and a turn
                # opened outside would then never be closed. The trade is a
                # TurnEnded whose TurnStarted was lost, which a client can
                # ignore, against a turn it is told began and never hears about
                # again. "person" rather than "owner": the backend is given a
                # principal, not a standing, and guessing the higher of the two
                # would be the wrong way to guess.
                await emit(TurnStarted(text=text), actor="person")
                await client.query(text)  # type: ignore[attr-defined]
                async for message in client.receive_response():  # type: ignore[attr-defined]
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock) and (spoken := block.text.strip()):
                                said.append(spoken)
                                await emit(Said(text=spoken))
                            elif isinstance(block, ThinkingBlock):
                                if mused := block.thinking.strip():
                                    await emit(Thought(text=mused))
                            elif isinstance(block, ToolUseBlock):
                                running[block.id] = (block.name, time.monotonic())
                                # Through the factory, never the class: it is
                                # what summarises, and a ToolStarted built by
                                # hand would be one with the arguments still in
                                # it.
                                await emit(
                                    tool_started(block.name, block.input or {}, block.id))
                    elif isinstance(message, UserMessage):
                        # A tool's result arrives as the user's turn; that is
                        # the only reason this message type is read here.
                        content = message.content if isinstance(message.content, list) else ()
                        for block in content:
                            if not isinstance(block, ToolResultBlock):
                                continue
                            await emit(
                                tool_output(block.tool_use_id, _result_text(block.content)))
                            call = running.pop(block.tool_use_id, None)
                            if call is None:
                                # A result for a call this turn never saw start.
                                # The output is still worth showing; a
                                # ToolFinished would have to invent both the
                                # tool and the time it took, and an invented
                                # duration is worse than none.
                                log.debug("result for unknown tool call %s",
                                          block.tool_use_id)
                                continue
                            tool, began = call
                            await emit(ToolFinished(
                                tool_use_id=block.tool_use_id,
                                tool=tool,
                                ok=not block.is_error,
                                ms=int((time.monotonic() - began) * 1000),
                            ))
                    elif isinstance(message, ResultMessage):
                        if getattr(message, "session_id", ""):
                            live.session_id = message.session_id
                            self._sessions.remember(live.spec.name, message.session_id)
                        cost = getattr(message, "total_cost_usd", None)
                        if isinstance(cost, (int, float)):
                            live.spent += float(cost)
                            log.info(
                                "agent %s turn cost $%.4f (session total $%.4f)",
                                live.spec.name, cost, live.spent,
                            )
                        if message.subtype != "success":
                            cut = _CUT_SHORT.get(message.subtype)
                            if cut:
                                outcome = "cut_short"
                            else:
                                # Not a ceiling: the SDK is reporting that the
                                # turn broke. Without this a subtype nobody
                                # anticipated, with text already spoken, closes
                                # as `done` -- a complete-looking answer for a
                                # turn that failed, which is the exact mislabel
                                # the outcome vocabulary exists to prevent.
                                outcome = "failed"
                            if cut and said:
                                # It answered, but not to the end. Say so rather
                                # than letting a truncated answer read as a
                                # finished one.
                                said.append(f"_(stopped early: {cut})_")
                            elif not said:
                                raise BackendError(
                                    f"*{live.spec.name}* stopped early "
                                    f"({cut or message.subtype})."
                                )
            answer = assemble(said)
            return answer
        except asyncio.TimeoutError:
            # This turn's own deadline, raised by the block above. A client
            # that never saw the turn close would show it running still.
            outcome = "timed_out"
            raise
        except asyncio.CancelledError:
            # Somebody else stopped it: a shutdown, or a relay that gave up on
            # the answer. Nothing ran out of time, and saying so in every live
            # thread on every deploy is a lie the channel has no way to check.
            outcome = "cut_short"
            raise
        except BaseException:
            # A ceiling already seen keeps `cut_short`: such a turn raised
            # because it had nothing left to say, not because anything broke.
            # BaseException rather than Exception so that nothing at all can
            # leave this block with the turn still reported as finished.
            if outcome != "cut_short":
                outcome = "failed"
            raise
        finally:
            # Every way out passes through here -- a result, an exception and a
            # timeout -- which is what makes a turn that never closes impossible
            # rather than merely unlikely. `answer` is empty on the ways out
            # that had none; the Said events already carry whatever was said.
            await emit(TurnEnded(outcome=outcome, answer=answer))

    async def _drop(self, live: _Live, reason: str) -> None:
        client = live.client
        live.client = None
        live.last_fault = reason
        if client is None:
            return
        try:
            await client.disconnect()  # type: ignore[attr-defined]
        except Exception:
            log.debug("disconnecting %s failed", live.spec.name, exc_info=True)
