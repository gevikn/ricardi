"""The parse is exact, so these are the whole command surface."""

import pytest

from ricardi import router


@pytest.mark.parametrize(
    "text,kind",
    [
        ("help", "help"),
        ("?", "help"),
        ("", "empty"),
        ("   ", "empty"),
        ("list agents", "list"),
        ("list", "list"),
        ("agents", "list"),
        ("Agents", "list"),
        ("list the agents", "list"),
        ("who", "who"),
        ("who?", "who"),
        ("deploy PR 412 to stage", "say"),
    ],
)
def test_bare_commands(text, kind):
    assert router.parse(text).kind == kind


@pytest.mark.parametrize(
    "text,agent,body",
    [
        ("tell scout to look at auth.py", "scout", "look at auth.py"),
        ("tell scout look at auth.py", "scout", "look at auth.py"),
        ("ask scout what changed", "scout", "what changed"),
        ("TELL Scout to run the tests", "Scout", "run the tests"),
        ("scout: what changed", "scout", "what changed"),
        ("deploy-bot: ship it", "deploy-bot", "ship it"),
        ("tell deploy_bot to ship it", "deploy_bot", "ship it"),
    ],
)
def test_addressed(text, agent, body):
    command = router.parse(text)
    assert command.kind == "tell"
    assert command.agent == agent
    assert command.text == body


def test_tell_with_nothing_to_say_just_binds():
    command = router.parse("tell scout")
    assert command.kind == "tell"
    assert command.agent == "scout"
    assert command.text == ""


def test_tell_me_the_agents_is_a_listing_not_an_agent():
    # "me" is not an agent, and this phrasing is natural enough to matter.
    assert router.parse("tell me the agents").kind == "list"
    assert router.parse("tell us the agents").kind == "list"


def test_multiline_body_survives():
    command = router.parse("tell scout to apply this:\nline one\nline two")
    assert command.kind == "tell"
    assert command.text == "apply this:\nline one\nline two"


def test_a_sentence_that_starts_with_a_word_and_colon_is_not_hijacked():
    # A colon after several words is punctuation, not addressing.
    assert router.parse("note: the build is red").kind == "tell"
    assert router.parse("the plan is this: ship it").kind == "say"


def test_the_message_is_kept_as_typed():
    # What someone typed, after flags come off and before it is interpreted.
    # A caller that disagrees with the interpretation -- "tell me ..." read as
    # an agent called "me" -- needs the original to fall back on.
    assert router.parse("tell me what version we are").raw == "tell me what version we are"
    assert router.parse("-ro tell me the thing").raw == "tell me the thing"
    assert router.parse("scout: look").raw == "scout: look"
