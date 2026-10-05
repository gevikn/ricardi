"""Turning what someone typed in Slack into one decision.

Deliberately not a language model. The command surface is small enough that
parsing it is exact, free and instant, and an exact parse is what makes the
end-to-end test meaningful. When the surface outgrows this, the LLM goes behind
`parse` without anything above it changing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "tell scout to look at auth.py" -- the "to" reads naturally in Slack and
# carries no meaning, so it comes off before the agent sees the message.
_TELL = re.compile(
    r"^(?:tell|ask)\s+(?P<agent>[A-Za-z0-9_-]+)(?:\s+to\b)?\s*(?P<text>.*)$",
    re.IGNORECASE | re.DOTALL,
)
# "scout: look at auth.py"
_COLON = re.compile(r"^(?P<agent>[A-Za-z0-9_-]+)\s*:\s*(?P<text>.+)$", re.DOTALL)
_LIST = re.compile(
    r"^(?:the\s+)?(?:list(?:\s+(?:the\s+)?agents?)?|agents?)\s*$", re.IGNORECASE
)
_HELP = re.compile(r"^(?:help|\?)\s*$", re.IGNORECASE)
_WHO = re.compile(r"^who(?:\s+is\s+this|\s*\?)?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Command:
    """What the message asked for.

    kind is one of: list, tell, say, who, help, empty.
    `tell` names its agent; `say` means "the agent this thread is already
    talking to", which only the caller knows.
    """

    kind: str
    agent: str = ""
    text: str = ""


def parse(message: str) -> Command:
    text = (message or "").strip()
    if not text:
        return Command(kind="empty")
    if _HELP.match(text):
        return Command(kind="help")
    if _LIST.match(text):
        return Command(kind="list")
    if _WHO.match(text):
        return Command(kind="who")

    m = _TELL.match(text)
    if m:
        body = m.group("text").strip()
        # "tell me the agents" is a listing, not an agent called "me".
        if m.group("agent").lower() in {"me", "us"} and _LIST.match(body):
            return Command(kind="list")
        if body:
            return Command(kind="tell", agent=m.group("agent"), text=body)
        # "tell scout" with nothing to say is a request to talk to scout.
        return Command(kind="tell", agent=m.group("agent"), text="")

    m = _COLON.match(text)
    if m:
        return Command(kind="tell", agent=m.group("agent"), text=m.group("text").strip())

    return Command(kind="say", text=text)


HELP = (
    "Here is what I understand:\n"
    "- `list agents` - who I can reach, and whether they are free\n"
    "- `tell <agent> <message>` - send that agent a message\n"
    "- `<agent>: <message>` - the same thing, shorter\n"
    "- `who` - which agent this thread is talking to\n"
    "Once a thread is talking to an agent, just reply in the thread and I pass "
    "it along."
)
