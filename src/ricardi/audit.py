"""A durable record of every decision.

Two people agreeing what Claude may do on their box only holds if they can see
what it actually did. Slack shows the announced commands; this is the complete
record, including everything that was refused and never reached the channel.

One JSON object per line, append-only. Nothing in here is a secret: tool inputs
are summarised and truncated rather than written out, because a log that
faithfully records every argument is a log that eventually records a password.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from ricardi.policy import Decision, PolicyPoint, Principal, Request, summarise

log = logging.getLogger("ricardi.audit")

# Long enough to identify a command, short enough that a pasted blob is cut off.
MAX_SUMMARY = 400


class AuditLog:
    """Appends one line per decision. Never raises into the caller."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None,
        max_bytes: int = 5 * 1024 * 1024,
        keep: int = 5,
    ):
        self.path = Path(path) if path else None
        # Rotated rather than unbounded: this file grows with every decision
        # for as long as the machine is up, and nothing else prunes it.
        self.max_bytes = max_bytes
        self.keep = max(0, keep)
        # Several turns can be decided at once, and a torn line is a useless
        # line. The write itself is small, so a plain lock is enough.
        self._lock = threading.Lock()
        self.failures = 0

    def record(self, request: Request, decision: Decision, error: str = "") -> None:
        if self.path is None:
            return
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": request.action,
            "allow": decision.allow,
            "rule": decision.rule or ("" if decision.allow else "default deny"),
            "who": request.principal.email or request.principal.id or "unknown",
            "who_id": request.principal.id,
            "channel": request.resource.channel_id,
            "agent": request.agent,
        }
        if request.tool:
            entry["tool"] = request.tool
            entry["what"] = summarise(request.tool, request.tool_input)[:MAX_SUMMARY]
        if not decision.allow and decision.reason:
            entry["reason"] = decision.reason[:MAX_SUMMARY]
        if error:
            entry["error"] = error[:MAX_SUMMARY]

        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_full()
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError as exc:
            # Deliberately not fatal. A full disk should not take the whole
            # system down, and the decision itself was still made correctly --
            # what is lost is the record of it. Loud, so it is not lost quietly.
            self.failures += 1
            log.error("could not write the audit log (%s failures so far): %s",
                      self.failures, exc)


    def _rotate_if_full(self) -> None:
        """Move the log aside once it is large, keeping a few generations.

        Size rather than time: a quiet week should not rotate a nearly empty
        file away, and a busy afternoon should not be allowed to fill a disk.
        """
        if self.path is None or self.max_bytes <= 0:
            return
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return

        if self.keep == 0:
            self.path.unlink(missing_ok=True)
            return

        # Oldest first, so nothing is overwritten before it is moved.
        oldest = self.path.with_name(self.path.name + "." + str(self.keep))
        oldest.unlink(missing_ok=True)
        for n in range(self.keep - 1, 0, -1):
            src = self.path.with_name(self.path.name + "." + str(n))
            if src.exists():
                src.replace(self.path.with_name(self.path.name + "." + str(n + 1)))
        self.path.replace(self.path.with_name(self.path.name + ".1"))


class AuditedPolicy:
    """Wraps a decision point so every answer is recorded.

    A wrapper rather than a call at each enforcement point: there are two of
    those today and there will be more, and the one that forgets to log is the
    one that matters.
    """

    def __init__(self, inner: PolicyPoint, log_file: AuditLog):
        self._inner = inner
        self._log = log_file

    def principal(self, slack_user_id: str) -> Principal:
        resolve = getattr(self._inner, "principal", None)
        if resolve is None:
            return Principal(id=slack_user_id)
        return resolve(slack_user_id)

    async def decide(self, request: Request) -> Decision:
        try:
            decision = await self._inner.decide(request)
        except Exception as exc:
            # A policy that failed is a decision worth recording too; the
            # enforcement points turn this into a denial.
            self._log.record(request, Decision(allow=False, rule="policy error"), str(exc))
            raise
        self._log.record(request, decision)
        return decision

    # Pass through what the CLI prints at startup.
    @property
    def rules(self):
        return getattr(self._inner, "rules", ())

    @property
    def roster(self):
        return getattr(self._inner, "roster", {})
