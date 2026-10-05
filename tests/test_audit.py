"""The record of what was decided.

The thing being guarded against here is a log that is either absent when it
matters or that faithfully writes down a password.
"""

import json

import pytest

from ricardi.audit import AuditedPolicy, AuditLog
from ricardi.policy import Decision, Principal, Request, Resource, Rule, RulesPolicy


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def tool_request(tool="Bash", **tool_input):
    return Request(
        action="tool",
        principal=Principal(id="U1", email="you@example.com", known=True),
        resource=Resource(channel_id="C1"),
        agent="scout",
        tool=tool,
        tool_input=tool_input,
    )


async def test_an_allowed_decision_is_recorded_with_the_rule_that_allowed_it(tmp_path):
    path = tmp_path / "audit.jsonl"
    inner = RulesPolicy([Rule(effect="allow", name="shell is fine", tools=["Bash"])])
    policy = AuditedPolicy(inner, AuditLog(path))

    await policy.decide(tool_request(command="pytest -q"))

    (entry,) = lines(path)
    assert entry["allow"] is True
    assert entry["rule"] == "shell is fine"
    assert entry["who"] == "you@example.com"
    assert entry["agent"] == "scout"
    assert entry["tool"] == "Bash"
    assert "pytest -q" in entry["what"]
    assert entry["at"].endswith("+00:00")


async def test_a_denial_records_the_rule_and_the_reason(tmp_path):
    path = tmp_path / "audit.jsonl"
    inner = RulesPolicy([
        Rule(effect="allow", name="shell", tools=["Bash"]),
        Rule(effect="deny", name="no pushing", command_matches=["*git push*"]),
    ])
    policy = AuditedPolicy(inner, AuditLog(path))

    await policy.decide(tool_request(command="git push origin main"))

    (entry,) = lines(path)
    assert entry["allow"] is False
    assert entry["rule"] == "no pushing"
    assert entry["reason"]


async def test_a_default_denial_says_so_rather_than_naming_nothing(tmp_path):
    path = tmp_path / "audit.jsonl"
    policy = AuditedPolicy(RulesPolicy(), AuditLog(path))

    await policy.decide(tool_request(command="anything"))

    (entry,) = lines(path)
    assert entry["allow"] is False
    assert entry["rule"] == "default deny"


async def test_what_was_refused_is_recorded_even_though_slack_never_saw_it(tmp_path):
    # The channel only hears about announced commands. Everything refused is
    # invisible there, which is exactly why it has to be here.
    path = tmp_path / "audit.jsonl"
    policy = AuditedPolicy(RulesPolicy(), AuditLog(path))

    for command in ("rm -rf /", "git push", "cat /etc/shadow"):
        await policy.decide(tool_request(command=command))

    assert len(lines(path)) == 3
    assert all(e["allow"] is False for e in lines(path))


async def test_a_long_tool_input_is_truncated(tmp_path):
    path = tmp_path / "audit.jsonl"
    policy = AuditedPolicy(RulesPolicy(), AuditLog(path))

    await policy.decide(tool_request(command="echo " + "x" * 5000))

    (entry,) = lines(path)
    # A log that writes down every argument eventually writes down a password.
    assert len(entry["what"]) <= 400


async def test_only_the_summary_is_written_not_the_whole_input(tmp_path):
    path = tmp_path / "audit.jsonl"
    policy = AuditedPolicy(RulesPolicy(), AuditLog(path))

    await policy.decide(
        tool_request("Write", file_path="/tmp/x", content="hunter2-should-not-appear")
    )

    raw = path.read_text(encoding="utf-8")
    assert "hunter2-should-not-appear" not in raw
    assert "/tmp/x" in raw


async def test_a_policy_that_raises_is_recorded_and_still_raises(tmp_path):
    class Exploding:
        async def decide(self, request):
            raise RuntimeError("the decision service is down")

    path = tmp_path / "audit.jsonl"
    policy = AuditedPolicy(Exploding(), AuditLog(path))

    with pytest.raises(RuntimeError):
        await policy.decide(tool_request(command="ls"))

    (entry,) = lines(path)
    assert entry["allow"] is False
    assert entry["rule"] == "policy error"
    assert "down" in entry["error"]


async def test_an_unwritable_log_does_not_break_the_decision(tmp_path):
    # A full disk should not take the whole system down. The decision was still
    # made correctly; what is lost is the record, loudly.
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory", encoding="utf-8")
    audit = AuditLog(blocked / "nested" / "audit.jsonl")
    policy = AuditedPolicy(RulesPolicy([Rule(effect="allow")]), audit)

    decision = await policy.decide(tool_request(command="ls"))

    assert decision.allow is True
    assert audit.failures == 1


async def test_no_path_means_no_log_and_no_error(tmp_path):
    policy = AuditedPolicy(RulesPolicy([Rule(effect="allow")]), AuditLog(None))
    assert (await policy.decide(tool_request(command="ls"))).allow is True


async def test_the_wrapper_passes_the_roster_through(tmp_path):
    inner = RulesPolicy([Rule(effect="allow")], roster={"U1": "you@example.com"})
    policy = AuditedPolicy(inner, AuditLog(tmp_path / "a.jsonl"))
    assert policy.principal("U1").email == "you@example.com"
    assert policy.principal("U-nobody").known is False
    assert policy.roster == {"U1": "you@example.com"}
    assert len(policy.rules) == 1


async def test_inbound_decisions_are_recorded_too(tmp_path):
    path = tmp_path / "audit.jsonl"
    policy = AuditedPolicy(RulesPolicy(), AuditLog(path))

    await policy.decide(
        Request(
            action="ask",
            principal=Principal(id="U-stranger"),
            resource=Resource(channel_id="C1"),
            agent="scout",
        )
    )

    (entry,) = lines(path)
    assert entry["action"] == "ask"
    assert entry["who"] == "U-stranger"
    assert "tool" not in entry


# -- rotation -------------------------------------------------------------
def big(tmp_path, n):
    """A log already past the threshold."""
    path = tmp_path / "audit.jsonl"
    path.write_text("x" * n, encoding="utf-8")
    return path


async def test_a_small_log_is_not_rotated(tmp_path):
    path = big(tmp_path, 100)
    policy = AuditedPolicy(RulesPolicy(), AuditLog(path, max_bytes=1000))
    await policy.decide(tool_request(command="ls"))
    assert not (tmp_path / "audit.jsonl.1").exists()


async def test_a_full_log_is_moved_aside_and_a_fresh_one_started(tmp_path):
    path = big(tmp_path, 2000)
    policy = AuditedPolicy(RulesPolicy(), AuditLog(path, max_bytes=1000))
    await policy.decide(tool_request(command="ls"))

    assert (tmp_path / "audit.jsonl.1").exists()
    # The new file holds only the newest entry, and parses.
    assert len(lines(path)) == 1


async def test_generations_shuffle_along_and_the_oldest_falls_off(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path, max_bytes=200, keep=2)
    policy = AuditedPolicy(RulesPolicy(), audit)

    marks = []
    for i in range(6):
        marks.append("mark-" + str(i))
        await policy.decide(tool_request(command=marks[-1] + " " + "y" * 200))

    kept = sorted(p.name for p in tmp_path.iterdir())
    # Only `keep` generations survive beside the live file.
    assert kept == ["audit.jsonl", "audit.jsonl.1", "audit.jsonl.2"], kept
    # The oldest marks are gone; the newest are still readable.
    everything = "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir())
    assert "mark-5" in everything
    assert "mark-0" not in everything


async def test_keeping_nothing_just_discards_the_old_log(tmp_path):
    path = big(tmp_path, 2000)
    policy = AuditedPolicy(RulesPolicy(), AuditLog(path, max_bytes=1000, keep=0))
    await policy.decide(tool_request(command="ls"))
    assert not (tmp_path / "audit.jsonl.1").exists()
    assert len(lines(path)) == 1


async def test_rotation_off_lets_it_grow(tmp_path):
    path = big(tmp_path, 5000)
    policy = AuditedPolicy(RulesPolicy(), AuditLog(path, max_bytes=0))
    await policy.decide(tool_request(command="ls"))
    assert not (tmp_path / "audit.jsonl.1").exists()
    assert path.stat().st_size > 5000
