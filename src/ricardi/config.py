"""Ricardi's agent catalogue.

The agents Slack can reach are declared, not discovered. A fixed catalogue
gives the humans in the channel stable names to address, and gives each agent a
working directory and a tool allowance decided ahead of time rather than by
whoever is typing.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentSpec:
    """What an agent is. What it may *do* lives in policy.toml.

    The split is deliberate: this file says which agents exist and where they
    work, policy says who may ask and which tools may run. Different lifecycles,
    different reviewers, and only one place that can grant anything.
    """

    name: str
    cwd: str
    description: str = ""
    system_prompt: str = ""
    # A ceiling on one turn. Zero means no ceiling, which on a machine left
    # running is a decision rather than a default.
    max_budget_usd: float = 0.0
    max_turns: int = 0


@dataclass(frozen=True)
class Settings:
    agents: tuple[AgentSpec, ...] = ()
    state_dir: Path | None = None
    # How long to work in silence before telling the channel we are on it.
    ack_after: float = 4.0
    # When the audit log is moved aside, and how many generations are kept.
    audit_max_mb: float = 5.0
    audit_keep: int = 5


class ConfigError(Exception):
    """The catalogue could not be read, with a reason worth printing."""





def load(path: str | os.PathLike[str]) -> Settings:
    p = Path(path).expanduser()
    if not p.exists():
        raise ConfigError(f"No agent catalogue at {p}. Copy agents.example.toml.")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{p} could not be read: {exc}") from exc

    entries = raw.get("agent") or []
    if not entries:
        raise ConfigError(f"{p} declares no [[agent]] entries.")

    agents: list[AgentSpec] = []
    seen: set[str] = set()
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ConfigError(f"{p} has an [[agent]] with no name.")
        # Names are how humans address agents in Slack, so they have to be
        # unambiguous and typeable in a mention.
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ConfigError(
                f"Agent name {name!r} must be letters, digits, hyphens or underscores."
            )
        if name.lower() in seen:
            raise ConfigError(f"{p} declares {name!r} twice.")
        seen.add(name.lower())

        cwd = str(entry.get("cwd", "")).strip()
        if not cwd:
            raise ConfigError(f"Agent {name!r} needs a cwd.")
        resolved = Path(cwd).expanduser()
        if not resolved.is_dir():
            raise ConfigError(f"Agent {name!r} points at {resolved}, which is not a directory.")

        # These used to live here and now belong to policy. Saying so beats
        # silently ignoring them, because either one looks like it grants
        # something and no longer does.
        for moved in ("allowed_tools", "permission_mode"):
            if moved in entry:
                raise ConfigError(
                    f"Agent {name!r} sets {moved}, which moved to policy.toml. "
                    "Tool permissions are decided there now, so that one file "
                    "grants everything. Remove it here."
                )

        budget = float(entry.get("max_budget_usd", 0) or 0)
        turns = int(entry.get("max_turns", 0) or 0)
        if budget < 0 or turns < 0:
            raise ConfigError(f"Agent {name!r} has a negative limit.")

        agents.append(
            AgentSpec(
                name=name,
                cwd=str(resolved),
                description=str(entry.get("description", "")).strip(),
                system_prompt=str(entry.get("system_prompt", "")).strip(),
                max_budget_usd=budget,
                max_turns=turns,
            )
        )

    settings = raw.get("ricardi") or {}
    state = settings.get("state_dir")
    state_dir = Path(str(state)).expanduser() if state else p.parent / "state"
    return Settings(
        agents=tuple(agents),
        state_dir=state_dir,
        ack_after=float(settings.get("ack_after", 4.0)),
        audit_max_mb=float(settings.get("audit_max_mb", 5.0)),
        audit_keep=int(settings.get("audit_keep", 5)),
    )
