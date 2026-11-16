"""What happened in a turn, as a typed stream any client can read.

A turn used to collapse to one string, so a browser or a terminal had nothing
to render but the end of it. This module is the vocabulary those clients and
the backends share. It imports nothing from `transports/` or `backends/`: the
vocabulary belongs to neither seam, and a seam that learned the other's shape
through here would be going the wrong way.

The deliverable is the JSON form in `docs/events.md`; the classes below are
one binding of it. Python 3.11 stdlib only, because a TypeScript client reads
the same contract and the box takes no runtime dependency for it.
"""

# No `from __future__ import annotations` here, deliberately: `from_wire` reads
# each body field's type from `f.type`, which is a real class only while
# annotations are evaluated. The future import would turn it into the string
# "str" and silently break the reader.
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import ClassVar, TypeVar

from ricardi.policy import summarise

# Bumped only when an existing field changes meaning. A new kind is not a bump:
# clients are lenient about kinds they do not know, which is the whole point.
SCHEMA_VERSION: int = 1

# Who caused the event. "ricardi" is the bot itself (a denial, a notice).
ACTORS = ("owner", "person", "agent", "ricardi")

OUTCOMES = ("done", "cut_short", "failed", "timed_out")

# Characters of tool output one event carries. Output is the one free text the
# agent does not choose the size of, and a client holding a stream open should
# not be handed a whole build log in a single frame.
MAX_OUTPUT: int = 8000


def now() -> str:
    """RFC 3339, UTC, whole seconds: sortable as text and the same in any client."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Envelope:
    """What every event carries, whatever it says.

    `name` is on every event because one box serves several agents, and a
    stream shared across names would leak one project's transcript to a client
    watching another. `seq` orders events within a name's stream.
    """

    seq: int
    at: str
    name: str
    turn: str
    actor: str

    def __post_init__(self) -> None:
        # Here because the actor is the envelope's field, not any body's.
        if self.actor not in ACTORS:
            raise ValueError(f"actor {self.actor!r} is not one of {ACTORS}")


@dataclass(frozen=True)
class TurnStarted:
    text: str
    KIND: ClassVar[str] = "turn_started"


@dataclass(frozen=True)
class Said:
    text: str
    KIND: ClassVar[str] = "said"


@dataclass(frozen=True)
class Thought:
    text: str
    KIND: ClassVar[str] = "thought"


@dataclass(frozen=True)
class TurnEnded:
    outcome: str
    answer: str
    KIND: ClassVar[str] = "turn_ended"

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome {self.outcome!r} is not one of {OUTCOMES}")


@dataclass(frozen=True)
class ToolStarted:
    """A tool call began. `summary` is all of its input the stream will carry.

    There is no field that could hold the arguments, on purpose: a log that
    records every argument eventually records a password. `tool_started` is the
    way in, and it summarises on the way.
    """

    tool: str
    summary: str
    tool_use_id: str
    KIND: ClassVar[str] = "tool_started"


@dataclass(frozen=True)
class ToolOutput:
    tool_use_id: str
    text: str
    truncated: bool
    KIND: ClassVar[str] = "tool_output"


@dataclass(frozen=True)
class ToolFinished:
    tool_use_id: str
    tool: str
    ok: bool
    ms: int
    KIND: ClassVar[str] = "tool_finished"


@dataclass(frozen=True)
class Refused:
    """The policy said no. `tier` is the one the person held, "" if none."""

    rule: str
    reason: str
    tier: str
    tool: str
    target: str
    KIND: ClassVar[str] = "refused"


@dataclass(frozen=True)
class Diffed:
    """A file the agent changed, as unified-diff hunks.

    This carries content where `ToolStarted` must not, and that is deliberate.
    The redaction rule is about tool input: a password lives in an argument. A
    diff is the file itself, and a watcher sees everything the session prints;
    no tier governs that. Keeping the two apart is what lets the first stay
    summarised without the second pretending to be.
    """

    path: str
    hunks: tuple[str, ...]
    KIND: ClassVar[str] = "diffed"


@dataclass(frozen=True)
class Pending:
    """A question waiting on a person; `correlation_id` is how the answer finds it."""

    correlation_id: str
    question: str
    options: tuple[str, ...]
    KIND: ClassVar[str] = "pending"


@dataclass(frozen=True)
class Unknown:
    """A kind this reader does not know, kept rather than dropped.

    Only a lenient reader makes one; no writer does, so it has no KIND and is
    not in `_BODIES`. `fields` is the one dict in the vocabulary because it
    holds data a client chose not to understand.
    """

    kind: str
    fields: dict[str, object]


@dataclass(frozen=True)
class Event:
    envelope: Envelope
    body: object


# Real wire kinds only, keyed by KIND. `from_wire` dispatches on this and the
# docs are checked against it, so a kind added here without a section fails.
_BODIES: dict[str, type] = {
    body.KIND: body for body in (TurnStarted, Said, Thought, TurnEnded, ToolStarted, ToolOutput,
             ToolFinished, Refused, Diffed, Pending)
}

_ENVELOPE_FIELDS = tuple(f.name for f in fields(Envelope))
_RESERVED = frozenset({"v", "kind", *_ENVELOPE_FIELDS})


# The only annotations a body field may have. An allowlist rather than a ban on
# dict: respelled (Mapping, Any, object, nested tuples) a ban is evaded, and
# the reader has no mapping guard, so the annotation is all that stands between
# a future kind and structured argument data. It also names every type
# `from_wire` knows how to read, so an unreadable one fails here, not there.
_FIELD_TYPES: tuple[object, ...] = (str, int, bool, tuple[str, ...])


def _check_bodies(bodies: dict[str, type]) -> None:
    """Refuse a body field the envelope owns, or one of a type we cannot carry.

    The wire form is flat, so a field sharing a name with the envelope or
    `v`/`kind` would overwrite the envelope's value on write, `name` (the
    partition key) included, and the event would be filed under the wrong agent
    without any error. Failing at import puts the mistake in the author's first
    run instead of in a transcript.

    The wire form is flat, so such a field would overwrite the envelope's value
    on write, `name` (the partition key) included, and the event would be
    filed under the wrong agent without any error. Failing at import puts the
    mistake in the author's first run instead of in a transcript.
    """
    for kind, cls in bodies.items():
        clash = _RESERVED & {f.name for f in fields(cls)}
        if clash:
            raise TypeError(f"{kind} has field(s) {sorted(clash)} that the "
                            "envelope owns; name them something else")
        for f in fields(cls):
            if f.type not in _FIELD_TYPES:
                raise TypeError(f"{kind}.{f.name} is annotated {f.type!r}; a body "
                                f"field must be one of {list(_FIELD_TYPES)}")


_check_bodies(_BODIES)


def to_wire(event: Event) -> dict[str, object]:
    """One flat dict: a client reads `kind`, then its own fields; nesting buys nothing."""
    body = event.body
    wire: dict[str, object] = {"v": SCHEMA_VERSION}
    wire.update({n: getattr(event.envelope, n) for n in _ENVELOPE_FIELDS})
    wire["kind"] = body.KIND
    # A tuple becomes a list here so the dict is what json.dumps would give
    # back; from_wire turns it into a tuple again.
    wire.update({f.name: list(v) if isinstance(v := getattr(body, f.name), tuple) else v
                 for f in fields(body)})
    return wire


T = TypeVar("T")


def _need(data: dict[str, object], name: str, kind: type[T]) -> T:
    if name not in data:
        raise ValueError(f"event is missing {name!r}")
    value = data[name]
    # bool is an int in Python, and a JSON true is not a sequence number; but
    # a field declared bool must still be readable.
    if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
        raise ValueError(f"{name!r} must be {kind.__name__}, got {value!r}")
    return value


def _need_strings(data: dict[str, object], name: str) -> tuple[str, ...]:
    # JSON has no tuple; the body is frozen and compared by value, so it must
    # come back as one.
    value = data.get(name)
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise ValueError(f"{name!r} must be a list of strings, got {value!r}")
    return tuple(value)


def _read_field(data: dict[str, object], name: str, kind: type) -> object:
    if kind == tuple[str, ...]:
        return _need_strings(data, name)
    return _need(data, name, kind)


def from_wire(data: dict[str, object], *, strict: bool = True) -> Event:
    """Read one event. The server is strict; a client passes `strict=False`.

    Strict refuses a kind or a field it does not know. Lenient keeps an unknown
    kind as `Unknown` and ignores extra fields on a known one, so a kind added
    later does not break a client that predates it. A version it does not speak
    is refused either way: leniency covers additions, not changed meanings.
    """
    version = _need(data, "v", int)
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema version {version!r}")
    envelope = Envelope(
        seq=_need(data, "seq", int),
        at=_need(data, "at", str),
        name=_need(data, "name", str),
        turn=_need(data, "turn", str),
        actor=_need(data, "actor", str),
    )
    kind = _need(data, "kind", str)
    rest = {k: v for k, v in data.items() if k not in _RESERVED}
    cls = _BODIES.get(kind)
    if cls is None:
        if strict:
            raise ValueError(f"unknown kind {kind!r}")
        return Event(envelope, Unknown(kind=kind, fields=rest))
    names = {f.name for f in fields(cls)}
    if strict:
        extra = sorted(set(rest) - names)
        if extra:
            raise ValueError(f"{kind} has no field {extra[0]!r}")
    return Event(envelope, cls(**{f.name: _read_field(rest, f.name, f.type)
                                  for f in fields(cls)}))


def tool_started(tool: str, tool_input: dict, tool_use_id: str) -> ToolStarted:
    """The only way to make a ToolStarted from a call: the input is summarised here."""
    return ToolStarted(tool=tool, summary=summarise(tool, tool_input),
                       tool_use_id=tool_use_id)


def tool_output(tool_use_id: str, text: str) -> ToolOutput:
    return ToolOutput(tool_use_id=tool_use_id, text=text[:MAX_OUTPUT],
                      truncated=len(text) > MAX_OUTPUT)
