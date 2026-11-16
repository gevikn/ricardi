"""What a person reading Slack sees of each kind of event."""

from ricardi.events import (
    Diffed, Envelope, Event, Pending, Refused, Said, Thought, ToolFinished,
    ToolOutput, ToolStarted, TurnEnded, TurnStarted, Unknown, _BODIES,
)
from ricardi.render import MAX_ANSWER, RENDERERS, render


def event(body, name="ricardi"):
    return Event(Envelope(seq=1, at="2026-11-10T15:04:05Z", name=name, turn="t1",
                          actor="agent"), body)


def test_a_said_event_renders_as_the_agents_answer():
    assert render(event(Said(text="hello"), name="ricardi")) == "*ricardi*\nhello"


def test_a_long_answer_is_truncated_here_rather_than_in_the_backend():
    # The stream carries the whole thing; only this renderer has a 3500-char
    # channel to fit it into.
    out = render(event(Said(text="x" * 5000)))
    assert len(out) < 5000 and "truncated" in out
    assert "5000 characters in all" in out
    assert out.count("x") == MAX_ANSWER


def test_an_answer_at_the_limit_is_not_truncated():
    assert "truncated" not in render(event(Said(text="x" * MAX_ANSWER)))


def test_a_tool_call_renders_as_one_line_and_never_its_arguments():
    assert render(event(ToolStarted("Bash", "Bash: ls -la", "tu_1"))) == "_ricardi_ `Bash: ls -la`"


def test_a_diff_degrades_to_a_sentence_rather_than_vanishing():
    # §13: slack.py degrades to text what it cannot show.
    out = render(event(Diffed("src/a.py", ("@@", "-a", "+b"))))
    assert "src/a.py" in out
    assert "-a" not in out and "+b" not in out


def test_a_refused_tool_shows_its_reason():
    body = Refused("tier", "You hold read on ricardi.", "read", "Edit", "")
    assert "You hold read on ricardi." in render(event(body))


def test_an_inbound_refusal_renders_empty_because_it_already_reached_the_person_privately():
    body = Refused("default deny", "You can't talk to ricardi.", "", "", "ricardi")
    assert render(event(body)) == ""


def test_a_pending_question_shows_the_question_and_its_options():
    out = render(event(Pending("c1", "Run `git push`?", ("yes", "no"))))
    assert "Run `git push`?" in out and "yes" in out and "no" in out


def test_a_failed_tool_says_so_and_a_successful_one_stays_quiet():
    assert render(event(ToolFinished("tu_1", "Bash", True, 12))) == ""
    assert "Bash" in render(event(ToolFinished("tu_1", "Bash", False, 12)))


def test_a_turn_that_did_not_finish_says_how_and_one_that_did_stays_quiet():
    assert render(event(TurnEnded("done", "all of it"))) == ""
    for outcome in ("cut_short", "failed", "timed_out"):
        out = render(event(TurnEnded(outcome, "partial")))
        assert out and "partial" not in out


def test_the_kinds_this_renderer_stays_quiet_about_render_empty():
    for body in (Thought(text="hmm"), ToolOutput("tu_1", "ok", False),
                 TurnStarted(text="go")):
        assert render(event(body)) == ""


def test_every_kind_has_a_decided_rendering():
    # A kind added later must not fall through to a silent default.
    for kind in _BODIES:
        assert kind in RENDERERS


def test_a_kind_this_renderer_does_not_know_renders_empty_rather_than_raising():
    assert render(event(Unknown(kind="from_the_future", fields={}))) == ""
