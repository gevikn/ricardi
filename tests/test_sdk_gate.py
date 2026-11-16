"""What the tool gate says to a stream, and that saying it changes nothing.

The gate is the one check that enforces everything. These tests hold two
things: a refusal becomes an event a client can render, and the decision is the
same with or without anyone watching.
"""

import importlib.util
import json

import pytest

from ricardi.backends.base import Caller
from ricardi.backends.sdk import SdkBackend
from ricardi.config import AgentSpec
from ricardi.events import Recorder, Refused, Stream, ToolStarted, to_wire
from ricardi.policy import Decision, Principal


class Fixed:
    """A policy that answers with whatever it was given, or raises it."""

    def __init__(self, answer):
        self.answer = answer

    async def decide(self, request):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def make(policy, **caller):
    recorder = Recorder()
    backend = SdkBackend([AgentSpec(name="scout", cwd=".")], policy=policy)
    live = backend._live["scout"]
    live.caller = Caller(
        principal=Principal(id="U1", email="you@example.com", known=True),
        events=Stream("scout", recorder),
        **caller,
    )
    return backend, live, recorder


async def run(live, backend, tool="Edit", tool_input=None, use_id="toolu_1"):
    hook = backend._gate(live)
    return await hook(
        {"tool_name": tool, "tool_input": tool_input or {"file_path": "/x"}},
        use_id,
        None,
    )


DENY = Decision(allow=False, reason="You can read here, not edit.", rule="tier", tier="read")


async def test_a_denied_tool_emits_a_refusal_carrying_the_rule_and_tier():
    backend, live, recorder = make(Fixed(DENY))
    await run(live, backend)
    refused = recorder.events[-1].body
    assert isinstance(refused, Refused)
    assert (refused.rule, refused.tier, refused.tool) == ("tier", "read", "Edit")
    assert refused.reason == DENY.reason
    assert refused.target == ""
    assert recorder.events[-1].envelope.actor == "ricardi"


async def test_the_gate_still_denies_exactly_what_it_denied_before():
    backend, live, _ = make(Fixed(DENY))
    out = await run(live, backend)
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY.reason,
        }
    }


async def test_a_refusal_with_no_tier_is_emitted_with_an_empty_one():
    # The storeless path returns before a tier is stamped. "" is the truth.
    backend, live, recorder = make(Fixed(Decision(allow=False, reason="no", rule="r")))
    await run(live, backend)
    assert recorder.events[-1].body.tier == ""


async def test_a_policy_that_errors_emits_a_refusal_and_still_denies():
    backend, live, recorder = make(Fixed(RuntimeError("down")))
    out = await run(live, backend)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == (
        "I couldn't check whether that's permitted, so I didn't run it."
    )
    assert recorder.events[-1].body.rule == "policy error"
    assert recorder.events[-1].body.tool == "Edit"


async def test_a_missing_policy_emits_a_refusal_and_still_denies():
    backend, live, recorder = make(None)
    out = await run(live, backend)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert recorder.events[-1].body.rule == "no policy"


async def test_an_announced_tool_emits_rather_than_only_posting_a_notice():
    posted = []

    async def notice(text):
        posted.append(text)

    allow = Decision(allow=True, obligations=("announce",))
    backend, live, recorder = make(Fixed(allow), notice=notice)
    out = await run(live, backend, "Bash", {"command": "deploy", "token": "hunter2"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    started = recorder.events[-1].body
    assert isinstance(started, ToolStarted)
    assert (started.tool, started.tool_use_id) == ("Bash", "toolu_1")
    assert posted, "the notice stays until the channel moves onto events"
    assert "hunter2" not in json.dumps([to_wire(e) for e in recorder.events])


async def test_an_allowed_tool_with_no_obligation_emits_nothing():
    backend, live, recorder = make(Fixed(Decision(allow=True)))
    await run(live, backend)
    assert recorder.events == []


async def test_a_gate_with_no_sink_decides_identically():
    # The sink is observation. Removing it must not change one decision.
    async def notice(text):
        pass

    # (what the policy does, how many events a watched gate should emit)
    cases = [
        (DENY, 1),
        (Decision(allow=False, reason="no", rule="r"), 1),
        (Decision(allow=True), 0),
        (Decision(allow=True, obligations=("announce",)), 1),
        (RuntimeError("down"), 1),
        (None, 1),
    ]
    for answer, emitted in cases:
        policy = None if answer is None else Fixed(answer)
        watched, wlive, recorder = make(policy, notice=notice)
        bare, blive, _ = make(policy, notice=notice)
        blive.caller = Caller(principal=wlive.caller.principal, notice=notice)
        assert blive.caller.events is None
        assert await run(wlive, watched) == await run(blive, bare), answer
        # The watched half really was watched, or the comparison proves nothing.
        assert len(recorder.events) == emitted, answer


async def test_a_gate_with_no_caller_still_denies_and_does_not_raise():
    backend = SdkBackend([AgentSpec(name="scout", cwd=".")], policy=Fixed(DENY))
    out = await run(backend._live["scout"], backend)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_caller_has_no_turn_by_default():
    assert Caller().turn == ""


async def test_a_gate_refusal_is_stamped_with_the_callers_turn():
    backend, live, recorder = make(Fixed(DENY), turn="abc123def456")
    await run(live, backend)
    assert recorder.events[-1].envelope.turn == "abc123def456"


needs_sdk = pytest.mark.skipif(
    importlib.util.find_spec("claude_agent_sdk") is None,
    reason="the Claude Agent SDK isn't installed",
)


@needs_sdk
async def test_a_refusal_and_the_turns_own_events_share_the_callers_turn():
    from tests.test_sdk_backend import Replaying, finished, said

    recorder = Recorder()
    backend = Replaying([AgentSpec(name="scout", cwd=".")], [said("hi"), finished()],
                        policy=Fixed(DENY))
    caller = Caller(events=Stream("scout", recorder), turn="feedfacecafe")
    try:
        # The gate runs inside the SDK's session; here it is driven by hand
        # on the same live record the turn uses.
        backend._live["scout"].caller = caller
        await backend._gate(backend._live["scout"])(
            {"tool_name": "Edit", "tool_input": {}}, "t", None
        )
        await backend.ask("scout", "go", caller)
    finally:
        await backend.close()
    assert {e.envelope.turn for e in recorder.events} == {"feedfacecafe"}
    assert any(isinstance(e.body, Refused) for e in recorder.events)
