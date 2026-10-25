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
from dataclasses import dataclass, field

from ricardi.backends.base import Agent, AgentBackend, BackendError, Caller
from ricardi.config import AgentSpec
from ricardi.policy import PolicyPoint, Request, summarise
from ricardi.store import SessionIds

log = logging.getLogger("ricardi.sdk")

# A turn that never ends would leave Slack silent forever. Better to say so.
DEFAULT_TURN_TIMEOUT = 600.0
# Slack accepts far more than this, but a wall of text in a channel is not an
# answer. Long output belongs in a file the agent writes and names.
MAX_ANSWER = 3500

# Result subtypes that mean a ceiling was reached rather than something broke.
_CUT_SHORT = {
    "error_max_budget_usd": "this turn reached its spending limit",
    "error_max_turns": "this turn reached its step limit",
}


def assemble(said: list[str]) -> str:
    """One Slack message from everything the agent said this turn.

    Every block, not just the last. An agent that works in steps often ends on
    "Done." with the substance several blocks earlier, so keeping only the
    closing text loses the answer. Verbosity is the lesser fault.
    """
    blocks = [b.strip() for b in said if b and b.strip()]
    if not blocks:
        return "(finished without saying anything)"
    answer = "\n\n".join(blocks)
    if len(answer) <= MAX_ANSWER:
        return answer
    kept = answer[:MAX_ANSWER].rstrip()
    return f"{kept}\n\n[...truncated, {len(answer)} characters in all]"


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
                return await asyncio.wait_for(
                    self._one_turn(live, client, text), timeout=self._turn_timeout
                )
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
        options["hooks"] = {"PreToolUse": [HookMatcher(hooks=[self._gate(live)])]}
        options["setting_sources"] = []
        # Resuming keeps an agent's memory across a Ricardi restart. The
        # session id comes from the transcript on this same box.
        if live.session_id:
            options["resume"] = live.session_id

        client = ClaudeSDKClient(options=ClaudeAgentOptions(**options))
        await client.connect()
        live.client = client
        live.last_fault = ""
        log.info("agent %s connected in %s", spec.name, spec.cwd)
        return client

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
            if self._policy is None:
                return decided(False, "No policy is configured, so no tool is permitted.")
            caller = live.caller or Caller()
            request = Request(
                action="tool",
                principal=caller.principal,
                resource=caller.resource,
                agent=live.spec.name,
                tool=tool,
                tool_input=dict(tool_input or {}),
                flags=caller.flags,
            )
            try:
                decision = await self._policy.decide(request)
            except Exception:
                # A policy that errors must not become a policy that permits.
                log.exception("policy failed on %s for %s", tool, live.spec.name)
                return decided(
                    False, "I couldn't check whether that's permitted, so I didn't run it."
                )

            if not decision.allow:
                log.info("denied %s for %s: %s", tool, live.spec.name, decision.reason)
                return decided(False, decision.reason)

            if "announce" in decision.obligations and caller.notice is not None:
                # Posted before the command runs, but this is a record, not a
                # checkpoint: nothing waits for a reply and there is no way to
                # veto from the channel. It makes use visible, not reversible.
                try:
                    await caller.notice(summarise(tool, request.tool_input))
                except Exception:
                    log.debug("could not announce %s", tool, exc_info=True)

            return decided(True, "")

        return gate

    async def _one_turn(self, live: _Live, client: object, text: str) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        await client.query(text)  # type: ignore[attr-defined]
        said: list[str] = []
        async for message in client.receive_response():  # type: ignore[attr-defined]
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        said.append(block.text.strip())
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
                    if cut and said:
                        # It answered, but not to the end. Say so rather than
                        # letting a truncated answer read as a finished one.
                        said.append(f"_(stopped early: {cut})_")
                    elif not said:
                        raise BackendError(
                            f"*{live.spec.name}* stopped early "
                            f"({cut or message.subtype})."
                        )
        return assemble(said)

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
