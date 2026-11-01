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

# "grant <@U2> read on ricardi" / "revoke <@U2> on ricardi". Slack sends a
# mention as <@U123> or <@U123|name>. The tier is any word here: whether it is a
# tier is the escalation check's to say, so a typo is audited as "not a tier"
# rather than falling out of the grammar unseen.
_USER = r"<@(?P<user>[A-Za-z0-9]+)(?:\|[^>]*)?>"
_NAME = r"(?P<name>[A-Za-z0-9_-]+)"
_GRANT = re.compile(
    rf"^grant\s+{_USER}\s+(?P<tier>\S+)\s+on\s+{_NAME}\s*$", re.IGNORECASE
)
_REVOKE = re.compile(rf"^revoke\s+{_USER}\s+on\s+{_NAME}\s*$", re.IGNORECASE)
_CONFER_WORD = re.compile(r"^(?P<verb>grant|revoke)\b", re.IGNORECASE)

GRANT_SHAPE = "`grant @person <read|write> on <agent>`"
REVOKE_SHAPE = "`revoke @person on <agent>`"

# A leading "-ro". Only at the very front, and only whole words, so a hyphen
# anywhere in the message -- "pytest --cov -x", "the -ro flag" -- is text.
_FLAG = re.compile(r"^-(?P<name>[A-Za-z]+)(?=\s|$)\s*")

# Every flag takes something away and none adds anything. That is the whole
# contract: an unrecognised flag can only ever have meant to restrict, so it is
# refused rather than dropped -- dropping it would run what the person was
# trying to prevent.
FLAGS = {
    "ro": "no tool that could change anything, whatever policy allows",
    "dry": "say what it would run, run nothing",
}


@dataclass(frozen=True)
class Command:
    """What the message asked for.

    kind is one of: list, tell, say, who, help, empty, grant, revoke, bad_grant, bad_revoke.
    `tell` names its agent; `say` means "the agent this thread is already
    talking to", which only the caller knows.
    """

    kind: str
    agent: str = ""
    text: str = ""
    # Sorted, so two spellings of the same request compare and store alike.
    flags: tuple[str, ...] = ()
    # What was typed, after flags came off and before any of it was read as
    # addressing. A caller that disagrees with the interpretation needs the
    # original: "tell me what version we are" is an agent called "me" here,
    # and a caller who knows there is no such agent wants the sentence back.
    raw: str = ""
    # grant and revoke only: the Slack id mentioned, and for a grant the tier,
    # already stripped and lowercased so "Read" works without loosening the
    # check that refuses anything not exactly a tier.
    user: str = ""
    tier: str = ""


def parse(message: str) -> Command:
    text = (message or "").strip()
    if not text:
        return Command(kind="empty")

    flags: set[str] = set()
    while True:
        m = _FLAG.match(text)
        if not m:
            break
        name = m.group("name").lower()
        if name not in FLAGS:
            return Command(
                kind="bad_flag",
                text=name,
                flags=tuple(sorted(flags)),
            )
        flags.add(name)
        text = text[m.end():].lstrip()
    found = tuple(sorted(flags))
    raw = text

    if found and not text:
        # "-ro" alone restricts nothing, because there is nothing to restrict.
        return Command(kind="empty", flags=found)
    if _HELP.match(text):
        return Command(kind="help", flags=found, raw=raw)
    if _LIST.match(text):
        return Command(kind="list", flags=found, raw=raw)
    if _WHO.match(text):
        return Command(kind="who", flags=found, raw=raw)

    m = _CONFER_WORD.match(text)
    if m:
        verb = m.group("verb").lower()
        parsed = (_GRANT if verb == "grant" else _REVOKE).match(text)
        if not parsed:
            return Command(kind=f"bad_{verb}", raw=raw)
        return Command(
            kind=verb,
            agent=parsed.group("name"),
            user=parsed.group("user"),
            tier=parsed.group("tier").strip().lower() if verb == "grant" else "",
            flags=found,
            raw=raw,
        )

    m = _TELL.match(text)
    if m:
        body = m.group("text").strip()
        # "tell me the agents" is a listing, not an agent called "me".
        if m.group("agent").lower() in {"me", "us"} and _LIST.match(body):
            return Command(kind="list", flags=found, raw=raw)
        if body:
            return Command(kind="tell", agent=m.group("agent"), text=body, flags=found, raw=raw)
        # "tell scout" with nothing to say is a request to talk to scout.
        return Command(kind="tell", agent=m.group("agent"), text="", flags=found, raw=raw)

    m = _COLON.match(text)
    if m:
        return Command(
            kind="tell",
            agent=m.group("agent"),
            text=m.group("text").strip(),
            flags=found,
            raw=raw,
        )

    return Command(kind="say", text=text, flags=found, raw=raw)


HELP = (
    "Here is what I understand:\n"
    "- `list agents` - who I can reach, and whether they are free\n"
    "- `tell <agent> <message>` - send that agent a message\n"
    "- `<agent>: <message>` - the same thing, shorter\n"
    "- `who` - which agent this thread is talking to\n"
    f"- {GRANT_SHAPE} / {REVOKE_SHAPE} - owners only: give or take back a tier\n"
    "Once a thread is talking to an agent, just reply in the thread and I pass "
    "it along.\n\n"
    "Flags go first, and only ever take something away:\n"
    + "\n".join(f"- `-{name}` - {why}" for name, why in sorted(FLAGS.items()))
    + "\nA flag sticks to the thread, so it still holds when someone else replies."
)
