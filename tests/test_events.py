"""The event vocabulary and its JSON wire form."""

import json
from dataclasses import dataclass

import pytest

from ricardi import events

from ricardi.events import (
    ACTORS,
    OUTCOMES,
    SCHEMA_VERSION,
    Envelope,
    Event,
    Said,
    Thought,
    TurnEnded,
    TurnStarted,
    Unknown,
    from_wire,
    now,
    to_wire,
)


# One instance per kind in _BODIES; the test below fails if a kind is added
# without one, so the round trip cannot quietly stop covering it.
_SAMPLES = {
    "turn_started": TurnStarted(text="go"),
    "said": Said(text="hello"),
    "thought": Thought(text="hmm"),
    "turn_ended": TurnEnded(outcome="timed_out", answer="partial"),
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
