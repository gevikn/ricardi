"""Reading the audit log back."""

import json
from datetime import datetime, timedelta, timezone

from ricardi.report import main, read, summarise, within


def write_log(path, entries):
    path.write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in entries) + "\n",
        encoding="utf-8",
    )
    return path


def entry(allow=True, **kw):
    base = {
        "at": "2026-10-05T12:00:00+00:00",
        "action": "tool",
        "allow": allow,
        "rule": "shell" if allow else "never",
        "who": "you@example.com",
        "channel": "C1",
        "agent": "bench",
    }
    base.update(kw)
    return base


def test_an_empty_log_says_so(tmp_path):
    assert "No decisions" in summarise([])


def test_the_headline_counts_both_outcomes(tmp_path):
    out = summarise([entry(), entry(), entry(allow=False)])
    assert "3 decisions" in out
    assert "2 allowed" in out
    assert "1 refused" in out


def test_refusals_are_grouped_by_the_rule_that_fired():
    out = summarise([
        entry(allow=False, rule="never", what="Bash: git push"),
        entry(allow=False, rule="never", what="Bash: git push"),
        entry(allow=False, rule="rate limit", action="ask"),
    ])
    assert "Why refused" in out
    assert "never" in out and "rate limit" in out
    # Repeats are collapsed rather than listed twice.
    assert "(x2)" in out


def test_allowed_tools_are_counted_but_refused_ones_are_not():
    out = summarise([
        entry(tool="Read", what="Read: /a"),
        entry(tool="Read", what="Read: /b"),
        entry(allow=False, tool="Bash", what="Bash: rm -rf /"),
    ])
    assert "Tools used" in out
    # Read was used twice; Bash never ran, so it belongs under refusals.
    tools = out.split("Tools used")[1].split("Why refused")[0]
    assert "Read" in tools
    assert "Bash" not in tools


def test_a_policy_failure_is_called_out():
    out = summarise([entry(allow=False, error="the decision service is down")])
    assert "policy itself failed" in out


def test_a_long_list_of_refusals_is_truncated():
    out = summarise([
        entry(allow=False, what="Bash: command " + str(i)) for i in range(40)
    ])
    assert "and 25 more" in out


# -- reading files --------------------------------------------------------
def test_a_torn_line_costs_one_decision_not_the_whole_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(entry()) + "\n" + '{"half written' + "\n" + json.dumps(entry()) + "\n",
        encoding="utf-8",
    )
    assert len(read(path)) == 2


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("\n" + json.dumps(entry()) + "\n\n", encoding="utf-8")
    assert len(read(path)) == 1


def test_rotated_generations_are_read_oldest_first(tmp_path):
    path = tmp_path / "audit.jsonl"
    write_log(path, [entry(what="newest")])
    write_log(tmp_path / "audit.jsonl.1", [entry(what="middle")])
    write_log(tmp_path / "audit.jsonl.2", [entry(what="oldest")])

    order = [e["what"] for e in read(path)]
    assert order == ["oldest", "middle", "newest"]


def test_rotated_generations_can_be_skipped(tmp_path):
    path = tmp_path / "audit.jsonl"
    write_log(path, [entry(what="newest")])
    write_log(tmp_path / "audit.jsonl.1", [entry(what="older")])
    assert [e["what"] for e in read(path, include_rotated=False)] == ["newest"]


def test_a_missing_live_file_still_reads_the_rotated_ones(tmp_path):
    write_log(tmp_path / "audit.jsonl.1", [entry(what="survived")])
    assert [e["what"] for e in read(tmp_path / "audit.jsonl")] == ["survived"]


# -- windowing ------------------------------------------------------------
def now_iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_the_window_keeps_only_recent_decisions():
    entries = [
        entry(at=now_iso(50), what="old"),
        entry(at=now_iso(1), what="recent"),
    ]
    assert [e["what"] for e in within(entries, 24)] == ["recent"]


def test_no_window_keeps_everything():
    entries = [entry(at=now_iso(500)), entry(at=now_iso(1))]
    assert len(within(entries, 0)) == 2


def test_an_unparseable_timestamp_is_dropped_from_a_window():
    assert within([entry(at="not a date")], 24) == []


# -- the command ----------------------------------------------------------
def test_it_reports_on_a_real_file(tmp_path, capsys):
    path = write_log(tmp_path / "audit.jsonl", [entry(), entry(allow=False)])
    assert main([str(path)]) == 0
    assert "2 decisions" in capsys.readouterr().out


def test_a_missing_file_is_an_error_not_a_crash(tmp_path, capsys):
    assert main([str(tmp_path / "nope.jsonl")]) == 2
    assert "No audit log" in capsys.readouterr().err
