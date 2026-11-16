"""How a person reading a chat channel sees an event.

Slack is one renderer of the stream among several, and what it chooses to show
is its own business: a browser client reads the same events and shows more. A
renderer chooses what to *show* and nothing else. Whether an action was allowed
was decided before the event existed, and a filter here would be a second
policy engine nobody tested.

Stdlib only, and no import from `backends/`: both seams may depend on this.
"""

from typing import Callable

from ricardi.events import Event

# What fits in one Slack message with room for the framing. The same value
# `backends/sdk.py` holds today; that copy goes when the app stops asking the
# backend for a string, because until then the backend still has to truncate.
MAX_ANSWER: int = 3500


def _said(event: Event) -> str:
    text = event.body.text
    if len(text) > MAX_ANSWER:
        # The stream carries the whole answer; only a channel with a ceiling
        # has to cut it, so the cut is made here and not upstream.
        text = f"{text[:MAX_ANSWER].rstrip()}\n\n[...truncated, {len(text)} characters in all]"
    return f"*{event.envelope.name}*\n{text}"


def _tool_started(event: Event) -> str:
    # The summary only. The stream never carried the arguments, and a renderer
    # must not go looking for them.
    return f"_{event.envelope.name}_ `{event.body.summary}`"


def _tool_finished(event: Event) -> str:
    # Success is the expected case and the call line already announced it.
    if event.body.ok:
        return ""
    return f"_{event.envelope.name}_ `{event.body.tool}` failed"


def _turn_ended(event: Event) -> str:
    # "done" is silent because the answer reaches the channel as a Said;
    # rendering the TurnEnded too would post every reply twice. The other
    # outcomes carry no Said of their own, so the channel needs a sentence.
    return {
        "done": "",
        "cut_short": f"_{event.envelope.name}_ was stopped before it finished.",
        "failed": f"_{event.envelope.name}_ hit an error and did not finish.",
        "timed_out": f"_{event.envelope.name}_ ran out of time before it finished.",
    }[event.body.outcome]


def _diffed(event: Event) -> str:
    # Slack cannot draw a diff. A sentence keeps the change visible; dropping
    # it would make a file edit the one thing the channel never mentioned.
    return f"_{event.envelope.name}_ changed `{event.body.path}` (the diff is not shown here)"


def _pending(event: Event) -> str:
    options = ", ".join(event.body.options)
    return f"_{event.envelope.name}_ is waiting on an answer: {event.body.question} ({options})"


def _refused(event: Event) -> str:
    # Only a refusal that names a tool. A denied tool reached log.info and the
    # model and no transport saw it; showing those is the point of the stream.
    # An inbound refusal (tool == "") already reaches the person refused,
    # privately, through the app; posting it here would tell the whole channel
    # that someone was turned away. This chooses what to show, not what the
    # policy allows, so it is not a second policy engine.
    return event.body.reason if event.body.tool else ""


def _quiet(event: Event) -> str:
    return ""


# One entry per kind in `events._BODIES`, held by a test: a kind added later
# without a decided rendering fails there rather than falling through silently.
# Thought and tool output are quiet here because a channel is a log people read
# aloud; a client with room for them shows them. The person's own message is
# already on screen, so TurnStarted adds nothing.
RENDERERS: dict[str, Callable[[Event], str]] = {
    "turn_started": _quiet,
    "said": _said,
    "thought": _quiet,
    "turn_ended": _turn_ended,
    "tool_started": _tool_started,
    "tool_output": _quiet,
    "tool_finished": _tool_finished,
    "refused": _refused,
    "diffed": _diffed,
    "pending": _pending,
}


def render(event: Event) -> str:
    """The text to show for `event`, or "" for one this renderer stays quiet about."""
    # `Unknown` has no KIND: it is the lenient reader's placeholder for a kind
    # this version cannot interpret, so inventing a presentation for it would
    # be guessing. It renders as nothing.
    renderer = RENDERERS.get(getattr(event.body, "KIND", ""))
    return renderer(event) if renderer else ""
