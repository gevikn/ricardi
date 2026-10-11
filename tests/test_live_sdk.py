"""Ricardi against real Claude Code sessions.

These spend tokens, so they are opt-in:

    RICARDI_LIVE=1 pytest tests/test_live_sdk.py -v

Each agent gets a scratch directory of its own with read-only tools, so nothing
under test can reach real work. The questions have one verifiable answer, which
is what makes an unattended pass meaningful rather than decorative.
"""

import asyncio
import http.client
import json
import os

import pytest

from ricardi.app import Ricardi
from ricardi.backends.base import BackendError
from ricardi.backends.base import Caller
from ricardi.backends.sdk import SdkBackend
from ricardi.config import AgentSpec
from ricardi.policy import Principal, Rule, RulesPolicy
from ricardi.transports.local import LocalTransport

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RICARDI_LIVE") != "1",
        reason="spends tokens; set RICARDI_LIVE=1 to run",
    ),
]

# Something no model would say by chance, so a pass cannot be a coincidence.
MARKER = "quartzite-7731"


def reading_policy():
    """What a question-and-answer agent needs, and nothing else."""
    return RulesPolicy([Rule(effect="allow", tools=["Read", "Glob", "Grep"])])


@pytest.fixture
def scratch(tmp_path):
    (tmp_path / "marker.txt").write_text(MARKER + "\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# Notes\n\nnothing here yet\n", encoding="utf-8")
    return tmp_path


def spec(cwd, name="scout"):
    return AgentSpec(
        name=name,
        cwd=str(cwd),
        description="reads a scratch directory",
        system_prompt=(
            "You answer questions about this directory. Answer in as few words "
            "as possible, with no preamble and no explanation."
        ),
    )


async def test_an_agent_reads_its_own_directory(scratch):
    """That cwd took effect, using the one tool present on every platform.

    A directory *listing* is not portable: Glob and Grep are absent from the
    session on some platforms, where the agent reaches for the shell instead.
    Read is everywhere, so this is what the cwd assertion rests on.
    """
    backend = SdkBackend([spec(scratch)], turn_timeout=180, policy=reading_policy())
    try:
        answer = await backend.ask("scout", "What are the contents of marker.txt?")
        assert MARKER in answer, answer
    finally:
        await backend.close()


async def test_an_agent_remembers_the_turn_before(scratch):
    backend = SdkBackend([spec(scratch)], turn_timeout=180, policy=reading_policy())
    try:
        await backend.ask("scout", "Read marker.txt and remember what it says.")
        # No mention of the file: only the session's own history can answer it.
        answer = await backend.ask("scout", "What did that file say? Repeat it exactly.")
        assert MARKER in answer, answer
    finally:
        await backend.close()


async def test_two_agents_keep_separate_conversations(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "who.txt").write_text("left-alpha\n", encoding="utf-8")
    (right / "who.txt").write_text("right-beta\n", encoding="utf-8")

    backend = SdkBackend([spec(left, "lefty"), spec(right, "righty")], turn_timeout=180,
                         policy=reading_policy())
    try:
        a, b = await asyncio.gather(
            backend.ask("lefty", "What is in who.txt?"),
            backend.ask("righty", "What is in who.txt?"),
        )
        assert "left-alpha" in a, a
        assert "right-beta" in b, b
        # Each ran in its own directory, so neither can see the other's file.
        assert "right-beta" not in a
        assert "left-alpha" not in b
    finally:
        await backend.close()


async def test_an_unknown_agent_is_refused_before_any_session_starts(scratch):
    backend = SdkBackend([spec(scratch)], policy=reading_policy())
    try:
        with pytest.raises(BackendError, match="no agent called"):
            await backend.ask("nobody", "hello")
    finally:
        await backend.close()


async def test_a_turn_that_overruns_is_given_up_on(scratch):
    # A deadline short enough that any real turn trips it, proving Slack hears
    # something rather than nothing when an agent hangs.
    backend = SdkBackend([spec(scratch)], turn_timeout=0.5, policy=reading_policy())
    try:
        with pytest.raises(BackendError, match="without finishing"):
            await backend.ask("scout", "Read marker.txt and summarise it.")
    finally:
        await backend.close()


async def test_the_whole_stack_over_http(scratch):
    """Slack's shape, minus Slack: HTTP in, a real Claude session, an answer out."""
    transport = LocalTransport(port=0)
    backend = SdkBackend([spec(scratch)], turn_timeout=180, policy=reading_policy())
    app = Ricardi(
        transport=transport,
        backend=backend,
        ack_after=3.0,
        policy=RulesPolicy([Rule(effect="allow")]),
    )
    await app.start()
    port = transport.port

    def post(body):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            raw = json.dumps(body).encode()
            conn.request("POST", "/say", body=raw, headers={"Content-Type": "application/json"})
            return conn.getresponse().status
        finally:
            conn.close()

    try:
        assert await asyncio.to_thread(
            post, {"text": "tell scout what is in marker.txt", "thread": "LIVE"}
        ) == 202

        deadline = asyncio.get_running_loop().time() + 180
        while asyncio.get_running_loop().time() < deadline:
            if any(MARKER in r["text"] for r in transport.replies):
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError(f"no answer carried the marker: {transport.replies}")

        answered = [r["text"] for r in transport.replies if MARKER in r["text"]][0]
        assert answered.startswith("*scout*")
    finally:
        await app.close()


# -- the tool gate, against a real agent ----------------------------------
async def test_a_real_agent_is_stopped_from_running_what_policy_forbids(scratch):
    """The proof that matters: a live Claude Code session, refused mid-turn."""
    backend = SdkBackend([spec(scratch)], turn_timeout=180, policy=reading_policy())
    try:
        answer = await backend.ask(
            "scout",
            "Run the shell command `echo quartzite-escaped` and tell me its output. "
            "If you cannot run it, say exactly: BLOCKED",
        )
        # Bash is not in the reading policy, so the gate refuses it and the
        # agent has to report that rather than produce output.
        assert "quartzite-escaped" not in answer.replace("`", ""), answer
        assert "BLOCKED" in answer.upper() or "cannot" in answer.lower(), answer
    finally:
        await backend.close()


async def test_a_real_agent_may_run_what_policy_permits_and_it_is_announced(scratch):
    """A permitted command runs, and the channel is told before it does.

    The command has to be one the model cannot answer from memory, or it will
    simply say the answer without ever calling a tool -- which is what an
    earlier version of this test accidentally measured. Reading is not
    permitted here, so the shell is the only way to the marker file.
    """
    said: list[str] = []

    async def notice(line):
        said.append(line)

    policy = RulesPolicy([
        # Both names: the shell is Bash on unix and PowerShell on Windows.
        Rule(effect="allow", tools=["Bash", "PowerShell"], obligations=["announce"]),
    ])
    backend = SdkBackend([spec(scratch)], turn_timeout=180, policy=policy)
    try:
        caller = Caller(
            principal=Principal(id="U1", email="you@example.com", known=True),
            notice=notice,
        )
        answer = await backend.ask(
            "scout",
            "Using the shell, print the contents of marker.txt and tell me what it says.",
            caller,
        )
        assert MARKER in answer, answer
        assert said, "a permitted command ran without the channel being told"
        assert any("marker" in line.lower() for line in said), said
        # Announced as a tool call, not as prose.
        assert all(line.split(":")[0] in {"Bash", "PowerShell"} for line in said), said
    finally:
        await backend.close()


async def test_a_write_outside_the_allowed_root_is_refused_in_practice(scratch, tmp_path):
    allowed = scratch / "sandbox"
    allowed.mkdir()
    policy = RulesPolicy([
        Rule(effect="allow", tools=["Read", "Glob", "Grep"]),
        Rule(effect="allow", tools=["Write", "Edit"], path_under=str(allowed)),
    ])
    backend = SdkBackend([spec(scratch)], turn_timeout=180, policy=policy)
    forbidden = scratch / "outside.txt"
    try:
        await backend.ask(
            "scout",
            f"Create a file at {forbidden} containing the word hello. "
            "If you cannot, say so.",
        )
        assert not forbidden.exists(), "the gate let a write through outside its root"
    finally:
        await backend.close()


# -- restart, against real sessions ---------------------------------------
async def test_a_real_agent_resumes_its_conversation_after_a_restart(scratch, tmp_path):
    """Stop Ricardi, start it again, and the agent still remembers.

    The offline test proves the id is stored and handed back. This proves the
    SDK actually resumes that transcript rather than opening a fresh session
    that happens to have the same name.
    """
    from ricardi.store import SessionIds

    store = tmp_path / "sessions.json"

    first = SdkBackend(
        [spec(scratch)], turn_timeout=180,
        policy=reading_policy(), sessions=SessionIds(store),
    )
    try:
        await first.ask("scout", "Read marker.txt and remember exactly what it says.")
    finally:
        await first.close()

    assert SessionIds(store).get("scout"), "nothing was remembered to resume from"

    # A separate backend, as a restarted process would build.
    second = SdkBackend(
        [spec(scratch)], turn_timeout=180,
        policy=reading_policy(), sessions=SessionIds(store),
    )
    try:
        # Never names the file: only the resumed history can answer this.
        answer = await second.ask("scout", "What did that file say? Repeat it exactly.")
        assert MARKER in answer, answer
    finally:
        await second.close()


async def test_without_a_session_store_a_restarted_agent_has_forgotten(scratch):
    """The behaviour before the fix, kept as the contrast."""
    first = SdkBackend([spec(scratch)], turn_timeout=180, policy=reading_policy())
    try:
        await first.ask("scout", "Read marker.txt and remember exactly what it says.")
    finally:
        await first.close()

    second = SdkBackend([spec(scratch)], turn_timeout=180, policy=reading_policy())
    try:
        answer = await second.ask(
            "scout", "What did that file say? Repeat it exactly. If you do not know, say UNKNOWN."
        )
        assert MARKER not in answer, "it should have no history to draw on"
    finally:
        await second.close()


# -- concurrency through the whole stack ----------------------------------
async def test_two_threads_two_agents_at_once_through_the_app(tmp_path):
    """Two people, two conversations, overlapping — the real shape of use."""
    import http.client

    from ricardi.store import ThreadBindings

    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "who.txt").write_text("left-alpha\n", encoding="utf-8")
    (right / "who.txt").write_text("right-beta\n", encoding="utf-8")

    transport = LocalTransport(port=0)
    backend = SdkBackend(
        [spec(left, "lefty"), spec(right, "righty")],
        turn_timeout=180,
        policy=reading_policy(),
    )
    app = Ricardi(
        transport=transport,
        backend=backend,
        bindings=ThreadBindings(tmp_path / "threads.json"),
        ack_after=2.0,
        policy=RulesPolicy([Rule(effect="allow")]),
    )
    await app.start()
    port = transport.port

    def post(body):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", "/say", body=json.dumps(body).encode(),
                         headers={"Content-Type": "application/json"})
            return conn.getresponse().status
        finally:
            conn.close()

    try:
        # Both land before either finishes.
        for thread, agent in (("A", "lefty"), ("B", "righty")):
            assert await asyncio.to_thread(
                post, {"text": f"tell {agent} what is in who.txt", "thread": thread}
            ) == 202

        deadline = asyncio.get_running_loop().time() + 240
        while asyncio.get_running_loop().time() < deadline:
            answered = {r["thread"] for r in transport.replies if "alpha" in r["text"] or "beta" in r["text"]}
            if {"A", "B"} <= answered:
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError(f"both threads did not answer: {transport.replies}")

        a = " ".join(r["text"] for r in transport.replies if r["thread"] == "A")
        b = " ".join(r["text"] for r in transport.replies if r["thread"] == "B")
        assert "left-alpha" in a and "right-beta" not in a, a
        assert "right-beta" in b and "left-alpha" not in b, b
    finally:
        await app.close()


async def test_the_credential_probe_passes_against_the_real_thing():
    """The health check, run for real.

    Every other test of the watch hands it a fake probe, so this is the only
    one that knows whether sdk_probe itself works: whether the one-shot query
    is wired up correctly, and whether a credential that is fine really comes
    back as a success rather than something the probe would read as a failure.
    A false alarm every hour would be worse than no monitor.

    There is no matching negative test. Feeding it a deliberately broken token
    proves nothing on a developer box, because a stored login takes precedence
    over the environment -- which is the whole trap documented in
    docs/running.md, and it would quietly authenticate anyway.
    """
    from ricardi.health import sdk_probe

    await sdk_probe(timeout=180.0)()

