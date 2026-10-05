"""Slack's event shapes, without Slack.

The transport's judgement -- what is addressed to Ricardi and what is two
colleagues talking -- is pure, so it is tested on real event payloads rather
than against a live workspace.
"""

import pytest

from ricardi.transports.slack import (
    SlackTransport,
    from_mention,
    from_thread_reply,
    strip_mentions,
)


def test_the_mention_comes_off_the_front():
    assert strip_mentions("<@U01RICARDI> list agents") == "list agents"


def test_a_named_mention_comes_off_too():
    assert strip_mentions("<@U01ABC|ricardi> help") == "help"


def test_a_mention_mid_sentence_comes_off():
    assert strip_mentions("ask <@U01ABC> about it") == "ask about it"


def test_text_without_a_mention_is_untouched():
    assert strip_mentions("deploy PR 412") == "deploy PR 412"


def test_a_mention_outside_a_thread_starts_one_at_its_own_timestamp():
    event = {
        "text": "<@U01C> list agents",
        "ts": "1700000000.000100",
        "channel": "C123",
        "user": "U999",
    }
    inbound = from_mention(event)
    assert inbound.thread == "1700000000.000100"
    assert inbound.text == "list agents"
    assert inbound.channel == "C123"
    assert inbound.user == "U999"


def test_a_mention_inside_a_thread_stays_in_that_thread():
    event = {
        "text": "<@U01C> carry on",
        "ts": "1700000009.000900",
        "thread_ts": "1700000000.000100",
        "channel": "C123",
        "user": "U999",
    }
    assert from_mention(event).thread == "1700000000.000100"


def test_a_reply_in_our_thread_is_ours():
    event = {"text": "and the tests?", "thread_ts": "T1", "channel": "C1", "user": "U1"}
    inbound = from_thread_reply(event, {"T1"})
    assert inbound is not None
    assert inbound.text == "and the tests?"


def test_a_reply_in_someone_elses_thread_is_ignored():
    event = {"text": "lunch?", "thread_ts": "T9", "channel": "C1"}
    assert from_thread_reply(event, {"T1"}) is None


def test_ordinary_channel_chatter_is_ignored():
    # No thread_ts at all: this is the channel, not a conversation with us.
    event = {"text": "morning all", "ts": "1700000000.000001", "channel": "C1"}
    assert from_thread_reply(event, {"T1"}) is None


def test_our_own_messages_are_ignored():
    event = {"text": "scout: done", "thread_ts": "T1", "bot_id": "B01RICARDI"}
    assert from_thread_reply(event, {"T1"}) is None


def test_edits_and_joins_are_ignored():
    for subtype in ("message_changed", "message_deleted", "channel_join", "thread_broadcast"):
        event = {"text": "whatever", "thread_ts": "T1", "subtype": subtype}
        assert from_thread_reply(event, {"T1"}) is None, subtype


def test_a_mention_in_a_thread_is_not_taken_twice():
    # app_mention already delivered this one.
    event = {"text": "<@U01C> carry on", "thread_ts": "T1"}
    assert from_thread_reply(event, {"T1"}) is None


def test_an_empty_reply_is_ignored():
    event = {"text": "   ", "thread_ts": "T1"}
    assert from_thread_reply(event, {"T1"}) is None


def test_a_reply_mentioning_someone_else_is_still_ours():
    # Regression: any mention at all used to drop the message, so "@dave should
    # see this" in a bound thread was silently lost.
    event = {"text": "<@U0DAVE> should see this", "thread_ts": "T1"}
    inbound = from_thread_reply(event, {"T1"}, bot_user_id="U01RICARDI")
    assert inbound is not None
    assert "U0DAVE" in inbound.text


def test_a_reply_mentioning_ricardi_is_left_to_app_mention():
    event = {"text": "<@U01RICARDI> carry on", "thread_ts": "T1"}
    assert from_thread_reply(event, {"T1"}, bot_user_id="U01RICARDI") is None


def test_without_our_own_id_any_mention_is_assumed_ours():
    # Before auth_test has answered, dropping is the safe guess: answering
    # twice is worse than answering late.
    event = {"text": "<@U0DAVE> look", "thread_ts": "T1"}
    assert from_thread_reply(event, {"T1"}) is None


def test_the_transport_builds_a_bolt_app_with_arguments_bolt_accepts():
    """A regression guard for a kwarg that does not exist.

    The transport was passing token_verification_enabled, which AsyncApp has
    never accepted, and nothing caught it until a live run. Constructing the
    app needs no network, so the wiring can be checked offline.
    """
    bolt = pytest.importorskip("slack_bolt.app.async_app")
    import asyncio

    transport = SlackTransport(bot_token="xoxb-not-real", app_token="xapp-not-real")

    async def build():
        # Stop right after the app is constructed and the listeners registered;
        # auth_test is the first thing needing Slack.
        try:
            await transport.start(lambda inbound: None)
        except Exception as exc:
            return exc
        return None

    outcome = asyncio.run(build())
    # Whatever went wrong must be Slack refusing the token, not Python refusing
    # the call.
    assert not isinstance(outcome, TypeError), outcome
    assert transport._app is not None
    assert isinstance(transport._app, bolt.AsyncApp)
