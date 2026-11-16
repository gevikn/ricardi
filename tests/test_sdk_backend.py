"""The SDK backend's own logic, with the session machinery stubbed.

The live tests prove it works against real Claude Code. These prove the parts
that are awkward to provoke there: what happens when two people ask one agent
at once, and how a turn's output is assembled.
"""

import asyncio
import importlib.util
import json

import pytest

from ricardi.backends.base import BackendError
from ricardi.backends.sdk import SdkBackend, assemble
from ricardi.config import AgentSpec
from ricardi.events import Recorder, Stream, ToolFinished, ToolStarted, to_wire


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
        # The deadline belongs to the turn, not to `ask`, so a stub standing in
        # for a turn has to keep it -- otherwise an overrunning stub is one no
        # timeout ever reaches.
        async with asyncio.timeout(self._turn_timeout):
            if self._delay:
                await asyncio.sleep(self._delay)
        self.order.append(f"end {live.spec.name} {text}")
        return f"answered {text}"


# -- assembling the answer ------------------------------------------------
def test_nothing_said_assembles_to_nothing():
    # The empty answer is how `TurnEnded` says the agent spoke no blocks; the
    # sentence a person reads for one of those is render.py's to choose.
    assert assemble([]) == ""
    assert assemble(["  ", ""]) == ""


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


def test_a_long_answer_is_carried_whole_rather_than_cut_here():
    # The ceiling belongs to the one reader that has one. Cutting here would
    # mean a browser client could never be shown the rest of the answer.
    answer = assemble(["x" * 9000])
    assert answer == "x" * 9000


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


def gated(rules):
    backend = Stubbed([spec()], policy=RulesPolicy(rules))
    live = backend._live["scout"]
    live.caller = Caller(
        principal=Principal(id="U1", email="you@example.com", known=True),
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


def test_the_gate_runs_under_a_timeout_we_chose():
    """The hook that enforces every tool decision has a deadline.

    Left unset it inherits the SDK's 60-second default, and what the CLI does
    when a hook exceeds it -- permit or refuse -- is not something this project
    has established. Two timeouts exist and only one of them is ours; naming it
    is how it stays ours if the default ever moves.
    """
    pytest.importorskip("claude_agent_sdk")
    from ricardi.backends.sdk import GATE_TIMEOUT

    backend = Stubbed([spec()])
    (matcher,) = backend._options(backend._live["scout"])["hooks"]["PreToolUse"]
    assert matcher.timeout == GATE_TIMEOUT


async def test_the_gate_passes_the_agents_roots_to_policy():
    """Without this, the per-name path bound silently does nothing.

    `policy.py` treats an empty `agent_roots` as "unconstrained by that check",
    which is what keeps a request with no agent behind it working. That makes
    this the one place the bound can be lost, so it is the one place worth
    asserting rather than assuming.
    """
    seen = {}

    class Watching:
        async def decide(self, request):
            seen["roots"] = request.agent_roots
            from ricardi.policy import Decision

            return Decision(allow=True)

    rooted = AgentSpec(
        name="scout", cwd=".", roots=("/workspace/one", "/workspace/two")
    )
    backend = Stubbed([rooted], policy=Watching())
    live = backend._live["scout"]
    live.caller = Caller(principal=Principal(id="U1", email="who@example.com", known=True))
    await backend._gate(live)(
        {"tool_name": "Read", "tool_input": {"file_path": "/workspace/one/x.py"}}, None, None
    )
    assert seen["roots"] == ("/workspace/one", "/workspace/two")


# -- what a turn puts on the stream ---------------------------------------
# These are the only tests that run the real `_one_turn`: `Replaying` stands in
# for the session, not for the turn, so what they assert is the emission order
# of the code itself rather than of a stub. The turn is where a client's whole
# picture comes from, and a turn that never closes leaves one spinning.

# The real `_one_turn` reads the SDK's message shapes, so these need it
# installed; nothing else in this file does.
needs_sdk = pytest.mark.skipif(
    importlib.util.find_spec("claude_agent_sdk") is None,
    reason="the Claude Agent SDK isn't installed",
)


class Replaying(SdkBackend):
    """A backend whose session replays prepared messages.

    An item that is an exception is raised rather than yielded, and a number is
    a pause -- which is how a turn is made to overrun its timeout.
    """

    def __init__(self, specs, messages, **kw):
        super().__init__(specs, **kw)
        self._messages = messages

    async def _connect(self, live):
        return _Session(self._messages)


class _Session:
    def __init__(self, messages):
        self._messages = messages
        self.asked: list[str] = []

    async def query(self, text):
        self.asked.append(text)

    async def receive_response(self):
        for message in self._messages:
            if isinstance(message, BaseException):
                raise message
            if isinstance(message, (int, float)):
                await asyncio.sleep(message)
                continue
            yield message


def said(text):
    from claude_agent_sdk import AssistantMessage, TextBlock

    return AssistantMessage(content=[TextBlock(text=text)], model="claude")


def thought(text):
    from claude_agent_sdk import AssistantMessage, ThinkingBlock

    return AssistantMessage(
        content=[ThinkingBlock(thinking=text, signature="sig")], model="claude"
    )


def tool_call(tool_use_id="tu_1", name="Bash", **tool_input):
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    return AssistantMessage(
        content=[ToolUseBlock(id=tool_use_id, name=name, input=tool_input)], model="claude"
    )


def tool_result(tool_use_id="tu_1", content="ok", is_error=False):
    from claude_agent_sdk import ToolResultBlock, UserMessage

    return UserMessage(
        content=[
            ToolResultBlock(tool_use_id=tool_use_id, content=content, is_error=is_error)
        ]
    )


def finished(subtype="success"):
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=subtype != "success",
        num_turns=1,
        session_id="sess-1",
    )


def a_turn(messages, **kw):
    """A backend replaying `messages`, with a recorder wired to the caller."""
    recorder = Recorder()
    backend = Replaying([spec("ricardi")], messages, **kw)
    caller = Caller(events=Stream("ricardi", recorder))
    return backend, recorder, caller


def kinds(recorder):
    return [type(e.body).__name__ for e in recorder.events]


@needs_sdk
async def test_a_turn_emits_text_tool_calls_and_an_outcome():
    backend, recorder, caller = a_turn(
        [tool_call(command="deploy"), tool_result(), said("hello"), finished()]
    )
    try:
        assert await backend.ask("ricardi", "go", caller) == "hello"
    finally:
        await backend.close()
    assert kinds(recorder) == ["TurnStarted", "ToolStarted", "ToolOutput",
                               "ToolFinished", "Said", "TurnEnded"]
    assert recorder.events[0].body.text == "go"
    assert recorder.events[-1].body.outcome == "done"
    assert recorder.events[-1].body.answer == "hello"


@needs_sdk
async def test_every_event_in_a_turn_carries_the_same_turn_id():
    backend, recorder, caller = a_turn([said("hello"), finished()])
    try:
        await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    turns = {e.envelope.turn for e in recorder.events}
    assert len(turns) == 1 and turns != {""}


@needs_sdk
async def test_a_tool_block_emits_the_summary_and_never_the_arguments():
    backend, recorder, caller = a_turn(
        [tool_call(command="deploy", token="hunter2"), tool_result(), finished()]
    )
    try:
        await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    started = next(e for e in recorder.events if isinstance(e.body, ToolStarted))
    assert started.body.tool_use_id == "tu_1"
    assert started.body.summary == "Bash: deploy"
    # The structure of a tool's input never reaches the stream, so an argument
    # the summary did not quote cannot leak through some other field.
    assert "hunter2" not in json.dumps([to_wire(e) for e in recorder.events])


@needs_sdk
async def test_a_tool_result_is_tied_to_its_call_and_timed():
    backend, recorder, caller = a_turn(
        [tool_call(command="deploy"), tool_result(content="built 3 files"), finished()]
    )
    try:
        await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    output = recorder.events[2].body
    assert (output.tool_use_id, output.text, output.truncated) == (
        "tu_1", "built 3 files", False)
    ended = next(e.body for e in recorder.events if isinstance(e.body, ToolFinished))
    assert (ended.tool_use_id, ended.tool, ended.ok) == ("tu_1", "Bash", True)
    assert isinstance(ended.ms, int) and ended.ms >= 0


@needs_sdk
async def test_a_tool_that_failed_says_so_rather_than_being_dropped():
    backend, recorder, caller = a_turn(
        [tool_call(command="deploy"), tool_result(content="boom", is_error=True), finished()]
    )
    try:
        await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    assert recorder.events[3].body.ok is False


@needs_sdk
async def test_a_result_for_a_call_we_never_saw_is_not_given_a_made_up_duration():
    # The output is still worth showing; a ToolFinished would have to invent
    # both the tool's name and how long it took.
    backend, recorder, caller = a_turn([tool_result(tool_use_id="tu_ghost"), finished()])
    try:
        await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    assert kinds(recorder) == ["TurnStarted", "ToolOutput", "TurnEnded"]


@needs_sdk
async def test_a_tool_still_open_when_a_turn_ends_closes_the_turn_anyway():
    backend, recorder, caller = a_turn([tool_call(command="deploy"), finished()])
    try:
        await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    assert kinds(recorder) == ["TurnStarted", "ToolStarted", "TurnEnded"]
    assert recorder.events[-1].body.outcome == "done"


@needs_sdk
async def test_the_agents_reasoning_is_carried_apart_from_what_it_said():
    backend, recorder, caller = a_turn([thought("hmm"), said("hello"), finished()])
    try:
        await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    assert kinds(recorder) == ["TurnStarted", "Thought", "Said", "TurnEnded"]
    assert recorder.events[1].body.text == "hmm"


@needs_sdk
async def test_an_agent_that_says_nothing_still_opens_and_closes_the_turn():
    # An empty turn must be distinguishable from a lost one: the stream shows
    # a turn that started and ended with no Said between them.
    backend, recorder, caller = a_turn([finished()])
    try:
        assert await backend.ask("ricardi", "go", caller) == ""
    finally:
        await backend.close()
    assert kinds(recorder) == ["TurnStarted", "TurnEnded"]
    assert recorder.events[-1].body.outcome == "done"


@needs_sdk
async def test_a_turn_that_raises_still_closes_with_failed():
    backend, recorder, caller = a_turn([said("partly"), RuntimeError("the session died")])
    try:
        with pytest.raises(BackendError):
            await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    assert kinds(recorder)[-1] == "TurnEnded"
    assert recorder.events[-1].body.outcome == "failed"


@needs_sdk
async def test_a_turn_that_times_out_closes_with_timed_out():
    backend, recorder, caller = a_turn([said("partly"), 5.0, finished()], turn_timeout=0.05)
    try:
        with pytest.raises(BackendError, match="without finishing"):
            await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    assert recorder.events[-1].body.outcome == "timed_out"


class Suspending:
    """A sink that yields to the loop on every event, as a socket does.

    `Recorder` never suspends, so no test built on it can see a cancellation
    that lands *inside* an emit -- and the first emit of a turn is the one
    that, left outside the `finally`, opens a turn nothing ever closes.
    """

    def __init__(self, pause):
        self.events = []
        self._pause = pause

    async def emit(self, event):
        self.events.append(event)
        await asyncio.sleep(self._pause)


@needs_sdk
async def test_a_turn_cancelled_while_it_opens_is_still_closed():
    # The timeout lands in the middle of the TurnStarted emit, which only a
    # sink that suspends can arrange. Move that emit back outside the try and
    # the client is left holding a turn that opened and never ended.
    sink = Suspending(pause=0.2)
    backend = Replaying([spec("ricardi")], [said("hello"), finished()], turn_timeout=0.02)
    caller = Caller(events=Stream("ricardi", sink))
    try:
        with pytest.raises(BackendError, match="without finishing"):
            await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    assert [type(e.body).__name__ for e in sink.events] == ["TurnStarted", "TurnEnded"]
    assert sink.events[-1].body.outcome == "timed_out"


@needs_sdk
async def test_a_turn_cancelled_from_outside_closes_as_cut_short():
    # Not `timed_out`. Shutdown cancels every turn still in flight, and app.py
    # cancels the backend task when the relay leaves early; while the route is
    # still bound, reporting those as a timeout posts "ran out of time" into
    # every live thread on each restart, which is simply untrue.
    sink = Suspending(pause=0.1)
    backend = Replaying([spec("ricardi")], [said("hello"), finished()], turn_timeout=60.0)
    caller = Caller(events=Stream("ricardi", sink))
    task = asyncio.create_task(backend.ask("ricardi", "go", caller))
    try:
        # Lands inside the TurnStarted emit, which is where a shutdown lands.
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await backend.close()
    assert [type(e.body).__name__ for e in sink.events] == ["TurnStarted", "TurnEnded"]
    assert sink.events[-1].body.outcome == "cut_short"


@needs_sdk
async def test_a_result_the_sdk_called_an_error_does_not_close_as_done():
    # Not a ceiling, so `_CUT_SHORT` has no entry for it, and the agent had
    # already spoken. Left as "done" a client and the channel show a
    # complete-looking answer for a turn the SDK reported as broken.
    backend, recorder, caller = a_turn(
        [said("here is what I found"), finished("error_during_execution")]
    )
    try:
        assert await backend.ask("ricardi", "go", caller) == "here is what I found"
    finally:
        await backend.close()
    assert recorder.events[-1].body.outcome == "failed"


@needs_sdk
async def test_a_cut_short_turn_says_so_in_the_outcome():
    backend, recorder, caller = a_turn([said("partly"), finished("error_max_turns")])
    try:
        await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    assert recorder.events[-1].body.outcome == "cut_short"


@needs_sdk
async def test_a_ceiling_reached_with_nothing_said_is_cut_short_not_failed():
    # It raises, because there is no answer to give; but nothing broke, and a
    # client that showed "failed" would send someone looking for a fault.
    backend, recorder, caller = a_turn([finished("error_max_budget_usd")])
    try:
        with pytest.raises(BackendError, match="stopped early"):
            await backend.ask("ricardi", "go", caller)
    finally:
        await backend.close()
    assert recorder.events[-1].body.outcome == "cut_short"


@needs_sdk
async def test_a_turn_with_no_sink_behaves_exactly_as_before():
    backend = Replaying([spec("ricardi")], [tool_call(command="deploy"), tool_result(),
                                            said("hello"), finished()])
    try:
        assert await backend.ask("ricardi", "go", Caller()) == "hello"
        assert await backend.ask("ricardi", "go") == "hello"
    finally:
        await backend.close()


@needs_sdk
async def test_a_sink_that_fails_does_not_cost_the_turn_its_answer():
    class Broken:
        async def emit(self, event):
            raise RuntimeError("the client went away")

    backend = Replaying([spec("ricardi")], [said("hello"), finished()])
    caller = Caller(events=Stream("ricardi", Broken()))
    try:
        assert await backend.ask("ricardi", "go", caller) == "hello"
    finally:
        await backend.close()
