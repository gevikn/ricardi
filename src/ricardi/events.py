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

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import ClassVar

# Bumped only when an existing field changes meaning. A new kind is not a bump:
# clients are lenient about kinds they do not know, which is the whole point.
SCHEMA_VERSION: int = 1

# Who caused the event. "ricardi" is the bot itself (a denial, a notice).
ACTORS = ("owner", "person", "agent", "ricardi")

OUTCOMES = ("done", "cut_short", "failed", "timed_out")


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
    body.KIND: body for body in (TurnStarted, Said, Thought, TurnEnded)
}

_ENVELOPE_FIELDS = tuple(f.name for f in fields(Envelope))


def to_wire(event: Event) -> dict[str, object]:
    """One flat dict: a client reads `kind`, then its own fields; nesting buys nothing."""
    body = event.body
    wire: dict[str, object] = {"v": SCHEMA_VERSION}
    wire.update({n: getattr(event.envelope, n) for n in _ENVELOPE_FIELDS})
    wire["kind"] = body.KIND
    wire.update({f.name: getattr(body, f.name) for f in fields(body)})
    return wire


def _need(data: dict[str, object], name: str, kind: type) -> object:
    if name not in data:
        raise ValueError(f"event is missing {name!r}")
    value = data[name]
    # bool is an int in Python, and a JSON true is not a sequence number.
    if not isinstance(value, kind) or isinstance(value, bool):
        raise ValueError(f"{name!r} must be {kind.__name__}, got {value!r}")
    return value


def from_wire(data: dict[str, object], *, strict: bool = True) -> Event:
    """Read one event. The server is strict; a client passes `strict=False`.

    Strict refuses a kind or a field it does not know. Lenient keeps an unknown
    kind as `Unknown` and ignores extra fields on a known one, so a kind added
    later does not break a client that predates it. A version it does not speak
    is refused either way: leniency covers additions, not changed meanings.
    """
    version = data.get("v")
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema version {version!r}")
    envelope = Envelope(
        seq=_need(data, "seq", int),  # type: ignore[arg-type]
        at=_need(data, "at", str),  # type: ignore[arg-type]
        name=_need(data, "name", str),  # type: ignore[arg-type]
        turn=_need(data, "turn", str),  # type: ignore[arg-type]
        actor=_need(data, "actor", str),  # type: ignore[arg-type]
    )
    kind = _need(data, "kind", str)
    rest = {k: v for k, v in data.items()
            if k not in ("v", "kind", *_ENVELOPE_FIELDS)}
    cls = _BODIES.get(kind)  # type: ignore[arg-type]
    if cls is None:
        if strict:
            raise ValueError(f"unknown kind {kind!r}")
        return Event(envelope, Unknown(kind=kind, fields=rest))  # type: ignore[arg-type]
    names = [f.name for f in fields(cls)]
    if strict:
        extra = sorted(set(rest) - set(names))
        if extra:
            raise ValueError(f"{kind} has no field {extra[0]!r}")
    return Event(envelope, cls(**{n: _need(rest, n, str) for n in names}))
