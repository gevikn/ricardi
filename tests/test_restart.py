"""What survives Ricardi stopping.

Agents are child processes, so stopping Ricardi stops them. Two engineers mid
-conversation will hit this constantly — a reboot, a deploy, a closed terminal
— and what they expect is to carry on talking, not to find the agent has
forgotten the last hour.

The gap these were written for: `session_id` lived only in memory, so every
restart silently started each agent over with no history. The thread binding
survived, so it looked like the same conversation and was not.
"""

import asyncio
import json

import pytest

from conftest import open_policy
from ricardi.app import Ricardi
from ricardi.backends.base import Caller, Agent
from ricardi.backends.fake import FakeBackend
from ricardi.backends.sdk import SdkBackend
from ricardi.config import AgentSpec
from ricardi.store import SessionIds, ThreadBindings
from ricardi.transports.base import Inbound


class Recorder:
    def __init__(self):
        self.sent = []

    async def start(self, on_message):
        self.on_message = on_message

    async def send(self, channel, thread, text):
        self.sent.append(text)

    async def close(self):
        return None


def message(text, thread="T1"):
    return Inbound(text=text, thread=thread, channel="C1", user="u1")


# -- the session store ----------------------------------------------------
def test_a_session_id_is_remembered(tmp_path):
    path = tmp_path / "sessions.json"
    SessionIds(path).remember("scout", "abc-123")
    assert SessionIds(path).get("scout") == "abc-123"


def test_an_unknown_agent_has_no_session(tmp_path):
    assert SessionIds(tmp_path / "sessions.json").get("nobody") == ""


def test_a_newer_session_replaces_the_old_one(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionIds(path)
    store.remember("scout", "first")
    store.remember("scout", "second")
    assert SessionIds(path).get("scout") == "second"


def test_an_empty_session_id_is_ignored(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionIds(path)
    store.remember("scout", "real")
    store.remember("scout", "")
    assert SessionIds(path).get("scout") == "real"


def test_forgetting_an_agent_clears_it(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionIds(path)
    store.remember("scout", "abc")
    store.forget("scout")
    assert SessionIds(path).get("scout") == ""


def test_a_corrupt_file_costs_memory_not_the_ability_to_answer(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{not json", encoding="utf-8")
    store = SessionIds(path)
    assert store.get("scout") == ""
    # And it recovers: the next write replaces the broken file.
    store.remember("scout", "abc")
    assert SessionIds(path).get("scout") == "abc"


def test_the_file_is_replaced_atomically(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionIds(path)
    store.remember("scout", "abc")
    store.remember("bench", "def")
    # No temporary files left behind, and the result parses.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sessions.json"]
    assert json.loads(path.read_text(encoding="utf-8")) == {"bench": "def", "scout": "abc"}


def test_no_path_keeps_it_in_memory(tmp_path):
    store = SessionIds(None)
    store.remember("scout", "abc")
    assert store.get("scout") == "abc"


# -- the backend restores them -------------------------------------------
class Stubbed(SdkBackend):
    """Records what `resume` the real client would have been built with."""

    def __init__(self, specs, result_session="", **kw):
        super().__init__(specs, **kw)
        self.resumed_with = None
        self._result_session = result_session

    async def _connect(self, live):
        self.resumed_with = live.session_id
        return object()

    async def _one_turn(self, live, client, text):
        if self._result_session:
            live.session_id = self._result_session
            self._sessions.remember(live.spec.name, self._result_session)
        return "ok"


def spec(name="scout"):
    return AgentSpec(name=name, cwd=".", description="stub")


async def test_a_turn_records_the_session_id(tmp_path):
    path = tmp_path / "sessions.json"
    backend = Stubbed([spec()], result_session="sess-1", sessions=SessionIds(path))
    try:
        await backend.ask("scout", "hello")
    finally:
        await backend.close()
    assert SessionIds(path).get("scout") == "sess-1"


async def test_a_restart_resumes_the_same_conversation(tmp_path):
    """The regression: this is what was silently not happening."""
    path = tmp_path / "sessions.json"

    first = Stubbed([spec()], result_session="sess-1", sessions=SessionIds(path))
    try:
        await first.ask("scout", "remember this")
    finally:
        await first.close()

    # A whole new process would build a new store from the same file.
    second = Stubbed([spec()], sessions=SessionIds(path))
    try:
        await second.ask("scout", "still there?")
    finally:
        await second.close()

    assert second.resumed_with == "sess-1", "the agent started over instead of resuming"


async def test_without_a_store_a_restart_starts_over(tmp_path):
    # Documents the in-memory default rather than hiding it.
    backend = Stubbed([spec()], result_session="sess-1")
    try:
        await backend.ask("scout", "hello")
    finally:
        await backend.close()
    assert Stubbed([spec()]).resumed_with is None


async def test_each_agent_keeps_its_own_session(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionIds(path)
    store.remember("scout", "sess-scout")
    store.remember("bench", "sess-bench")

    backend = Stubbed([spec("scout"), spec("bench")], sessions=SessionIds(path))
    try:
        await backend.ask("bench", "hello")
        assert backend.resumed_with == "sess-bench"
    finally:
        await backend.close()


# -- the thread binding survives too -------------------------------------
async def test_a_thread_still_knows_its_agent_after_a_restart(tmp_path):
    path = tmp_path / "threads.json"

    first = Ricardi(
        transport=Recorder(),
        backend=FakeBackend(),
        bindings=ThreadBindings(path),
        ack_after=30.0,
        policy=open_policy(),
    )
    await first.handle(message("tell deployer to ship it"))

    backend = FakeBackend([Agent(name="deployer")])
    second = Ricardi(
        transport=Recorder(),
        backend=backend,
        bindings=ThreadBindings(path),
        ack_after=30.0,
        policy=open_policy(),
    )
    await second.handle(message("and again"))
    assert backend.asked == [("deployer", "and again")]


async def test_both_halves_are_needed_for_the_conversation_to_continue(tmp_path):
    """The binding alone is not continuity.

    A surviving binding routes the next message to the right agent; only the
    session id makes that agent remember what was said. Losing either one looks
    the same from Slack, which is why this was invisible.
    """
    threads = tmp_path / "threads.json"
    sessions = tmp_path / "sessions.json"

    ThreadBindings(threads).bind("T1", "scout")
    SessionIds(sessions).remember("scout", "sess-1")

    assert ThreadBindings(threads).get("T1") == "scout"
    assert SessionIds(sessions).get("scout") == "sess-1"
