"""Request flags: what the asker can take away, and never add.

A flag narrows what policy already allows. Nothing here may ever widen it --
that invariant is what lets the flag namespace grow without growing the attack
surface, so it is asserted directly rather than implied.
"""

import pytest

from ricardi import router


@pytest.mark.parametrize(
    "text,flags,agent,body",
    [
        ("-ro tell scout to look at auth.py", ("ro",), "scout", "look at auth.py"),
        ("-dry tell scout to fix it", ("dry",), "scout", "fix it"),
        ("-ro -dry tell scout to fix it", ("dry", "ro"), "scout", "fix it"),
        ("-ro scout: what changed", ("ro",), "scout", "what changed"),
        ("-RO tell scout to look", ("ro",), "scout", "look"),
    ],
)
def test_leading_flags_come_off_before_the_agent_is_named(text, flags, agent, body):
    cmd = router.parse(text)
    assert cmd.flags == flags
    assert cmd.agent == agent
    assert cmd.text == body


def test_a_flag_on_a_bare_message_still_parses_as_say():
    cmd = router.parse("-ro and what about the other one")
    assert cmd.kind == "say"
    assert cmd.flags == ("ro",)
    assert cmd.text == "and what about the other one"


@pytest.mark.parametrize(
    "text",
    [
        "tell scout to run pytest --cov -x",
        "tell scout about the -ro flag",
        "scout: why does ls -a work",
    ],
)
def test_a_hyphen_inside_the_message_is_not_a_flag(text):
    assert router.parse(text).flags == ()


def test_an_unknown_flag_is_refused_rather_than_ignored():
    # Silently dropping "-dyr" would run the thing the person was trying to
    # avoid. A flag that does not restrict must not look like one that does.
    cmd = router.parse("-dyr tell scout to fix it")
    assert cmd.kind == "bad_flag"
    assert "dyr" in cmd.text


# -- the deny pass --------------------------------------------------------
from ricardi import policy as P  # noqa: E402


def _tool(name="Bash", flags=(), **tool_input):
    return P.Request(
        action="tool",
        principal=P.Principal(id="U1", email="you@example.com", known=True),
        agent="repo",
        tool=name,
        tool_input=tool_input,
        flags=tuple(flags),
    )


def _permissive():
    """A policy that allows everything, so only the flag can say no."""
    return P.RulesPolicy([P.Rule(effect="allow", name="allow anything")])


async def test_ro_refuses_a_tool_the_rules_allowed():
    d = await _permissive().decide(_tool("Bash", flags=("ro",), command="ls"))
    assert d.allow is False
    assert "-ro" in d.reason


async def test_ro_leaves_reading_alone():
    d = await _permissive().decide(_tool("Read", flags=("ro",), file_path="/x"))
    assert d.allow is True


async def test_ro_refuses_a_tool_it_has_never_heard_of():
    # An unknown tool under -ro must be treated as though it changes things.
    # Anything else means a new tool silently escapes the restriction.
    d = await _permissive().decide(_tool("SomeNewTool", flags=("ro",)))
    assert d.allow is False


async def test_ro_does_not_stop_you_addressing_an_agent():
    pol = P.RulesPolicy([P.Rule(effect="allow", name="may ask", actions=["ask"])])
    req = P.Request(
        action="ask",
        principal=P.Principal(id="U1", email="you@example.com", known=True),
        agent="repo",
        flags=("ro",),
    )
    assert (await pol.decide(req)).allow is True


async def test_the_decision_says_which_flag_refused_it():
    # The audit log answers "why was this refused?" from this field.
    d = await _permissive().decide(_tool("Write", flags=("ro",), file_path="/x"))
    assert "ro" in d.rule


@pytest.mark.parametrize("flag", sorted(router.FLAGS))
async def test_no_flag_can_turn_a_refusal_into_a_permission(flag):
    """The invariant the whole design rests on.

    A flag narrows. If one could ever widen, the flag namespace would be an
    escalation surface -- and it is a namespace meant to grow.
    """
    nothing_allowed = P.RulesPolicy()
    for tool_name in ("Bash", "Read", "Write", "Edit", "SomeNewTool"):
        bare = await nothing_allowed.decide(_tool(tool_name))
        flagged = await nothing_allowed.decide(_tool(tool_name, flags=(flag,)))
        assert bare.allow is False
        assert flagged.allow is False, f"-{flag} granted {tool_name}"


async def test_dry_refuses_to_run_but_says_what_it_would_have_run():
    d = await _permissive().decide(_tool("Bash", flags=("dry",), command="pytest -x"))
    assert d.allow is False
    assert "pytest -x" in d.reason


# -- sticking to the thread -----------------------------------------------
import json  # noqa: E402

from ricardi.store import ThreadBindings  # noqa: E402


def test_flags_set_on_the_opening_message_hold_for_the_thread():
    # The situation this exists for: you open a thread -ro and walk away, and
    # a collaborator replies in it without the flag. The restriction must not
    # evaporate just because the next person did not repeat it.
    b = ThreadBindings(None)
    b.bind("t1", "repo", flags=("ro",))
    assert b.flags("t1") == ("ro",)


def test_an_unflagged_reply_does_not_lift_the_restriction():
    b = ThreadBindings(None)
    b.bind("t1", "repo", flags=("ro",))
    b.bind("t1", "repo")
    assert b.flags("t1") == ("ro",)


def test_lifting_is_possible_but_has_to_be_explicit():
    b = ThreadBindings(None)
    b.bind("t1", "repo", flags=("ro",))
    b.lift("t1")
    assert b.flags("t1") == ()


def test_a_thread_bound_before_flags_existed_still_loads(tmp_path):
    # The old file was a flat {thread: agent}. Refusing to read it would
    # orphan every live conversation on the next deploy.
    p = tmp_path / "threads.json"
    p.write_text(json.dumps({"t1": "scout"}), encoding="utf-8")
    b = ThreadBindings(p)
    assert b.get("t1") == "scout"
    assert b.flags("t1") == ()


def test_bindings_with_flags_survive_a_restart(tmp_path):
    p = tmp_path / "threads.json"
    ThreadBindings(p).bind("t1", "repo", flags=("ro",))
    again = ThreadBindings(p)
    assert again.get("t1") == "repo"
    assert again.flags("t1") == ("ro",)


# -- end to end through the app -------------------------------------------
from conftest import open_policy  # noqa: E402

from ricardi.app import Ricardi  # noqa: E402
from ricardi.backends.base import Caller  # noqa: E402
from ricardi.backends.fake import FakeBackend  # noqa: E402
from ricardi.transports.base import Inbound  # noqa: E402


class _Recorder:
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


class _CallerSpy(FakeBackend):
    """Remembers the Caller, which is what carries a flag into a tool gate."""

    def __init__(self):
        super().__init__()
        self.callers: list[Caller] = []

    async def ask(self, name, text, caller=None):
        self.callers.append(caller)
        return await super().ask(name, text, caller)


def _app():
    transport, backend = _Recorder(), _CallerSpy()
    app = Ricardi(
        transport=transport, backend=backend, ack_after=30.0, policy=open_policy()
    )
    return app, transport, backend


def _msg(text, thread="t1"):
    return Inbound(text=text, thread=thread, channel="c1", user="u1")


async def test_a_flag_reaches_the_thing_that_gates_tools():
    app, _, backend = _app()
    await app.handle(_msg("-ro tell scout to summarise this"))
    assert backend.callers[-1].flags == ("ro",)


async def test_a_collaborators_unflagged_reply_is_still_restricted():
    app, _, backend = _app()
    await app.handle(_msg("-ro tell scout to summarise this"))
    await app.handle(_msg("and what about the tests"))
    assert backend.callers[-1].flags == ("ro",)


async def test_an_unflagged_thread_carries_nothing():
    app, _, backend = _app()
    await app.handle(_msg("tell scout to summarise this"))
    assert backend.callers[-1].flags == ()


async def test_a_misspelled_flag_stops_the_request_reaching_the_agent():
    app, transport, backend = _app()
    await app.handle(_msg("-dyr tell scout to delete everything"))
    assert backend.asked == []
    assert "dyr" in transport.last


async def test_the_flags_are_discoverable():
    from ricardi import router

    for name in router.FLAGS:
        assert f"-{name}" in router.HELP
