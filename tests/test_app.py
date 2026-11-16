"""One turn at a time, with a fake agent behind it."""

import asyncio

import pytest

from conftest import RendersEvents, open_policy

from ricardi.app import Ricardi
from ricardi.backends.base import Agent
from ricardi.backends.fake import FakeBackend
from ricardi.store import ThreadBindings
from ricardi.transports.base import Inbound


class Recorder(RendersEvents):
    """A transport that remembers instead of sending."""

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, str, str]] = []

    async def start(self, on_message):
        self.on_message = on_message

    async def send(self, channel, thread, text):
        self.sent.append((channel, thread, text))

    async def close(self):
        return None

    @property
    def last(self) -> str:
        return self.sent[-1][2] if self.sent else ""


def message(text, thread="t1"):
    return Inbound(text=text, thread=thread, channel="c1", user="u1")


@pytest.fixture
def wired():
    transport = Recorder()
    backend = FakeBackend()
    app = Ricardi(transport=transport, backend=backend, ack_after=30.0, policy=open_policy())
    return app, transport, backend


async def test_list_names_every_agent(wired):
    app, transport, _ = wired
    await app.handle(message("list agents"))
    assert "scout" in transport.last
    assert "deployer" in transport.last
    assert "2 agents" in transport.last


async def test_help_is_offered_for_an_empty_mention(wired):
    app, transport, _ = wired
    await app.handle(message(""))
    assert "list agents" in transport.last


async def test_tell_reaches_the_named_agent(wired):
    app, transport, backend = wired
    await app.handle(message("tell scout to read auth.py"))
    assert backend.asked == [("scout", "read auth.py")]
    assert "read auth.py" in transport.last


async def test_a_thread_keeps_talking_to_the_same_agent(wired):
    app, transport, backend = wired
    await app.handle(message("tell scout to read auth.py"))
    await app.handle(message("and now the tests"))
    assert backend.asked == [("scout", "read auth.py"), ("scout", "and now the tests")]


async def test_threads_do_not_bleed_into_each_other(wired):
    app, _, backend = wired
    await app.handle(message("tell scout to read auth.py", thread="t1"))
    await app.handle(message("tell deployer to ship it", thread="t2"))
    await app.handle(message("carry on", thread="t1"))
    assert backend.asked[-1] == ("scout", "carry on")


async def test_an_unbound_thread_is_told_how_to_address_someone(wired):
    app, transport, backend = wired
    await app.handle(message("deploy PR 412"))
    assert backend.asked == []
    assert "tell <agent>" in transport.last
    # The listing comes with it, so the next message can be right.
    assert "scout" in transport.last


async def test_an_unknown_agent_says_who_is_actually_there(wired):
    app, transport, backend = wired
    await app.handle(message("tell nobody to do a thing"))
    assert backend.asked == []
    assert "nobody" in transport.last
    assert "scout" in transport.last


async def test_who_reports_the_binding(wired):
    app, transport, _ = wired
    await app.handle(message("who"))
    assert "not talking to anyone" in transport.last
    await app.handle(message("tell scout"))
    await app.handle(message("who"))
    assert "scout" in transport.last


async def test_tell_with_no_message_only_binds(wired):
    app, transport, backend = wired
    await app.handle(message("tell scout"))
    assert backend.asked == []
    assert "scout" in transport.last
    await app.handle(message("now do it"))
    assert backend.asked == [("scout", "now do it")]


async def test_a_slow_agent_gets_an_interim_word(wired):
    transport = Recorder()
    backend = FakeBackend(delay=0.2)
    app = Ricardi(transport=transport, backend=backend, ack_after=0.01, policy=open_policy())
    await app.handle(message("tell scout to take its time"))
    assert len(transport.sent) == 2
    assert "Still working" in transport.sent[0][2]
    assert "take its time" in transport.sent[1][2]


async def test_a_fast_agent_is_not_announced(wired):
    app, transport, _ = wired
    await app.handle(message("tell scout to be quick"))
    assert len(transport.sent) == 1


async def test_a_backend_that_breaks_becomes_words_not_a_traceback():
    class Broken(FakeBackend):
        async def ask(self, name, text):
            raise RuntimeError("the session died")

    transport = Recorder()
    app = Ricardi(transport=transport, backend=Broken(), ack_after=30.0, policy=open_policy())
    await app.handle(message("tell scout to do a thing"))
    assert transport.sent, "the human must hear something"
    assert "broke" in transport.last.lower()


async def test_bindings_outlive_a_restart(tmp_path):
    path = tmp_path / "threads.json"
    first = Ricardi(
        transport=Recorder(),
        backend=FakeBackend(),
        bindings=ThreadBindings(path),
        ack_after=30.0,
        policy=open_policy(),
    )
    await first.handle(message("tell deployer to ship it"))

    transport = Recorder()
    backend = FakeBackend()
    second = Ricardi(
        transport=transport,
        backend=backend,
        bindings=ThreadBindings(path),
        ack_after=30.0,
        policy=open_policy(),
    )
    await second.handle(message("and again"))
    assert backend.asked == [("deployer", "and again")]


async def test_an_agent_name_is_matched_however_it_was_typed():
    transport = Recorder()
    backend = FakeBackend([Agent(name="DeployBot")])
    app = Ricardi(transport=transport, backend=backend, ack_after=30.0, policy=open_policy())
    await app.handle(message("tell deploybot to ship it"))
    # Resolved to the catalogue's spelling, which is what the backend knows.
    assert backend.asked == [("DeployBot", "ship it")]


# -- the inbound enforcement point ----------------------------------------
def closed_policy():
    """Permits listing, nothing else -- enough to tell the two paths apart."""
    from ricardi.policy import Rule, RulesPolicy

    return RulesPolicy(
        rules=[Rule(effect="allow", actions=["list"])],
        roster={"u1": "tester@example.com"},
    )


class PrivateRecorder(Recorder):
    def __init__(self):
        super().__init__()
        self.private: list[tuple[str, str, str]] = []

    async def send_private(self, channel, thread, user, text):
        self.private.append((thread, user, text))


async def test_a_refused_request_never_reaches_the_agent():
    transport = PrivateRecorder()
    backend = FakeBackend()
    app = Ricardi(
        transport=transport,
        backend=backend,
        ack_after=30.0,
        policy=closed_policy(),
    )
    await app.handle(message("tell scout to read auth.py"))
    assert backend.asked == []


async def test_a_refusal_is_private_not_posted_to_the_channel():
    transport = PrivateRecorder()
    app = Ricardi(
        transport=transport,
        backend=FakeBackend(),
        ack_after=30.0,
        policy=closed_policy(),
    )
    await app.handle(message("tell scout to read auth.py"))
    assert transport.private, "the person who asked must be told"
    assert not transport.sent, "the channel must not be"


async def test_a_refusal_falls_back_to_the_channel_when_private_is_unavailable():
    transport = Recorder()  # no send_private
    app = Ricardi(
        transport=transport,
        backend=FakeBackend(),
        ack_after=30.0,
        policy=closed_policy(),
    )
    await app.handle(message("tell scout to read auth.py"))
    assert transport.sent, "silence would look like a crash"


async def test_a_refused_tell_does_not_bind_the_thread():
    transport = PrivateRecorder()
    backend = FakeBackend()
    app = Ricardi(
        transport=transport,
        backend=backend,
        ack_after=30.0,
        policy=closed_policy(),
    )
    await app.handle(message("tell scout to read auth.py"))
    # If the refusal had bound the thread, this bare message would be relayed.
    await app.handle(message("and now the tests"))
    assert backend.asked == []


async def test_a_policy_that_raises_denies_rather_than_permits():
    class Exploding:
        async def decide(self, request):
            raise RuntimeError("the decision service is down")

    transport = PrivateRecorder()
    backend = FakeBackend()
    app = Ricardi(
        transport=transport,
        backend=backend,
        ack_after=30.0,
        policy=Exploding(),
    )
    await app.handle(message("tell scout to read auth.py"))
    assert backend.asked == []
    assert transport.private, "and the person is told why nothing happened"


async def test_with_no_policy_at_all_nothing_is_permitted():
    transport = PrivateRecorder()
    backend = FakeBackend()
    app = Ricardi(transport=transport, backend=backend, ack_after=30.0)
    await app.handle(message("tell scout to read auth.py"))
    assert backend.asked == []


async def test_a_refusal_to_a_new_thread_goes_to_the_channel():
    """Regression: it was delivered into a thread nobody had open.

    Correct, instant, and invisible -- which on someone's first message looks
    exactly like a bot that is down.
    """
    transport = PrivateRecorder()
    app = Ricardi(
        transport=transport,
        backend=FakeBackend(),
        ack_after=30.0,
        policy=closed_policy(),
    )
    await app.handle(
        Inbound(text="tell scout hi", thread="T1", channel="C1", user="u1", in_thread=False)
    )
    assert transport.private, "the person must be told"
    thread, user, _ = transport.private[-1]
    assert thread == "", "a refusal in an unopened thread is never seen"
    assert user == "u1"


async def test_a_refusal_inside_an_open_thread_stays_there():
    # They are reading that thread, so the reply belongs with the message.
    transport = PrivateRecorder()
    app = Ricardi(
        transport=transport,
        backend=FakeBackend(),
        ack_after=30.0,
        policy=closed_policy(),
    )
    await app.handle(
        Inbound(text="tell scout hi", thread="T1", channel="C1", user="u1", in_thread=True)
    )
    thread, _, _ = transport.private[-1]
    assert thread == "T1"


# -- an inbound refusal reaches the stream ---------------------------------
from ricardi.events import Recorder as EventRecorder, Refused  # noqa: E402


def _named_denial():
    # A request no rule matches is denied with rule "" (the default). A named
    # deny is what gives the event a rule to carry and the message one to leak.
    from ricardi.policy import Rule, RulesPolicy

    return RulesPolicy(
        rules=[Rule(effect="deny", name="no asking here", actions=["ask"])],
        roster={"u1": "tester@example.com"},
    )


def _grant_policy():
    from ricardi.policy import Rule, RulesPolicy

    return RulesPolicy(
        rules=[Rule(effect="allow", actions=["list"])],
        roster={"u1": "tester@example.com", "U2": "someone@example.com"},
    )


async def test_a_refused_request_emits_the_decision_not_just_the_reason():
    # _permit discards the Decision today, so rule and tier cannot reach a
    # transport. The refused person still sees only the reason.
    sink = EventRecorder()
    app = Ricardi(transport=PrivateRecorder(), backend=FakeBackend(), ack_after=30.0,
                  policy=_named_denial(), sink=sink)
    await app.handle(message("tell scout to read auth.py"))
    event = sink.events[-1]
    refused = event.body
    assert isinstance(refused, Refused)
    assert refused.rule and refused.tier == ""
    assert refused.target == ""
    assert event.envelope.name == "scout"


async def test_a_refusal_naming_no_agent_uses_the_empty_name():
    from ricardi.policy import RulesPolicy

    sink = EventRecorder()
    policy = RulesPolicy(rules=[], roster={"u1": "tester@example.com"})
    app = Ricardi(transport=PrivateRecorder(), backend=FakeBackend(), ack_after=30.0,
                  policy=policy, sink=sink)
    await app.handle(message("list agents"))
    assert sink.events[-1].envelope.name == ""
    assert isinstance(sink.events[-1].body, Refused)


async def test_streams_are_per_name_so_sequences_do_not_cross():
    sink = EventRecorder()
    app = Ricardi(transport=PrivateRecorder(), backend=FakeBackend(), ack_after=30.0,
                  policy=closed_policy(), sink=sink)
    await app.handle(message("tell scout to read auth.py"))
    await app.handle(message("tell deployer to ship it", thread="t2"))
    await app.handle(message("tell scout to read b.py", thread="t3"))
    seqs = [(e.envelope.name, e.envelope.seq) for e in sink.events]
    assert seqs == [("scout", 1), ("deployer", 1), ("scout", 2)]


async def test_a_refused_grant_carries_who_it_was_aimed_at():
    sink = EventRecorder()
    app = Ricardi(transport=PrivateRecorder(), backend=FakeBackend(), ack_after=30.0,
                  policy=_grant_policy(), sink=sink)
    await app.handle(message("grant <@U2> read on scout"))
    assert sink.events[-1].body.target == "someone@example.com"


async def test_the_person_still_sees_the_reason_and_not_the_rule():
    sink = EventRecorder()
    transport = PrivateRecorder()
    app = Ricardi(transport=transport, backend=FakeBackend(), ack_after=30.0,
                  policy=_named_denial(), sink=sink)
    await app.handle(message("tell scout to read auth.py"))
    refused = sink.events[-1].body
    assert refused.reason and transport.private[-1][2] == refused.reason
    assert refused.rule == "no asking here"
    assert refused.rule not in transport.private[-1][2]


async def test_a_policy_failure_emits_a_refusal_and_still_refuses():
    class Exploding:
        async def decide(self, request):
            raise RuntimeError("down")

    sink = EventRecorder()
    transport = PrivateRecorder()
    backend = FakeBackend()
    app = Ricardi(transport=transport, backend=backend, ack_after=30.0,
                  policy=Exploding(), sink=sink)
    await app.handle(message("tell scout to read auth.py"))
    assert sink.events[-1].body.rule == "policy error"
    assert backend.asked == [] and transport.private


async def test_an_inbound_refusal_reaches_the_person_and_not_the_channel():
    # The transport is the sink now, so the refusal is an event like everything
    # else -- and the Slack renderer stays quiet about an inbound one, which is
    # what keeps the channel from being told that someone was turned away.
    transport = PrivateRecorder()
    app = Ricardi(transport=transport, backend=FakeBackend(), ack_after=30.0,
                  policy=closed_policy())
    await app.handle(message("tell scout to read auth.py"))
    assert transport.private
    assert isinstance(transport.emitted[-1].body, Refused)
    assert not transport.sent, "the channel must not hear about it"


async def test_a_default_deny_emits_the_same_words_the_audit_log_uses():
    # No rule matched, so the decision's rule is "". The stream says so in the
    # audit log's words rather than going blank on the commonest refusal.
    sink = EventRecorder()
    app = Ricardi(transport=PrivateRecorder(), backend=FakeBackend(), ack_after=30.0,
                  policy=closed_policy(), sink=sink)
    await app.handle(message("tell scout to read auth.py"))
    assert sink.events[-1].body.rule == "default deny"


# -- everything Ricardi says is an event -----------------------------------
from ricardi import router  # noqa: E402
from ricardi.events import Event  # noqa: E402
from ricardi.render import render  # noqa: E402


class Silent(FakeBackend):
    """An agent that finishes a turn without saying anything.

    The shape `sdk.py` produces for the same turn: a turn that opened and
    closed with no `Said` between them, and an empty answer.
    """

    def _answer(self, name: str, text: str) -> str:
        return ""


def shown(transport) -> list[str]:
    return [text for _, _, text in transport.sent]


async def test_everything_ricardi_says_goes_out_as_an_event(wired):
    app, transport, _ = wired
    await app.handle(message("tell scout to read auth.py"))
    assert all(isinstance(c, Event) for c in transport.emitted)
    # The half that could actually be false: nothing reaches the channel
    # except by being rendered from one of those events.
    assert shown(transport) == [t for e in transport.emitted if (t := render(e))]
    assert shown(transport), "and it did say something"


async def test_a_relayed_turn_shares_one_turn_id_end_to_end():
    # Slow enough that Ricardi's own notice lands inside the turn: the point
    # is that her word and the agent's carry the same id.
    transport = Recorder()
    app = Ricardi(transport=transport, backend=FakeBackend(delay=0.2),
                  ack_after=0.01, policy=open_policy())
    await app.handle(message("tell scout to take its time"))
    ids = {e.envelope.turn for e in transport.emitted}
    assert len(ids) == 1 and ids != {""}
    assert any(e.envelope.actor == "ricardi" for e in transport.emitted)


async def test_sequence_numbers_continue_across_two_turns_in_one_name(wired):
    app, transport, _ = wired
    await app.handle(message("tell scout to read auth.py"))
    await app.handle(message("and now the tests"))
    # Three events to a turn -- it started, it said one thing, it ended -- and
    # the second turn carries on counting rather than starting over.
    assert [e.envelope.seq for e in transport.emitted] == [1, 2, 3, 4, 5, 6]


async def test_the_help_text_still_reaches_slack_unchanged(wired):
    app, transport, _ = wired
    await app.handle(message(""))
    assert router.HELP in "".join(render(e) for e in transport.emitted)
    assert transport.last.startswith(router.HELP), "and with nothing in front of it"


async def test_the_still_working_notice_is_an_event_too():
    transport = Recorder()
    app = Ricardi(transport=transport, backend=FakeBackend(delay=0.2),
                  ack_after=0.01, policy=open_policy())
    await app.handle(message("tell scout to take its time"))
    assert any("Still working" in render(e) for e in transport.emitted)


async def test_an_answer_reaches_the_channel_exactly_once(wired):
    # The agent's answer is the Said the backend emitted. A second one from
    # the app would print every reply twice.
    app, transport, _ = wired
    await app.handle(message("tell scout to read auth.py"))
    assert len([t for t in shown(transport) if "read auth.py" in t]) == 1


async def test_a_silent_turn_still_says_one_thing_in_the_channel():
    # An agent that says nothing must not be indistinguishable from a lost
    # turn -- in the channel, not only in the stream.
    transport = Recorder()
    app = Ricardi(transport=transport, backend=Silent(), ack_after=30.0,
                  policy=open_policy())
    await app.handle(message("tell scout to say nothing"))
    assert len(shown(transport)) == 1
    assert "without saying anything" in transport.last


async def test_a_relayed_caller_carries_the_turn_and_the_stream():
    class Spy(FakeBackend):
        def __init__(self):
            super().__init__()
            self.callers = []

        async def ask(self, name, text, caller=None):
            self.callers.append(caller)
            return await super().ask(name, text, caller)

    transport = Recorder()
    backend = Spy()
    app = Ricardi(transport=transport, backend=backend, ack_after=30.0,
                  policy=open_policy())
    await app.handle(message("tell scout to read auth.py"))
    caller = backend.callers[-1]
    assert caller.turn and caller.events is app._stream_for("scout")
    # No notice: the channel renders ToolStarted now, and announcing through
    # the caller as well would post the same line for the same call twice.
    assert caller.notice is None


# -- two conversations with one agent, at the same time --------------------
async def test_two_threads_on_one_agent_do_not_cross():
    # Regression: one route per *name* meant the second message rebound it
    # while the first turn was still streaming, so the first thread went
    # silent and the second was shown someone else's answer.
    transport = Recorder()
    app = Ricardi(transport=transport, backend=FakeBackend(delay=0.2),
                  ack_after=30.0, policy=open_policy())
    await asyncio.gather(
        app.handle(message("tell scout to read auth.py", thread="t1")),
        app.handle(message("tell scout to ship it", thread="t2")),
    )
    where = {text: thread for _, thread, text in transport.sent}
    assert where == {"*scout*\n[scout] read auth.py": "t1",
                     "*scout*\n[scout] ship it": "t2"}


async def test_a_message_in_another_thread_does_not_divert_a_running_turn():
    # The same bug without a second turn: any word Ricardi says about an agent
    # used to rebind that agent's one route, so a `who` in the thread next
    # door took the rest of a running turn with it.
    transport = Recorder()
    app = Ricardi(transport=transport, backend=FakeBackend(delay=0.2),
                  ack_after=30.0, policy=open_policy())
    await app.handle(message("tell scout hello", thread="t2"))
    running = asyncio.create_task(
        app.handle(message("tell scout to take its time", thread="t1"))
    )
    await asyncio.sleep(0.05)
    await app.handle(message("who", thread="t2"))
    await running
    landed = [(thread, text) for _, thread, text in transport.sent]
    assert ("t1", "*scout*\n[scout] take its time") in landed
    assert ("t2", "This thread is talking to *scout*.") in landed


async def test_a_finished_message_leaves_no_route_behind(wired):
    # One route per message is one entry per message; a table nobody clears
    # is a leak in a process meant to run for months.
    app, transport, _ = wired
    await app.handle(message("tell scout to read auth.py"))
    await app.handle(message("list agents"))
    assert transport._routes == {}
