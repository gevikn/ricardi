"""A provisioning request: what a session owner asks for, before it exists.

An owner cannot ask for a session in Slack, because what a request produces
*is* the channel. And the dangerous half of provisioning -- minting a key,
creating a repository, registering a name -- must not be reachable from a
channel at all. So a request is a file in a pull request: the owner writes it,
CI runs `check`, and an admin reviews a diff.

That shape is not a workaround. It is the same shape as `policy.toml`: the
thing that grants is a file two people read, and the escalation step is a human
pressing merge. It also needs no inbound port, which is most of why this
deployment is simple.

This module only reads and refuses. Applying a request -- cloning, registering,
creating the channel, writing grants -- is deliberately elsewhere, because an
applier has to decide things a checker does not.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# The three person-tiers. `exec` is absent on purpose: what a shell may do is
# the *session's* scope, not a person's tier. See the design note in
# docs/superpowers/specs/2026-10-28-shared-sessions-design.md, section 8.
TIERS = ("none", "read", "write")

# Names the system already answers to. `me` and `us` are here because the
# router reads "tell me the agents" as a listing, so an agent called `me` would
# be unaddressable by the one phrasing people reach for.
RESERVED = ("ricardi", "setup", "admin", "me", "us")

_TOP = {"workspace", "name", "owner", "invitee", "repo", "limits"}
_OWNER = {"slack", "email", "git"}
_INVITEE = {"slack", "email", "git", "tier"}
_REPO = {"name", "create_if_missing"}
_LIMITS = {"max_budget_usd", "max_turns"}


class RequestError(Exception):
    """The request is not fit to review, with a reason worth printing."""


@dataclass(frozen=True)
class Person:
    """Someone in the session. `tier` is empty for the owner."""

    slack: str
    email: str
    git: str
    tier: str = ""


@dataclass(frozen=True)
class Repo:
    name: str
    create_if_missing: bool = False


@dataclass(frozen=True)
class SessionRequest:
    workspace: str
    name: str
    owner: Person
    invitees: tuple[Person, ...] = ()
    repo: Repo | None = None
    max_budget_usd: float = 0.0
    max_turns: int = 0


def load(
    path: str | os.PathLike[str],
    *,
    existing_names: tuple[str, ...] | list[str] = (),
    max_budget_usd: float = 0.0,
) -> SessionRequest:
    """Read one request and refuse it if a human should not have to.

    `existing_names` is the catalogue, so a collision is caught here rather
    than at the restart that would have applied it. `max_budget_usd` is the
    admin's ceiling; 0 means no ceiling is being applied at this step.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise RequestError(f"No request at {p}.")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RequestError(f"{p} could not be read: {exc}") from exc

    _only(raw, _TOP, "the request")

    workspace = str(raw.get("workspace", "")).strip()
    if not workspace:
        raise RequestError(f"{p} needs a workspace.")

    name = _name(p, str(raw.get("name", "")).strip(), existing_names)
    owner = _person(raw.get("owner"), "the owner", _OWNER, tier=False)
    invitees = tuple(
        _person(one, f"invitee {i + 1}", _INVITEE, tier=True)
        for i, one in enumerate(raw.get("invitee") or [])
    )
    _people_make_sense(owner, invitees)
    budget, turns = _limits(p, raw.get("limits"), max_budget_usd)

    return SessionRequest(
        workspace=workspace,
        name=name,
        owner=owner,
        invitees=invitees,
        repo=_repo(raw.get("repo")),
        max_budget_usd=budget,
        max_turns=turns,
    )


def _only(table: dict, allowed: set[str], what: str) -> None:
    """Refuse a key nobody will read.

    Refused rather than ignored, for the reason an unknown flag is: a setting
    that looks like it asks for something and silently does nothing is worse
    than one that fails. `teir = "read"` would otherwise provision someone with
    no tier at all.
    """
    extra = sorted(set(table) - allowed)
    if extra:
        raise RequestError(
            f"I don't understand {', '.join(repr(k) for k in extra)} in {what}. "
            f"I know: {', '.join(sorted(allowed))}."
        )


def _name(p: Path, name: str, existing: tuple[str, ...] | list[str]) -> str:
    if not name:
        raise RequestError(f"{p} needs a name.")
    # The same rule the catalogue applies, because this name ends up there and
    # a request that passes here and fails at startup has wasted the review.
    if not name.replace("-", "").replace("_", "").isalnum():
        raise RequestError(
            f"Name {name!r} must be letters, digits, hyphens or underscores. "
            "It is what people type in Slack."
        )
    if name.lower() in RESERVED:
        raise RequestError(f"Name {name!r} is reserved. Pick another.")
    if name.lower() in {str(e).lower() for e in existing}:
        raise RequestError(f"Name {name!r} is already in use.")
    return name


def _person(raw, what: str, allowed: set[str], *, tier: bool) -> Person:
    if not isinstance(raw, dict):
        raise RequestError(f"{what} is missing.")
    _only(raw, allowed, what)
    slack = str(raw.get("slack", "")).strip()
    email = str(raw.get("email", "")).strip()
    git = str(raw.get("git", "")).strip()
    if not slack:
        raise RequestError(f"{what} needs a slack id.")
    if not email:
        raise RequestError(f"{what} needs an email.")
    if not git:
        # Nothing downstream can work this out: GitHub will not resolve a user
        # by email unless they have made it public. The person inviting knows
        # it, so it is collected here or not at all.
        raise RequestError(f"{what} needs a git username.")
    if not tier:
        return Person(slack=slack, email=email, git=git)

    asked = str(raw.get("tier", "")).strip().lower()
    if asked == "exec":
        raise RequestError(
            f"{what} asks for tier 'exec', which is not a person's tier. What "
            "a shell may do is the scope of the session, set on the session "
            f"itself. Use one of: {', '.join(TIERS)}."
        )
    if asked not in TIERS:
        raise RequestError(
            f"{what} has tier {asked or '(none given)'!r}. "
            f"I know: {', '.join(TIERS)}."
        )
    return Person(slack=slack, email=email, git=git, tier=asked)


def _people_make_sense(owner: Person, invitees: tuple[Person, ...]) -> None:
    # Checked before the duplicate scan so the clearer message wins: listing
    # the owner again is either a mistake or an attempt to give them a
    # different tier from the one owning it confers.
    for one in invitees:
        if one.slack == owner.slack or one.email.lower() == owner.email.lower():
            raise RequestError(
                "The owner is also listed as an invitee. Their tier comes from "
                "owning the session; remove the invitee entry."
            )

    for field, label in (("slack", "slack id"), ("email", "email")):
        seen: set[str] = set()
        for one in invitees:
            value = getattr(one, field).lower()
            if value in seen:
                # The roster is keyed on email, so two people sharing one are
                # one principal and every rule about them covers both.
                raise RequestError(f"{label} {value!r} is listed twice.")
            seen.add(value)


def _limits(p: Path, raw, ceiling: float) -> tuple[float, int]:
    if not isinstance(raw, dict):
        raise RequestError(
            f"{p} needs a [limits] table. On a machine left running, a session "
            "with no ceiling has to be something somebody chose."
        )
    _only(raw, _LIMITS, "[limits]")
    budget = float(raw.get("max_budget_usd", 0) or 0)
    turns = int(raw.get("max_turns", 0) or 0)
    # `agents.toml` lets 0 mean "no ceiling", deliberately. A request may not:
    # an unbounded session is an admin's decision, not an owner's default.
    if budget <= 0:
        raise RequestError("max_budget_usd must be above zero.")
    if turns <= 0:
        raise RequestError("max_turns must be above zero.")
    if ceiling > 0 and budget > ceiling:
        raise RequestError(
            f"max_budget_usd {budget} is over the ceiling of {ceiling}."
        )
    return budget, turns


def _repo(raw) -> Repo | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RequestError("[repo] is not a table.")
    _only(raw, _REPO, "[repo]")
    name = str(raw.get("name", "")).strip()
    if "/" not in name.strip("/") or name.count("/") != 1:
        raise RequestError(
            f"Repo {name!r} should be owner/repo, so there is no guessing about "
            "which account it belongs to."
        )
    return Repo(name=name, create_if_missing=bool(raw.get("create_if_missing", False)))
