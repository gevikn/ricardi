"""How often one person may set an agent working.

Every turn spends money on the machine's own account, so an allowlisted person
in a loop is an expensive accident rather than an attack. This is the ceiling on
that, and it sits beside the per-turn ceilings in `agents.toml`: those bound what
one turn may cost, this bounds how many turns there are.

It wraps the decision point rather than sitting beside it, so a refusal here
reaches the audit log by the same path as any other — a limit nobody can see
being hit is a limit nobody will understand.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from ricardi.policy import Decision, PolicyPoint, Principal, Request


@dataclass(frozen=True)
class Limits:
    """Zero means no ceiling, which on an always-on machine is a choice."""

    per_minute: int = 0
    per_hour: int = 0

    @property
    def any(self) -> bool:
        return self.per_minute > 0 or self.per_hour > 0


class RateLimited:
    """Counts turns per person over a sliding window.

    In memory: a restart clears the counts. That is a deliberate trade — the
    alternative is a file written on every message to defend against someone
    who, being on the roster, could simply ask you to restart it. The per-turn
    budget in `agents.toml` is the ceiling that does survive.
    """

    def __init__(self, inner: PolicyPoint, limits: Limits, now=time.monotonic):
        self._inner = inner
        self._limits = limits
        self._now = now
        self._seen: dict[str, deque[float]] = defaultdict(deque)

    def principal(self, slack_user_id: str) -> Principal:
        resolve = getattr(self._inner, "principal", None)
        if resolve is None:
            return Principal(id=slack_user_id)
        return resolve(slack_user_id)

    async def decide(self, request: Request) -> Decision:
        decision = await self._inner.decide(request)

        # Only a new turn counts. Tool calls happen inside a turn already
        # permitted, and listing the agents costs nothing.
        if not decision.allow or request.action != "ask" or not self._limits.any:
            return decision

        who = request.principal.id or request.principal.email or "unknown"
        now = self._now()
        seen = self._seen[who]
        while seen and now - seen[0] > 3600:
            seen.popleft()

        minute = sum(1 for t in seen if now - t <= 60)
        if self._limits.per_minute and minute >= self._limits.per_minute:
            return self._refuse(seen, now, 60, "in the last minute")
        if self._limits.per_hour and len(seen) >= self._limits.per_hour:
            return self._refuse(seen, now, 3600, "in the last hour")

        seen.append(now)
        return decision

    def _refuse(self, seen: deque[float], now: float, window: float, phrase: str) -> Decision:
        oldest = next((t for t in seen if now - t <= window), now)
        wait = max(1, int(window - (now - oldest)) + 1)
        when = f"{wait} seconds" if wait < 90 else f"{wait // 60} minutes"
        return Decision(
            allow=False,
            reason=(
                f"You've asked for more than I'll run {phrase}. "
                f"Try again in about {when}."
            ),
            rule="rate limit",
        )

    @property
    def rules(self):
        return getattr(self._inner, "rules", ())

    @property
    def roster(self):
        return getattr(self._inner, "roster", {})
