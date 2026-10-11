"""Does the credential still work? Asked on a timer, answered in Slack.

Ricardi's two halves fail independently, and that is the whole opportunity.
Slack is a WebSocket this process dials out and keeps. Claude Code is a child
process holding a credential that expires on a schedule nobody documents. When
the second one dies the first is still perfectly healthy, so Ricardi stays
connected, logged in, and answers nothing -- which from inside a channel is
indistinguishable from the box being switched off. docs/running.md says so in
as many words, and that sentence is the bug this module closes: the half that
still works reports the half that does not.

Why a real turn rather than something cheaper. Nothing short of one proves the
credential. `claude --version` does not authenticate; the OAuth token's expiry
is not readable from the environment variable that carries it; an API key looks
identical whether or not it has been revoked. The probe costs a fraction of a
cent and is the only check that cannot be quietly wrong.

What this deliberately does not do: watch real turns. A turn that fails for a
person has already told that person -- they are looking at the error in the
thread. The gap worth closing is the quiet weekend, when the credential dies
and nobody is there to ask.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

log = logging.getLogger("ricardi.health")

# An hour between checks while healthy. The cost of being wrong here is
# measured in hours of a soak nobody can trust, so this need not be minutes.
DEFAULT_EVERY = 3600.0
# After a failure, ask again soon rather than waiting out the whole interval:
# a single blip should not take an hour to rule out.
DEFAULT_RETRY = 60.0
# How many in a row before saying anything. One failed probe is a blip, three
# is a condition. A monitor that cries wolf gets muted, and a muted monitor is
# worth less than no monitor.
DEFAULT_STREAK = 3
# One two-word turn should take seconds. Longer means something is wrong in a
# way the check should report rather than sit through.
DEFAULT_TIMEOUT = 120.0
# Slack is not the place for a thousand lines of stderr.
MAX_ERROR = 600


class ProbeFailed(Exception):
    """The check ran, and the answer was no."""


# Tokens look enough like words that an error message can carry one. This text
# gets posted into a channel, so anything shaped like a credential comes out.
_SECRET = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{4,}|xox[abprs]-[A-Za-z0-9-]{4,}|xapp-[A-Za-z0-9-]{4,})"
)


def redact(text: str) -> str:
    """Mask anything shaped like a credential.

    An authentication error is exactly the kind that quotes back what it was
    given, and this ends up in a Slack channel. Posting a live token into a
    channel in order to announce that the token has expired would be a poor
    trade.
    """
    return _SECRET.sub("[redacted]", text or "")


def describe(exc: BaseException) -> str:
    """One readable line, or a few, for an exception -- safe to post."""
    text = f"{type(exc).__name__}: {exc}".strip()
    # ProcessError carries the CLI's stderr, which is where the reason is.
    stderr = getattr(exc, "stderr", None)
    if stderr:
        text = f"{text}\n{str(stderr).strip()}"
    text = redact(text).strip()
    if len(text) > MAX_ERROR:
        text = text[:MAX_ERROR].rstrip() + "..."
    return text or type(exc).__name__


# Matched case-insensitively against the whole error text. This wording has
# changed more than once upstream, which is exactly why classification only
# chooses the advice and never whether to alert -- see classify().
_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "auth",
        (
            "oauth token has expired",
            "oauth token expired",
            "invalid bearer token",
            "authentication_error",
            "invalid api key",
            "invalid x-api-key",
            "please run /login",
            "not logged in",
            "401 unauthorized",
        ),
    ),
    (
        "billing",
        ("credit balance is too low", "insufficient credit", "payment required"),
    ),
    (
        "limit",
        ("rate limit", "usage limit", "too many requests", "429"),
    ),
)


def classify(text: str) -> str:
    """One of "auth", "billing", "limit", or "" for anything else.

    This picks the wording of the alert and nothing else. A probe that fails
    for a reason nobody anticipated still means Ricardi cannot answer, so it
    is still reported: a monitor that stays quiet because it did not recognise
    an error is worse than no monitor at all, because silence from it reads as
    reassurance.
    """
    low = (text or "").lower()
    for kind, needles in _KINDS:
        if any(n in low for n in needles):
            return kind
    return ""


ADVICE = {
    "auth": (
        "Re-issue the credential in `.env` on the host and restart me: "
        "`claude setup-token` for a subscription token, or an API key from the "
        "Anthropic Console, which does not expire on a clock."
    ),
    "billing": (
        "That reads as billing rather than a dead credential, so top up the "
        "account the key belongs to. Nothing on the box needs changing."
    ),
    "limit": (
        "That reads as a usage limit rather than a dead credential, so it "
        "should clear on its own. I will say when it does."
    ),
    "": (
        "I do not recognise that error, so it may not be the credential at "
        "all. The box's log has the whole traceback."
    ),
}

# Only worth saying when the fix is a new credential. On a billing or rate
# limit problem it is advice about something nobody is about to do.
_PRECEDENCE_WARNING = (
    "Do not sign in on the box to fix it: a stored login takes precedence over "
    "`.env` and would shadow the new credential. `docs/running.md` has the "
    "detail."
)


def _ago(seconds: float) -> str:
    """A duration a person reads without converting anything."""
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


def _result_text(message: object) -> str:
    """Whatever a failed ResultMessage can tell us, in one line."""
    bits = [str(getattr(message, "subtype", "") or "")]
    for attr in ("api_error_status", "result", "errors"):
        value = getattr(message, attr, None)
        if value:
            bits.append(str(value))
    return " ".join(b for b in bits if b).strip() or "the turn ended without success"


def sdk_probe(timeout: float = DEFAULT_TIMEOUT) -> Callable[[], Awaitable[None]]:
    """A probe that runs one throwaway turn through the Agent SDK.

    In a session of its own, not an agent's: a ping in an agent's transcript
    would sit in that agent's context for the rest of its life, and a check
    that the people using it can see is not worth having.

    No tool can run. The same PreToolUse deny the backend relies on is
    installed here too -- not because a two-word prompt would call one, but
    because SECURITY.md claims no turn anywhere runs a tool without passing a
    gate, and a turn that quietly skipped it would make that false.
    """

    async def probe() -> None:
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            HookMatcher,
            ResultMessage,
            query,
        )

        async def deny(payload: dict, tool_use_id: str | None, context: object):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "The health check runs no tools.",
                }
            }

        options = ClaudeAgentOptions(
            max_turns=1,
            setting_sources=[],
            system_prompt="You are a health check. Reply with the single word: ok",
            hooks={"PreToolUse": [HookMatcher(hooks=[deny])]},
        )

        async def once() -> None:
            saw_result = False
            async for message in query(prompt="ok", options=options):
                if isinstance(message, ResultMessage):
                    saw_result = True
                    failed = getattr(message, "is_error", False)
                    if failed or message.subtype != "success":
                        raise ProbeFailed(redact(_result_text(message)))
            if not saw_result:
                # The CLI exited without ever saying how the turn ended, which
                # is what a process that cannot start looks like from here.
                raise ProbeFailed("the CLI exited without a result")

        await asyncio.wait_for(once(), timeout=timeout)

    return probe


class CredentialWatch:
    """Probes on a timer and announces the transitions, not the ticks.

    Stateful, because the useful event is the change. A channel told once that
    authentication is down, and once more when it comes back, is a monitor.
    The same message every hour is noise, and noise gets muted, after which it
    reports nothing at all.
    """

    def __init__(
        self,
        probe: Callable[[], Awaitable[None]],
        announce: Callable[[str], Awaitable[None]] | None = None,
        every: float = DEFAULT_EVERY,
        retry: float = DEFAULT_RETRY,
        streak: int = DEFAULT_STREAK,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._probe = probe
        self._announce = announce
        self._every = every
        self._retry = retry
        self._streak = max(1, streak)
        # Injected so the loop can be tested without waiting an hour for it.
        self._clock = clock
        self._sleep = sleep
        self._failures = 0
        self._first_failure = 0.0
        self._last_error = ""
        # Whether the channel has been told. Not the same as "is it failing":
        # two failures are a condition nobody has been bothered with yet.
        self._announced = False

    @property
    def announced(self) -> bool:
        return self._announced

    @property
    def failures(self) -> int:
        return self._failures

    async def check(self) -> bool:
        """One probe. Announces a transition, and never raises."""
        try:
            await self._probe()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - a probe may fail any way
            self._failures += 1
            self._last_error = (
                redact(str(exc)) if isinstance(exc, ProbeFailed) else describe(exc)
            )
            if self._failures == 1:
                self._first_failure = self._clock()
            log.warning(
                "credential check failed (%d in a row): %s",
                self._failures,
                self._last_error,
            )
            if self._failures >= self._streak and not self._announced:
                self._announced = True
                await self._say(self.down_message(), logging.ERROR)
            return False

        if self._announced:
            self._announced = False
            down_for = max(0.0, self._clock() - self._first_failure)
            self._failures = 0
            await self._say(self.up_message(down_for), logging.WARNING)
            return True
        if self._failures:
            log.info("credential ok, after %d failure(s)", self._failures)
        else:
            # One line an hour, which doubles as a heartbeat: on a box nobody
            # is sitting at, the last of these in state/service.log is when
            # Ricardi was last alive and authenticating.
            log.info("credential ok")
        self._failures = 0
        return True

    async def run(self) -> None:
        """Check now, then keep checking. Cancel the task to stop it.

        Immediately rather than after the first interval, because the moment a
        restart matters most is the one just after it: a box that came back up
        with a dead credential should not look healthy for an hour.
        """
        log.info(
            "credential check every %s, %s after a failure, announcing at %d in a row",
            _ago(self._every),
            _ago(self._retry),
            self._streak,
        )
        while True:
            ok = await self.check()
            # The fast retry applies only while a failure is unconfirmed. Once
            # the channel has been told there is nothing to hurry for, and the
            # ordinary interval is also how recovery gets noticed.
            await self._sleep(self._every if ok or self._announced else self._retry)

    # -- what it says -----------------------------------------------------
    def down_message(self) -> str:
        kind = classify(self._last_error)
        lines = [
            "*I can't reach Claude.* Slack is fine -- this is the credential on "
            "the box, so every agent will fail until it is dealt with.",
            "",
            f"Failed {self._failures} checks in a row. Last error:",
            f"```{self._last_error}```",
            "",
            ADVICE[kind],
        ]
        if kind in {"auth", ""}:
            lines += ["", _PRECEDENCE_WARNING]
        return "\n".join(lines)

    def up_message(self, down_for: float) -> str:
        return (
            "*Authentication is working again* -- back to answering normally. "
            f"It was failing for about {_ago(down_for)}, so anything anyone "
            "asked in that window got nothing."
        )

    async def _say(self, text: str, level: int) -> None:
        """Log it always; post it when there is somewhere to post.

        The log is the fallback monitor: a deployment with no alert channel
        configured still leaves the whole story in `state/service.log`, which
        is the file someone goes to when the bot has gone quiet.
        """
        log.log(level, "%s", text.replace("\n", " | "))
        if self._announce is None:
            return
        try:
            await self._announce(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Failing to post must not stop the watching. The likeliest cause
            # is the bot not being in the channel it was pointed at.
            log.exception("could not post the credential alert")
