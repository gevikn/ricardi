"""The event vocabulary and its JSON wire form."""

import json

import pytest

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


def test_an_event_round_trips_through_the_wire_form():
    event = Event(Envelope(seq=1, at="2026-11-10T15:04:05Z", name="ricardi",
                           turn="t1", actor="agent"), Said(text="hello"))
    assert from_wire(to_wire(event)) == event


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
