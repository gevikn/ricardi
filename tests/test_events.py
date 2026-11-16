"""The event vocabulary and its JSON wire form."""

import dataclasses
import json
import logging
import typing
from dataclasses import dataclass
from pathlib import Path

import pytest

from ricardi import events

from ricardi.events import (
    ACTORS,
    MAX_OUTPUT,
    Diffed,
    Pending,
    Refused,
    ToolFinished,
    ToolOutput,
    ToolStarted,
    tool_output,
    tool_started,
    OUTCOMES,
    SCHEMA_VERSION,
    Envelope,
    Event,
    Said,
    Thought,
    TurnEnded,
    TurnStarted,
    Recorder,
    Stream,
    Unknown,
    from_wire,
    new_turn_id,
    now,
    to_wire,
)
from ricardi.policy import summarise

ROOT = Path(__file__).resolve().parent.parent


# One instance per kind in _BODIES; the test below fails if a kind is added
# without one, so the round trip cannot quietly stop covering it.
_SAMPLES = {
    "turn_started": TurnStarted(text="go"),
    "said": Said(text="hello"),
    "thought": Thought(text="hmm"),
    "turn_ended": TurnEnded(outcome="timed_out", answer="partial"),
    "tool_started": ToolStarted("Bash", "Bash: ls", "tu_1"),
    "tool_output": ToolOutput("tu_1", "ok", False),
    "tool_finished": ToolFinished("tu_1", "Bash", True, 12),
    "refused": Refused("tier", "You hold read on ricardi.", "read", "Edit", ""),
    "diffed": Diffed("src/a.py", ("@@ -1 +1 @@", "-a", "+b")),
    "pending": Pending("c1", "Run `git push`?", ("yes", "no")),
}


def test_every_kind_has_a_sample():
    assert set(_SAMPLES) == set(events._BODIES)


@pytest.mark.parametrize("kind", sorted(_SAMPLES))
def test_an_event_round_trips_through_the_wire_form(kind):
    event = Event(Envelope(seq=1, at="2026-11-10T15:04:05Z", name="ricardi",
                           turn="t1", actor="agent"), _SAMPLES[kind])
    assert from_wire(to_wire(event)) == event


def test_a_turn_ended_with_a_bad_outcome_is_refused_on_read_too():
    wire = to_wire(Event(Envelope(1, now(), "n", "t", "agent"),
                         TurnEnded(outcome="done", answer="")))
    wire["outcome"] = "exploded"
    with pytest.raises(ValueError):
        from_wire(wire)


def test_a_body_field_named_like_an_envelope_field_is_refused_at_definition():
    @dataclass(frozen=True)
    class ToolUsed:
        name: str
        KIND = "tool_used"

    with pytest.raises(TypeError):
        events._check_bodies({"tool_used": ToolUsed})


def test_int_and_bool_fields_round_trip(monkeypatch):
    @dataclass(frozen=True)
    class Measured:
        path: str
        ms: int
        ok: bool
        KIND = "measured"

    monkeypatch.setitem(events._BODIES, "measured", Measured)
    event = Event(Envelope(1, now(), "n", "t", "agent"), Measured("a", 3, True))
    assert from_wire(to_wire(event)) == event
    wire = to_wire(event)
    wire["ms"] = True
    with pytest.raises(ValueError):
        from_wire(wire)


def test_a_boolean_or_float_version_is_not_version_one():
    base = {"seq": 1, "at": now(), "name": "n", "turn": "t",
            "actor": "agent", "kind": "said", "text": "x"}
    for bad in (True, 1.0):
        with pytest.raises(ValueError):
            from_wire({**base, "v": bad})


def test_the_wire_form_is_flat_and_names_its_kind_and_version():
    wire = to_wire(Event(Envelope(seq=7, at="2026-11-10T15:04:05Z",
                                  name="ricardi", turn="t1", actor="agent"),
                         Said(text="hi")))
    assert wire == {"v": 1, "seq": 7, "at": "2026-11-10T15:04:05Z",
                    "name": "ricardi", "turn": "t1", "actor": "agent",
                    "kind": "said", "text": "hi"}


def test_a_kind_this_version_does_not_know_is_refused_server_side():
    with pytest.raises(ValueError):
        from_wire({"v": 1, "seq": 1, "at": "2026-11-10T15:04:05Z",
                   "name": "n", "turn": "t", "actor": "agent",
                   "kind": "screencast_frame", "data": "..."})


def test_a_lenient_reader_keeps_an_unknown_kind_instead_of_failing():
    event = from_wire({"v": 1, "seq": 1, "at": "2026-11-10T15:04:05Z",
                       "name": "n", "turn": "t", "actor": "agent",
                       "kind": "screencast_frame", "data": "x"}, strict=False)
    assert isinstance(event.body, Unknown)
    assert event.body.kind == "screencast_frame"
    assert event.body.fields == {"data": "x"}


def test_every_wire_form_is_json_serialisable():
    for body in (TurnStarted(text="go"), Said(text="hi"),
                 Thought(text="hmm"), TurnEnded(outcome="done", answer="hi")):
        json.dumps(to_wire(Event(Envelope(1, now(), "n", "t", "agent"), body)))


def test_an_actor_or_outcome_outside_the_vocabulary_is_refused():
    with pytest.raises(ValueError):
        Event(Envelope(1, now(), "n", "t", "nobody"), Said(text="hi"))
    with pytest.raises(ValueError):
        Event(Envelope(1, now(), "n", "t", "agent"),
              TurnEnded(outcome="exploded", answer=""))


def test_every_actor_and_outcome_in_the_vocabulary_is_accepted():
    for actor in ACTORS:
        Envelope(1, now(), "n", "t", actor)
    for outcome in OUTCOMES:
        TurnEnded(outcome=outcome, answer="")


def test_now_is_rfc3339_utc_to_the_second():
    assert len(now()) == 20
    assert now().endswith("Z")
    assert "." not in now()


def test_a_version_this_reader_does_not_speak_is_refused_even_when_lenient():
    with pytest.raises(ValueError):
        from_wire({"v": SCHEMA_VERSION + 1, "seq": 1, "at": now(), "name": "n",
                   "turn": "t", "actor": "agent", "kind": "said",
                   "text": "x"}, strict=False)


def test_a_known_kind_missing_a_field_is_refused():
    with pytest.raises(ValueError):
        from_wire({"v": 1, "seq": 1, "at": now(), "name": "n", "turn": "t",
                   "actor": "agent", "kind": "said"})


def test_a_known_kind_with_an_extra_field_is_refused_when_strict():
    wire = {"v": 1, "seq": 1, "at": now(), "name": "n", "turn": "t",
            "actor": "agent", "kind": "said", "text": "x", "extra": 1}
    with pytest.raises(ValueError):
        from_wire(wire)
    assert from_wire(wire, strict=False).body == Said(text="x")


def test_an_envelope_field_missing_is_refused():
    with pytest.raises(ValueError):
        from_wire({"v": 1, "seq": 1, "at": now(), "turn": "t",
                   "actor": "agent", "kind": "said", "text": "x"})


def test_every_body_field_is_a_scalar_or_a_tuple_of_strings():
    # Structural: nothing that can hold a mapping or a nested structure exists
    # in the vocabulary. The guard runs at import (`_check_bodies`); this pins
    # that the real bodies satisfy it and that it is the allowlist doing it.
    allowed = {str, int, bool, tuple[str, ...]}
    for body in events._BODIES.values():
        for f in dataclasses.fields(body):
            assert f.type in allowed, (body.__name__, f.name)


@pytest.mark.parametrize("annotation", [dict, dict[str, str], typing.Mapping[str, str],
                                        typing.Any, object, list[str],
                                        tuple[tuple[str, str], ...], tuple[int, ...]])
def test_a_body_field_of_an_unsupported_type_is_refused_at_definition(annotation):
    @dataclass(frozen=True)
    class Loose:
        data: annotation
        KIND = "loose"

    with pytest.raises(TypeError, match=r"loose\.data"):
        events._check_bodies({"loose": Loose})


def test_a_tool_event_carries_the_summary_of_its_input_and_nothing_structural():
    # The summary is argument *text* (the channel shows the same, so a person
    # approving a command sees it); what must not appear is the argument
    # *structure*: no key of the input, no nested value.
    command = {"command": "psql -U admin -W hunter2", "env": {"PGPASSWORD": "s3cret"}}
    started = tool_started("Bash", command, "tu_1")
    assert started.summary == summarise("Bash", command)
    wire = to_wire(Event(Envelope(1, now(), "n", "t", "agent"), started))
    assert set(wire) == {"v", "seq", "at", "name", "turn", "actor", "kind",
                         "tool", "summary", "tool_use_id"}
    assert not any(isinstance(v, (dict, list)) for v in wire.values())
    assert "s3cret" not in repr(wire)


def test_a_tool_with_no_summarisable_input_is_still_identified():
    started = tool_started("TodoWrite", {"todos": [{"a": 1}]}, "tu_2")
    assert started.tool == "TodoWrite"
    assert started.summary == "TodoWrite"


def test_output_is_capped_and_says_that_it_was():
    out = tool_output("tu_1", "x" * (MAX_OUTPUT + 500))
    assert len(out.text) == MAX_OUTPUT and out.truncated is True
    assert tool_output("tu_1", "x" * MAX_OUTPUT).truncated is False
    assert tool_output("tu_1", "short").truncated is False


def test_a_tuple_field_survives_a_real_json_round_trip():
    # JSON has no tuples: a client sends a list, and the frozen body must still
    # compare equal to the one that was written.
    event = Event(Envelope(1, now(), "n", "t", "agent"),
                  Diffed("a.py", ("@@", "-a", "+b")))
    assert from_wire(json.loads(json.dumps(to_wire(event)))) == event


def test_a_tuple_field_with_a_non_string_item_is_refused():
    wire = to_wire(Event(Envelope(1, now(), "n", "t", "agent"), Pending("c", "q", ("a",))))
    wire["options"] = ["a", 2]
    with pytest.raises(ValueError):
        from_wire(wire)
    wire["options"] = "a"
    with pytest.raises(ValueError):
        from_wire(wire)


def test_every_kind_in_the_module_is_documented():
    text = (ROOT / "docs" / "events.md").read_text(encoding="utf-8")
    for kind in events._BODIES:
        assert f"`{kind}`" in text, kind


class ExplodingSink:
    def __init__(self) -> None:
        self.attempts = 0

    async def emit(self, event: Event) -> None:
        self.attempts += 1
        raise RuntimeError("the socket is closed")


async def test_a_stream_stamps_sequence_and_time_and_name():
    recorder = Recorder()
    stream = Stream("ricardi", recorder, clock=lambda: "2026-11-10T15:04:05Z")
    await stream.emit("agent", Said(text="one"), turn="t1")
    await stream.emit("agent", Said(text="two"), turn="t1")
    assert [e.envelope.seq for e in recorder.events] == [1, 2]
    assert {e.envelope.name for e in recorder.events} == {"ricardi"}
    assert {e.envelope.at for e in recorder.events} == {"2026-11-10T15:04:05Z"}
    assert {(e.envelope.turn, e.envelope.actor) for e in recorder.events} == {("t1", "agent")}


async def test_two_streams_for_two_names_do_not_share_a_sequence():
    # A single global transcript on a box serving two names is a cross-project
    # leak, which is what 1:1 exists to prevent.
    recorder = Recorder()
    await Stream("a", recorder).emit("agent", Said(text="x"))
    await Stream("b", recorder).emit("agent", Said(text="y"))
    assert [(e.envelope.name, e.envelope.seq) for e in recorder.events] == [("a", 1), ("b", 1)]


async def test_a_sink_that_raises_does_not_stop_the_stream():
    sink = ExplodingSink()
    stream = Stream("ricardi", sink)
    first = await stream.emit("agent", Said(text="one"))  # must not raise
    second = await stream.emit("agent", Said(text="two"))
    assert (first.body, second.body) == (Said(text="one"), Said(text="two"))
    # The failed event still used its number, so a reader sees a gap, not a
    # repeat, if the sink recovers.
    assert [first.envelope.seq, second.envelope.seq] == [1, 2]
    assert sink.attempts == 2


async def test_a_sink_that_raises_is_logged_once_per_event(caplog):
    stream = Stream("ricardi", ExplodingSink())
    with caplog.at_level(logging.DEBUG, logger="ricardi"):
        await stream.emit("agent", Said(text="one"))
        await stream.emit("agent", Said(text="two"))
    failures = [r for r in caplog.records if "the socket is closed" in r.getMessage()
                or (r.exc_info and "the socket is closed" in str(r.exc_info[1]))]
    assert len(failures) == 2
    # A level a person would see: at debug, a dead transport would be silent.
    assert all(r.levelno >= logging.WARNING for r in failures)
    assert all("Said" in r.getMessage() and "ricardi" in r.getMessage() for r in failures)


async def test_a_recorder_keeps_events_in_order():
    recorder = Recorder()
    stream = Stream("n", recorder)
    sent = [await stream.emit("owner", Said(text=t)) for t in "abc"]
    assert recorder.events == sent


def test_a_turn_id_is_twelve_hex_characters_and_not_repeated():
    ids = {new_turn_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 12 and int(i, 16) >= 0 for i in ids)
