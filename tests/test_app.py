"""One turn at a time, with a fake agent behind it."""

import pytest

from conftest import open_policy

from ricardi.app import Ricardi
from ricardi.backends.base import Agent
from ricardi.backends.fake import FakeBackend
from ricardi.store import ThreadBindings
from ricardi.transports.base import Inbound


class Recorder:
    """A transport that remembers instead of sending."""

    def __init__(self):
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
