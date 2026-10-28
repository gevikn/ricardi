"""Two holes the access simulation found, and the fix for each.

H1 -- an allow rule that names a channel but no users matched *anyone* in that
channel, including someone Ricardi has never heard of. Unnamed dimensions match
all, so a rule that forgot `users` granted far more than it read as granting.

H2 -- a tool rule's `path_under` is written per rule, so one wide read rule let
every agent read every other agent's tree. Harmless when the agents were one
person's own repositories; not harmless once a name belongs to someone else.

Both are about *scoping*, which is why they share a file.
"""

from __future__ import annotations

import asyncio

import pytest

from ricardi import config, policy as P


def decide(pol, request):
    return asyncio.run(pol.decide(request))


# --------------------------------------------------------------- H1


def channel_rule():
    """An allow that names where but not who -- the shape that failed open."""
    return P.Rule(effect="allow", name="ask-here", actions=("ask",), channels=("C1",))


def asker(known: bool, email: str = ""):
    return P.Request(
        action="ask",
        principal=P.Principal(id="U1", email=email, known=known),
        resource=P.Resource(channel_id="C1"),
        agent="n1",
    )


def test_an_unknown_asker_is_refused_by_a_rule_that_names_no_users():
    pol = P.RulesPolicy([channel_rule()])
    assert decide(pol, asker(known=False)).allow is False


def test_the_refusal_says_it_is_about_not_knowing_them():
    pol = P.RulesPolicy([channel_rule()])
    reason = decide(pol, asker(known=False)).reason
    assert "don't know you" in reason


def test_a_known_asker_still_passes_the_same_rule():
    pol = P.RulesPolicy([channel_rule()])
    assert decide(pol, asker(known=True, email="you@example.com")).allow is True


def test_an_unknown_asker_is_refused_even_for_a_tool():
    """The hole was not specific to `ask`. Every action closes."""
    pol = P.RulesPolicy(
        [P.Rule(effect="allow", name="read-all", actions=("tool",), tools=("Read",))]
    )
    request = P.Request(
        action="tool",
        principal=P.Principal(id="U1"),
        agent="n1",
        tool="Read",
        tool_input={"file_path": "/workspace/n1/app.py"},
    )
    assert decide(pol, request).allow is False


def test_a_deny_still_names_itself_for_an_unknown_asker():
    """H1 must not swallow a matching deny.

    A stranger tripping a credential deny should be recorded as that rule, not
    as the default. Same outcome, different audit line, and the audit line is
    the point.
    """
    pol = P.RulesPolicy(
        [
            P.Rule(effect="allow", name="read-all", actions=("tool",), tools=("Read",)),
            P.Rule(
                effect="deny",
                name="no-credentials",
                actions=("tool",),
                path_matches=("*.pem",),
            ),
        ]
    )
    request = P.Request(
        action="tool",
        principal=P.Principal(id="U-stranger"),
        agent="n1",
        tool="Read",
        tool_input={"file_path": "/workspace/n1/key.pem"},
    )
    decision = decide(pol, request)
    assert decision.allow is False
    assert decision.rule == "no-credentials"


# --------------------------------------------------------------- H2


def wide_read():
    """One read rule over everything -- what a careful person writes first."""
    return P.Rule(
        effect="allow",
        name="read-workspace",
        actions=("tool",),
        tools=("Read", "Grep", "Glob"),
        path_under="/workspace",
    )


def read_as(agent: str, path: str, roots: tuple[str, ...] = ()):
    return P.Request(
        action="tool",
        principal=P.Principal(id="U1", email="you@example.com", known=True),
        agent=agent,
        agent_roots=roots,
        tool="Read",
        tool_input={"file_path": path},
    )


def test_a_name_cannot_read_outside_its_own_roots():
    pol = P.RulesPolicy([wide_read()])
    request = read_as("n1", "/workspace/project2/secrets.py", roots=("/workspace/project1",))
    assert decide(pol, request).allow is False


def test_a_name_can_read_inside_its_own_roots():
    pol = P.RulesPolicy([wide_read()])
    request = read_as("n1", "/workspace/project1/app.py", roots=("/workspace/project1",))
    assert decide(pol, request).allow is True


def test_every_declared_root_is_honoured():
    """The point of a list: one name, several checkouts or worktrees."""
    pol = P.RulesPolicy([wide_read()])
    roots = ("/workspace/project1", "/workspace/project1-docs")
    assert decide(pol, read_as("n1", "/workspace/project1/app.py", roots)).allow is True
    assert decide(pol, read_as("n1", "/workspace/project1-docs/x.md", roots)).allow is True
    assert decide(pol, read_as("n1", "/workspace/project2/x.py", roots)).allow is False


def test_roots_narrow_and_never_widen():
    """A root outside what the rules allow grants nothing.

    The roots are an intersection, not an alternative source of permission --
    otherwise a name's own config could reach past the reviewed file.
    """
    pol = P.RulesPolicy([wide_read()])
    request = read_as("n1", "/etc/shadow", roots=("/etc",))
    assert decide(pol, request).allow is False


def test_a_name_declaring_no_roots_is_left_as_it_was():
    """The compatibility seam, and the reason it is safe.

    A request carrying no roots is unconstrained by this check, which is what
    keeps `test_policy.py`'s cross-agent read meaningful. It is safe because
    `config.load` guarantees every agent has at least one root and the gate
    passes them -- see the two tests below.
    """
    pol = P.RulesPolicy([wide_read()])
    assert decide(pol, read_as("other", "/workspace/project2/x.py")).allow is True


# --------------------------------------------------------------- the wiring


def test_roots_default_to_the_working_directory(tmp_path):
    cwd = tmp_path / "project1"
    cwd.mkdir()
    (tmp_path / "agents.toml").write_text(
        f'[[agent]]\nname = "n1"\ncwd = "{cwd.as_posix()}"\n', encoding="utf-8"
    )
    settings = config.load(tmp_path / "agents.toml")
    assert settings.agents[0].roots == (str(cwd),)


def test_declared_roots_are_loaded(tmp_path):
    cwd = tmp_path / "project1"
    cwd.mkdir()
    docs = tmp_path / "project1-docs"
    docs.mkdir()
    (tmp_path / "agents.toml").write_text(
        f'[[agent]]\nname = "n1"\ncwd = "{cwd.as_posix()}"\n'
        f'roots = ["{cwd.as_posix()}", "{docs.as_posix()}"]\n',
        encoding="utf-8",
    )
    settings = config.load(tmp_path / "agents.toml")
    assert settings.agents[0].roots == (str(cwd), str(docs))


def test_a_root_that_is_not_a_directory_is_refused(tmp_path):
    """Same guard as `cwd`. A root that does not exist bounds nothing."""
    cwd = tmp_path / "project1"
    cwd.mkdir()
    (tmp_path / "agents.toml").write_text(
        f'[[agent]]\nname = "n1"\ncwd = "{cwd.as_posix()}"\n'
        f'roots = ["{(tmp_path / "nope").as_posix()}"]\n',
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError, match="not a directory"):
        config.load(tmp_path / "agents.toml")
