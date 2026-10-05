"""Who may ask, and what the agent may then do.

Two questions, one decision point. The first is nearly trivial -- a roster of
the people allowed to address agents. The second is the one worth having: every
tool an agent tries to use is checked before it runs, so what two engineers
agreed Claude may do on their box is executable rather than a wiki page.

The interface is deliberately small so a real policy service can replace the
rules engine without anything above it changing. Everything here fails closed:
an error, a timeout, a missing attribute and an unmatched request all deny.
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# The shell is named per platform, so a rule naming one covers nothing on the
# other. Writing `tools = ["shell"]` covers whichever is in play, which is what
# anyone means and what nobody remembers to spell out.
SHELL_TOOLS = ("Bash", "PowerShell")
SHELL_ALIAS = "shell"

# Tool inputs name their path under different keys depending on the tool.
_PATH_KEYS = ("file_path", "path", "notebook_path")
# Keys worth quoting back to the channel when announcing what is about to run.
_SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "url")


@dataclass(frozen=True)
class Principal:
    """Who is asking. `known` is false for anyone not on the roster."""

    id: str = ""
    email: str = ""
    known: bool = False


@dataclass(frozen=True)
class Resource:
    """Where they are asking from."""

    channel_id: str = ""
    channel_name: str = ""


@dataclass(frozen=True)
class Request:
    """One thing to decide.

    `action` is "ask" (address an agent), "list" (see the catalogue), or "tool"
    (the agent wants to run something).
    """

    action: str
    principal: Principal = Principal()
    resource: Resource = Resource()
    agent: str = ""
    tool: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    text: str = ""


@dataclass(frozen=True)
class Decision:
    allow: bool
    # Shown to the human, or handed to the model on a denied tool. Says what to
    # do about it without naming which rule fired.
    reason: str = ""
    obligations: tuple[str, ...] = ()
    # Which rule decided this, for the audit log. "Why was this allowed?" is
    # unanswerable at 2am without it. Empty means the default denied it.
    rule: str = ""


@runtime_checkable
class PolicyPoint(Protocol):
    async def decide(self, request: Request) -> Decision: ...


class PolicyError(Exception):
    """The policy itself is wrong, with a reason worth printing at startup."""


def summarise(tool: str, tool_input: dict[str, Any]) -> str:
    """A one-line description of a tool call, for the channel to see."""
    for key in _SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            if len(text) > 300:
                text = text[:300] + "…"
            return f"{tool}: {text}"
    return tool


def _paths(tool_input: dict[str, Any]) -> list[str]:
    return [
        str(tool_input[k]) for k in _PATH_KEYS
        if isinstance(tool_input.get(k), str) and tool_input[k]
    ]


def _under(path: str, root: str) -> bool:
    """Is `path` inside `root`, after links and `..` are resolved?

    Resolved rather than compared as text, because "scratch/../../.ssh" is
    textually under scratch and is not where anyone meant.
    """
    try:
        p = Path(path).expanduser().resolve()
        r = Path(root).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    try:
        # normcase so Windows' case-insensitive paths compare as the OS sees them.
        return os.path.normcase(str(p)).startswith(os.path.normcase(str(r)) + os.sep) or (
            os.path.normcase(str(p)) == os.path.normcase(str(r))
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class Rule:
    effect: str
    name: str = ""
    actions: tuple[str, ...] = ()
    users: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    path_under: str = ""
    # Globs against any path in the tool input. Where path_under asks "are all
    # of them inside this root" for allows, this asks "does any of them look
    # like this", which is the shape a deny wants.
    path_matches: tuple[str, ...] = ()
    command_matches: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()

    def matches(self, request: Request) -> bool:
        """Every dimension the rule names must match; unnamed ones match all."""
        if self.actions and request.action not in self.actions:
            return False
        if self.users and not _any_glob(request.principal.email, self.users):
            return False
        if self.channels and request.resource.channel_id not in self.channels:
            return False
        if self.agents and not _any_glob(request.agent, self.agents):
            return False
        if self.tools and not _tool_matches(request.tool, self.tools):
            return False

        if self.path_under:
            paths = _paths(request.tool_input)
            # No path to check means the condition cannot be satisfied, and an
            # unsatisfied condition must not grant anything.
            if not paths or not all(_under(p, self.path_under) for p in paths):
                return False

        if self.path_matches:
            paths = _paths(request.tool_input)
            if not paths or not any(
                _glob(p.replace("\\", "/"), pat)
                for p in paths
                for pat in self.path_matches
            ):
                return False

        if self.command_matches:
            command = request.tool_input.get("command")
            if not isinstance(command, str):
                return False
            flat = " ".join(command.split())
            if not any(_glob(flat, pat) for pat in self.command_matches):
                return False

        return True


def _tool_matches(tool: str, patterns: tuple[str, ...]) -> bool:
    """Match a tool name, expanding the `shell` alias to every platform's."""
    expanded: list[str] = []
    for pattern in patterns:
        if pattern.lower() == SHELL_ALIAS:
            expanded.extend(SHELL_TOOLS)
        else:
            expanded.append(pattern)
    return _any_glob(tool, tuple(expanded))


def _glob(value: str, pattern: str) -> bool:
    """Case-insensitive glob that means the same thing on every platform.

    `fnmatch.fnmatch` runs both sides through `os.path.normcase`, which
    lowercases on Windows and does nothing on POSIX. Matching through it made
    deny rules case-sensitive on Linux and not on Windows, so `GIT PUSH` evaded
    a `*git push*` deny on one of two identical deployments. `fnmatchcase` over
    explicitly lowered strings has no such opinion.
    """
    return fnmatch.fnmatchcase(value.lower(), pattern.lower())


def _any_glob(value: str, patterns: tuple[str, ...]) -> bool:
    if not value:
        return False
    return any(_glob(value, p) for p in patterns)


class RulesPolicy:
    """The built-in decision point: a roster and a list of rules.

    Deny overrides. An explicit deny wins over any allow, and a request no rule
    allows is denied. There is no ordering subtlety to get wrong.
    """

    def __init__(
        self,
        rules: list[Rule] | tuple[Rule, ...] = (),
        roster: dict[str, str] | None = None,
    ):
        self.rules = tuple(rules)
        # Slack user id -> email. The roster is the whole identity story: no
        # directory lookups, so no extra Slack scopes and nothing to cache.
        self.roster = dict(roster or {})

    def principal(self, slack_user_id: str) -> Principal:
        email = self.roster.get(slack_user_id, "")
        return Principal(id=slack_user_id, email=email, known=bool(email))

    async def decide(self, request: Request) -> Decision:
        matched = [r for r in self.rules if r.matches(request)]

        denies = [r for r in matched if r.effect == "deny"]
        if denies:
            return Decision(
                allow=False,
                reason=self._deny_reason(request, denies[0]),
                rule=denies[0].name or "an unnamed deny",
            )

        allows = [r for r in matched if r.effect == "allow"]
        if allows:
            obligations: list[str] = []
            for rule in allows:
                for ob in rule.obligations:
                    if ob not in obligations:
                        obligations.append(ob)
            return Decision(
                allow=True,
                obligations=tuple(obligations),
                rule=", ".join(r.name or "unnamed" for r in allows),
            )

        return Decision(allow=False, reason=self._default_reason(request), rule="")

    # -- what the human or the model is told ------------------------------
    def _deny_reason(self, request: Request, rule: Rule) -> str:
        if request.action == "tool":
            what = summarise(request.tool, request.tool_input)
            return f"Not permitted here: {what}. Ask a person to run it."
        return "That isn't permitted here."

    def _default_reason(self, request: Request) -> str:
        if request.action == "tool":
            what = summarise(request.tool, request.tool_input)
            return (
                f"Not permitted here: {what}. Nothing allows it, and the default "
                "is no. Try something within the agreed limits, or ask a person."
            )
        if not request.principal.known:
            return "I don't know you, so I can't pass anything to an agent."
        return "You aren't allowed to use that agent in this channel."


class ClosedPolicy:
    """Denies everything, with the reason it is closed.

    Used when policy could not be loaded. A broken policy file must not mean an
    open door, and it must not mean a silent one either.
    """

    def __init__(self, why: str):
        self.why = why
        self.roster: dict[str, str] = {}

    def principal(self, slack_user_id: str) -> Principal:
        return Principal(id=slack_user_id)

    async def decide(self, request: Request) -> Decision:
        return Decision(allow=False, reason=f"Policy isn't loaded, so nothing is allowed: {self.why}")


def load_limits(path: str | os.PathLike[str]) -> tuple[int, int]:
    """Read the [limits] table. Declared with the roster, since it governs people."""
    p = Path(path).expanduser()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"{p} could not be read: {exc}") from exc
    table = raw.get("limits") or {}
    per_minute = int(table.get("turns_per_minute", 0) or 0)
    per_hour = int(table.get("turns_per_hour", 0) or 0)
    if per_minute < 0 or per_hour < 0:
        raise PolicyError(f"{p}: a negative limit means nothing.")
    return per_minute, per_hour


def load(path: str | os.PathLike[str]) -> RulesPolicy:
    p = Path(path).expanduser()
    if not p.exists():
        raise PolicyError(f"No policy at {p}. Copy policy.example.toml.")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"{p} could not be read: {exc}") from exc

    roster: dict[str, str] = {}
    for entry in raw.get("identity") or []:
        slack = str(entry.get("slack", "")).strip()
        email = str(entry.get("email", "")).strip()
        if not slack or not email:
            raise PolicyError(f"{p}: every [[identity]] needs both slack and email.")
        if slack in roster:
            raise PolicyError(f"{p}: {slack} is listed twice.")
        roster[slack] = email

    rules: list[Rule] = []
    for entry in raw.get("rule") or []:
        effect = str(entry.get("effect", "")).strip().lower()
        if effect not in {"allow", "deny"}:
            raise PolicyError(
                f"{p}: rule {entry.get('name', '?')!r} has effect {effect!r}; "
                "use allow or deny."
            )
        rules.append(
            Rule(
                effect=effect,
                name=str(entry.get("name", "")),
                actions=_tuple(entry.get("actions")),
                users=_tuple(entry.get("users")),
                channels=_tuple(entry.get("channels")),
                agents=_tuple(entry.get("agents")),
                tools=_tuple(entry.get("tools")),
                path_under=str(entry.get("path_under", "")).strip(),
                path_matches=_tuple(entry.get("path_matches")),
                command_matches=_tuple(entry.get("command_matches")),
                obligations=_tuple(entry.get("obligations")),
            )
        )

    if not rules:
        raise PolicyError(f"{p} has no rules, so nothing would be allowed.")
    return RulesPolicy(rules=rules, roster=roster)


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)
