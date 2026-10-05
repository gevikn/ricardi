"""The Slack front end, over Socket Mode.

Socket Mode dials out to Slack over a WebSocket, so the box Claude Code runs on
needs no public hostname, no inbound port and no request-signature checking.
That removes the whole exposed-endpoint tier the obvious design would need.

Two ways in:
  app_mention  someone said @ricardi in a channel
  message      a reply inside a thread Ricardi is already part of

The second is what makes a thread feel like a conversation instead of a series
of commands, and it needs the channels:history scope to arrive at all.
"""

from __future__ import annotations

import asyncio
import logging
import re

from ricardi.transports.base import Inbound, OnMessage

log = logging.getLogger("ricardi.slack")

# "<@U012ABC> tell scout to look" -- the mention is addressing, not content.
_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")
# Removing a mention mid-sentence leaves a gap. Runs of spaces and tabs close
# up; newlines stay, because a multi-line message was laid out deliberately.
_GAP = re.compile(r"[ \t]{2,}")


def strip_mentions(text: str) -> str:
    return _GAP.sub(" ", _MENTION.sub(" ", text or "")).strip()


def from_mention(event: dict) -> Inbound | None:
    """A channel mention addressed to Ricardi by a person.

    Another app, or a Slack workflow, can post a message that mentions us. That
    is not a person asking, and honouring it would let any automation in the
    workspace drive an agent.
    """
    if event.get("bot_id") or event.get("subtype"):
        return None
    if not event.get("user"):
        return None
    return Inbound(
        text=strip_mentions(event.get("text", "")),
        # A mention outside a thread starts one, rooted at its own timestamp.
        thread=event.get("thread_ts") or event.get("ts") or "",
        channel=event.get("channel", ""),
        user=event.get("user", ""),
    )


def from_thread_reply(event: dict, ours: set[str], bot_user_id: str = "") -> Inbound | None:
    """A plain message, which is only ours under narrow conditions.

    Returning None is the common case and the important one: a channel is full
    of people talking to each other, and none of it should reach an agent.
    """
    # Edits, joins, deletions and anything a bot said are not requests.
    if event.get("subtype") or event.get("bot_id"):
        return None
    thread = event.get("thread_ts") or ""
    # Not in a thread, or in a thread Ricardi was never part of.
    if not thread or thread not in ours:
        return None
    text = event.get("text", "")
    # A message mentioning Ricardi already arrived as app_mention; answering
    # it here too would double it. Mentions of anyone else are ordinary words
    # in a reply that is still addressed to us.
    if bot_user_id:
        if re.search(r"<@" + re.escape(bot_user_id) + r"(?:\|[^>]*)?>", text):
            return None
    elif _MENTION.search(text):
        # Without the bot's own id, any mention has to be treated as ours, or
        # a mention would be answered twice.
        return None
    if not text.strip():
        return None
    return Inbound(
        text=text.strip(),
        thread=thread,
        channel=event.get("channel", ""),
        user=event.get("user", ""),
    )


class SlackTransport:
    def __init__(self, bot_token: str, app_token: str):
        if not bot_token or not app_token:
            raise ValueError(
                "Slack needs both a bot token (xoxb-) and an app token (xapp-)"
            )
        self._bot_token = bot_token
        self._app_token = app_token
        self._app = None
        self._handler = None
        self._bot_user_id = ""
        # Threads Ricardi has spoken in. A plain reply is only ours to answer
        # if we are already in that thread; otherwise the channel's ordinary
        # conversation would be fed to an agent.
        self._ours: set[str] = set()
        # Turns in flight. Held so they are not garbage collected mid-run, and
        # so shutdown can stop them.
        self._turns: set[asyncio.Task[None]] = set()

    async def start(self, on_message: OnMessage) -> None:
        try:
            from slack_bolt.adapter.socket_mode.async_handler import (
                AsyncSocketModeHandler,
            )
            from slack_bolt.app.async_app import AsyncApp
        except ImportError as exc:
            raise RuntimeError(
                "Slack support isn't installed. Install ricardi with the 'slack' extra."
            ) from exc

        # AsyncApp does not check the token at construction, so a bad one shows
        # up at the auth_test below rather than here.
        app = AsyncApp(token=self._bot_token)
        self._app = app

        # Bolt sends Slack's acknowledgement only after the listener returns
        # (run_async_bolt_app awaits async_dispatch, then send_async_response).
        # An agent's turn takes minutes, so answering inside the listener would
        # hold the ack open and Slack would redeliver the event -- running the
        # same prompt against the agent twice. The turn goes to a task and the
        # listener returns at once.
        def dispatch(inbound: Inbound) -> None:
            task = asyncio.create_task(on_message(inbound))
            self._turns.add(task)
            task.add_done_callback(self._turns.discard)

        @app.event("app_mention")
        async def _mention(event):  # noqa: ANN001 - bolt supplies this
            inbound = from_mention(event)
            if inbound is None:
                return
            if inbound.thread:
                self._ours.add(inbound.thread)
            dispatch(inbound)

        @app.event("message")
        async def _message(event):  # noqa: ANN001
            inbound = from_thread_reply(event, self._ours, self._bot_user_id)
            if inbound is not None:
                dispatch(inbound)

        auth = await app.client.auth_test()
        self._bot_user_id = auth.get("user_id", "")
        log.info("connected to Slack as %s", auth.get("user", "?"))

        self._handler = AsyncSocketModeHandler(app, self._app_token)
        await self._handler.connect_async()

    async def send(self, channel: str, thread: str, text: str) -> None:
        if self._app is None:
            raise RuntimeError("transport not started")
        if thread:
            self._ours.add(thread)
        await self._app.client.chat_postMessage(
            channel=channel, thread_ts=thread or None, text=text
        )

    async def send_private(self, channel: str, thread: str, user: str, text: str) -> None:
        """Visible only to the person who asked.

        A refusal is between Ricardi and one person; posting it to the channel
        would turn every mistyped agent name into noise everyone reads.
        """
        if self._app is None:
            raise RuntimeError("transport not started")
        await self._app.client.chat_postEphemeral(
            channel=channel, user=user, thread_ts=thread or None, text=text
        )

    async def close(self) -> None:
        if self._handler is not None:
            await self._handler.disconnect_async()
            self._handler = None
        for task in list(self._turns):
            task.cancel()
        if self._turns:
            # Drained so cancellation finishes before the loop closes.
            await asyncio.gather(*list(self._turns), return_exceptions=True)
