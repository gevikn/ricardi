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

from ricardi.policy import PLANE2_VERBS, Decision, PolicyPoint, Principal, Request

# A human changing access does it a handful of times; a stuck client does it
# forever. Unlike the ask ceilings, these default *on*, because the two
# failures are not alike. An unlimited ask is bounded by the per-turn budget in
# `agents.toml` and by someone's patience. An unlimited `grant` is bounded by
# neither and appends to the one file in this deployment that is deliberately
# never truncated -- the rows are the record of who let whom in, so nothing
# prunes them. A ceiling only an operator can switch on is a ceiling the live
# box does not have. Write 0 to turn these off on purpose.
GRANTS_PER_MINUTE = 6
GRANTS_PER_HOUR = 30


@dataclass(frozen=True)
class Limits:
    """Zero means no ceiling, which on an always-on machine is a choice."""

    per_minute: int = 0
    per_hour: int = 0
    grants_per_minute: int = GRANTS_PER_MINUTE
    grants_per_hour: int = GRANTS_PER_HOUR

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
        # A separate window, not a shared one. Changing access twice must not
        # cost someone the turns they came for, and a grant loop must not be
        # excused by a quiet hour of asking.
        self._plane2: dict[str, deque[float]] = defaultdict(deque)

    def principal(self, slack_user_id: str) -> Principal:
        resolve = getattr(self._inner, "principal", None)
        if resolve is None:
            return Principal(id=slack_user_id)
        return resolve(slack_user_id)

    async def decide(self, request: Request) -> Decision:
        decision = await self._inner.decide(request)
        if not decision.allow:
            return decision

        # Only a new turn counts. Tool calls happen inside a turn already
        # permitted, and listing the agents costs nothing.
        if request.action == "ask":
            return self._count(
                request, decision, self._seen,
                self._limits.per_minute, self._limits.per_hour,
                "asked for more than I'll run",
            )
        # Plane 2 -- grant, revoke, reset, scope. Taken from the set rather
        # than listed here, so a verb added there cannot quietly arrive
        # without a ceiling.
        if request.action in PLANE2_VERBS:
            return self._count(
                request, decision, self._plane2,
                self._limits.grants_per_minute, self._limits.grants_per_hour,
                "changed access more often than I'll record",
            )
        return decision

    def _count(
        self,
        request: Request,
        decision: Decision,
        windows: dict[str, deque[float]],
        per_minute: int,
        per_hour: int,
        what: str,
    ) -> Decision:
        if per_minute <= 0 and per_hour <= 0:
            return decision

        who = request.principal.id or request.principal.email or "unknown"
        now = self._now()
        seen = windows[who]
        while seen and now - seen[0] > 3600:
            seen.popleft()

        minute = sum(1 for t in seen if now - t <= 60)
        if per_minute and minute >= per_minute:
            return self._refuse(seen, now, 60, "in the last minute", what)
        if per_hour and len(seen) >= per_hour:
            return self._refuse(seen, now, 3600, "in the last hour", what)

        seen.append(now)
        return decision

    def _refuse(
        self, seen: deque[float], now: float, window: float, phrase: str, what: str
    ) -> Decision:
        oldest = next((t for t in seen if now - t <= window), now)
        wait = max(1, int(window - (now - oldest)) + 1)
        when = f"{wait} seconds" if wait < 90 else f"{wait // 60} minutes"
        return Decision(
            allow=False,
            reason=f"You've {what} {phrase}. Try again in about {when}.",
            rule="rate limit",
        )

    @property
    def rules(self):
        return getattr(self._inner, "rules", ())

    @property
    def roster(self):
        return getattr(self._inner, "roster", {})
