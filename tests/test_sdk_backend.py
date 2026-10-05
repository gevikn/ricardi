"""The SDK backend's own logic, with the session machinery stubbed.

The live tests prove it works against real Claude Code. These prove the parts
that are awkward to provoke there: what happens when two people ask one agent
at once, and how a turn's output is assembled.
"""

import asyncio

import pytest

from ricardi.backends.base import BackendError
from ricardi.backends.sdk import MAX_ANSWER, SdkBackend, assemble
from ricardi.config import AgentSpec


def spec(name="scout", cwd="."):
    return AgentSpec(name=name, cwd=cwd, description="a stub")


class Stubbed(SdkBackend):
    """A backend whose sessions are replaced by a recorded delay."""

    def __init__(self, specs, delay=0.0, **kw):
        super().__init__(specs, **kw)
        self._delay = delay
        self.order: list[str] = []

    async def _connect(self, live):
        return object()

    async def _one_turn(self, live, client, text):
        self.order.append(f"start {live.spec.name} {text}")
        if self._delay:
            await asyncio.sleep(self._delay)
        self.order.append(f"end {live.spec.name} {text}")
        return f"answered {text}"


# -- assembling the answer ------------------------------------------------
def test_nothing_said_is_said_plainly():
    assert assemble([]) == "(finished without saying anything)"
    assert assemble(["  ", ""]) == "(finished without saying anything)"


def test_a_single_block_comes_through_as_is():
    assert assemble(["the answer"]) == "the answer"


def test_every_block_is_kept_not_only_the_last():
    # The regression this guards: an agent that works in steps ends on "Done."
    # and the substance is in an earlier block.
    answer = assemble(["I found it in auth.py line 40.", "Done."])
    assert "auth.py" in answer
    assert "Done." in answer


def test_blocks_are_separated_so_they_read_as_paragraphs():
    assert assemble(["one", "two"]) == "one\n\ntwo"


def test_a_very_long_answer_is_truncated_and_says_so():
    answer = assemble(["x" * (MAX_ANSWER + 500)])
    assert len(answer) < MAX_ANSWER + 200
    assert "truncated" in answer
    assert str(MAX_ANSWER + 500) in answer


# -- one turn at a time per agent -----------------------------------------
async def test_two_asks_to_one_agent_do_not_interleave():
    backend = Stubbed([spec()], delay=0.05)
    try:
        await asyncio.gather(
            backend.ask("scout", "first"),
            backend.ask("scout", "second"),
        )
    finally:
        await backend.close()
    # A session is one conversation: the second turn must not start until the
    # first has finished, or the two prompts interleave into nonsense.
    assert backend.order[0].startswith("start")
    assert backend.order[1].startswith("end")
    assert backend.order[2].startswith("start")
    assert backend.order[3].startswith("end")
    assert backend.order[0].split()[-1] == backend.order[1].split()[-1]


async def test_different_agents_run_at_the_same_time():
    backend = Stubbed([spec("lefty"), spec("righty")], delay=0.1)
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.gather(
            backend.ask("lefty", "a"),
            backend.ask("righty", "b"),
        )
        elapsed = loop.time() - started
    finally:
        await backend.close()
    # Serialised would be 0.2s; they share no lock, so this is well under.
    assert elapsed < 0.18, elapsed


async def test_an_unknown_agent_never_reaches_a_session():
    backend = Stubbed([spec()])
    try:
        with pytest.raises(BackendError, match="no agent called"):
            await backend.ask("nobody", "hello")
    finally:
        await backend.close()
    assert backend.order == []


async def test_busy_is_reported_while_a_turn_runs():
    backend = Stubbed([spec()], delay=0.1)
    try:
        task = asyncio.create_task(backend.ask("scout", "slow"))
        await asyncio.sleep(0.02)
        statuses = {a.name: a.status for a in await backend.list_agents()}
        assert statuses["scout"] == "busy"
        await task
        statuses = {a.name: a.status for a in await backend.list_agents()}
        assert statuses["scout"] == "ready"
    finally:
        await backend.close()


async def test_a_turn_that_overruns_leaves_the_agent_askable():
    backend = Stubbed([spec()], delay=5.0, turn_timeout=0.05)
    try:
        with pytest.raises(BackendError, match="without finishing"):
            await backend.ask("scout", "forever")
        # Reported as ready with the fault noted, not as down: the next message
        # reconnects and resumes from the transcript.
        status = (await backend.list_agents())[0].status
        assert status.startswith("ready")
        assert "timed out" in status
    finally:
        await backend.close()


async def test_a_backend_with_no_agents_is_refused_at_construction():
    with pytest.raises(ValueError, match="at least one agent"):
        SdkBackend([])


# -- the tool enforcement point -------------------------------------------
# The gate is what makes "what Claude may do on the box" executable rather than
# agreed in a conversation, so it is tested directly rather than through a turn.
from ricardi.backends.base import Caller  # noqa: E402
from ricardi.policy import Principal, Rule, RulesPolicy  # noqa: E402


def gated(rules, notice=None):
    backend = Stubbed([spec()], policy=RulesPolicy(rules))
    live = backend._live["scout"]
    live.caller = Caller(
        principal=Principal(id="U1", email="you@example.com", known=True),
        notice=notice,
    )
    hook = backend._gate(live)

    async def call(tool, tool_input):
        # The shape the SDK hands a PreToolUse hook.
        out = await hook(
            {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input},
            "toolu_1",
            None,
        )
        return out["hookSpecificOutput"]

    return backend, call


async def test_a_permitted_tool_is_allowed():
    _, gate = gated([Rule(effect="allow", tools=["Read"])])
    result = await gate("Read", {"file_path": "/repo/main.py"})
    assert result["permissionDecision"] == "allow"


async def test_a_tool_no_rule_permits_is_denied():
    _, gate = gated([Rule(effect="allow", tools=["Read"])])
    result = await gate("Bash", {"command": "rm -rf /"})
    assert result["permissionDecision"] == "deny"
    # The message goes to the model, so it should say what to do instead.
    assert "permitted" in result["permissionDecisionReason"].lower()


async def test_a_denied_tool_is_refused_with_a_reason_the_model_can_act_on():
    # Denied, not killed: the agent should be able to try something else.
    _, gate = gated([Rule(effect="allow", tools=["Read"])])
    result = await gate("Bash", {"command": "ls"})
    assert result["permissionDecision"] == "deny"
    assert result["permissionDecisionReason"].strip()


async def test_the_denial_names_the_command_so_a_human_can_read_the_log():
    _, gate = gated([Rule(effect="allow", tools=["Read"])])
    result = await gate("Bash", {"command": "git push origin main"})
    assert "git push origin main" in result["permissionDecisionReason"]


async def test_an_announced_command_reaches_the_channel_before_it_runs():
    said: list[str] = []

    async def notice(line):
        said.append(line)

    _, gate = gated(
        [Rule(effect="allow", tools=["Bash"], obligations=["announce"])],
        notice=notice,
    )
    result = await gate("Bash", {"command": "pytest -q"})
    assert result["permissionDecision"] == "allow"
    assert said == ["Bash: pytest -q"]


async def test_a_tool_allowed_without_the_obligation_is_silent():
    said: list[str] = []

    async def notice(line):
        said.append(line)

    _, gate = gated([Rule(effect="allow", tools=["Read"])], notice=notice)
    await gate("Read", {"file_path": "/repo/main.py"})
    assert said == []


async def test_a_failing_announcement_does_not_block_the_tool():
    async def notice(line):
        raise RuntimeError("slack is down")

    _, gate = gated(
        [Rule(effect="allow", tools=["Bash"], obligations=["announce"])],
        notice=notice,
    )
    result = await gate("Bash", {"command": "pytest"})
    assert result["permissionDecision"] == "allow"


async def test_a_policy_that_raises_denies_the_tool():
    class Exploding:
        async def decide(self, request):
            raise RuntimeError("down")

    backend = Stubbed([spec()], policy=Exploding())
    live = backend._live["scout"]
    live.caller = Caller()
    out = await backend._gate(live)(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_without_a_policy_every_tool_is_refused():
    backend = Stubbed([spec()])
    live = backend._live["scout"]
    out = await backend._gate(live)(
        {"tool_name": "Read", "tool_input": {"file_path": "/x"}}, None, None
    )
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "no policy" in decision["permissionDecisionReason"].lower()


async def test_the_gate_sees_who_asked_not_just_which_agent():
    seen = {}

    class Watching:
        async def decide(self, request):
            seen["email"] = request.principal.email
            seen["agent"] = request.agent
            seen["tool"] = request.tool
            from ricardi.policy import Decision

            return Decision(allow=True)

    backend = Stubbed([spec()], policy=Watching())
    live = backend._live["scout"]
    live.caller = Caller(principal=Principal(id="U1", email="who@example.com", known=True))
    await backend._gate(live)(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None
    )
    assert seen == {"email": "who@example.com", "agent": "scout", "tool": "Bash"}
