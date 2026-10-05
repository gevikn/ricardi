"""What command matching catches, and what it does not.

These tests assert the *limits* of `command_matches` as much as its behaviour.
The ones under "gets through" are not bugs waiting to be fixed by tightening a
pattern -- they are the reason `SECURITY.md` says the boundary is the worktree
and the missing push credentials, and that policy is the second line.

If you improve matching and one of these starts failing, that is good news:
update it and move the case up to the other section. What must not happen is
these cases quietly disappearing, leaving a reader to assume completeness that
was never there.
"""

import pytest

from ricardi.policy import Decision, Principal, Request, Resource, Rule, RulesPolicy

# A policy shaped like the shipped example: a narrow allow, a blunt deny.
GUARDED = RulesPolicy([
    Rule(effect="allow", name="shell", tools=["Bash", "PowerShell"]),
    Rule(
        effect="deny",
        name="never",
        command_matches=["*git push*", "*rm -rf*", "*curl*"],
    ),
])


async def allowed(command: str) -> bool:
    request = Request(
        action="tool",
        principal=Principal(id="U1", email="you@example.com", known=True),
        resource=Resource(channel_id="C1"),
        agent="bench",
        tool="Bash",
        tool_input={"command": command},
    )
    return (await GUARDED.decide(request)).allow


# -- caught -----------------------------------------------------------------
@pytest.mark.parametrize("command", [
    "git push origin main",
    "git push",
    # Whitespace is flattened before matching, so padding does not help.
    "git    push   origin   main",
    "git\tpush origin main",
    "git\npush",
    # Matching is case-insensitive.
    "GIT PUSH ORIGIN MAIN",
    "Git Push",
    # A substring match catches chaining and sequencing.
    "ls -la; git push origin main",
    "make build && git push",
    "true || git push",
    # And command substitution, when the text still contains the string.
    "$(echo git push)",
    # Caught by accident, not by design: the path ends ".git" and is followed
    # by " push", so the literal "git push" appears. Change the directory name
    # and it would sail through. Matching text means coincidences count as
    # security, which is the whole problem with it.
    "git --git-dir=/repo/.git push",
    "rm -rf /tmp/x",
    "curl https://example.com",
])
async def test_these_are_caught(command):
    assert await allowed(command) is False, command


# -- gets through -----------------------------------------------------------
@pytest.mark.parametrize("command,why", [
    ("git -C /repo push origin main",
     "the string 'git push' never appears: an option sits between the words"),
    ('g"i"t push',
     "the shell strips the quotes; the pattern sees them"),
    ("gi\\t push",
     "a backslash the shell removes and the pattern does not"),
    ("echo Z2l0IHB1c2g9 | base64 -d | sh",
     "the command is data until the shell decodes it"),
    ("C=push; git $C",
     "the dangerous word is assembled from a variable at run time"),
    ("./deploy.sh",
     "the command is in a file; nothing in the text says what it does"),
    ("python -c \"import subprocess; subprocess.run(['git','push'])\"",
     "a different language runs it, and argv never spells the phrase"),
    ("alias p=push; git p",
     "an alias defined in the same line"),
    ("rm -r -f /tmp/x",
     "'rm -rf' is a literal pattern and the flags are separable"),
])
async def test_these_get_through_and_that_is_documented(command, why):
    """Each of these defeats the deny list. The worktree is what stops them."""
    assert await allowed(command) is True, f"{command} -- {why}"


# -- the narrow-allow direction is safer ------------------------------------
async def test_an_allow_list_only_has_to_permit_so_it_fails_closed():
    """The same evasions against a narrow allow simply do not match."""
    narrow = RulesPolicy([
        Rule(effect="allow", tools=["Bash"], command_matches=["pytest*", "ls *"]),
    ])

    async def ok(command):
        request = Request(
            action="tool",
            principal=Principal(id="U1", email="a@b.c", known=True),
            agent="bench",
            tool="Bash",
            tool_input={"command": command},
        )
        return (await narrow.decide(request)).allow

    assert await ok("pytest -q") is True
    # Every case from the section above is denied here, because nothing
    # permits it -- no anticipation required.
    for sneaky in (
        "git -C /repo push",
        'g"i"t push',
        "echo Z2l0IHB1c2g9 | base64 -d | sh",
        "C=push; git $C",
        "./deploy.sh",
    ):
        assert await ok(sneaky) is False, sneaky


async def test_a_narrow_allow_is_also_narrower_than_people_expect():
    """Which is why the shipped example lists several spellings."""
    narrow = RulesPolicy([
        Rule(effect="allow", tools=["Bash"], command_matches=["pytest*"]),
    ])

    async def ok(command):
        request = Request(
            action="tool",
            principal=Principal(id="U1", email="a@b.c", known=True),
            agent="bench", tool="Bash", tool_input={"command": command},
        )
        return (await narrow.decide(request)).allow

    assert await ok("pytest -q") is True
    # Same intent, three spellings the pattern does not cover.
    assert await ok("python -m pytest") is False
    assert await ok("uv run pytest") is False
    assert await ok("cd tests && pytest") is False


# -- the structural boundary is what actually holds -------------------------
async def test_a_path_rule_is_not_fooled_by_the_same_tricks():
    """Paths are resolved, not matched as text, so they do not have this problem."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bench"
        root.mkdir()
        policy = RulesPolicy([
            Rule(effect="allow", tools=["Write"], path_under=str(root)),
        ])

        async def ok(path):
            request = Request(
                action="tool",
                principal=Principal(id="U1", email="a@b.c", known=True),
                agent="bench", tool="Write", tool_input={"file_path": str(path)},
            )
            return (await policy.decide(request)).allow

        assert await ok(root / "note.txt") is True
        # Textual tricks that work on commands do not work here: the path is
        # resolved before it is compared.
        assert await ok(root / ".." / "escape.txt") is False
        assert await ok(root / "a" / ".." / ".." / "escape.txt") is False
        assert await ok(Path(tmp) / "elsewhere.txt") is False


# -- the same verdict on every platform -----------------------------------
# Regression: fnmatch.fnmatch normalises case on Windows and not on POSIX, so
# a deny rule written once behaved differently on two identical deployments.
# These pin the behaviour to be identical everywhere, which is the only way a
# policy reviewed on one machine means anything on the other.
@pytest.mark.parametrize("command", [
    "GIT PUSH ORIGIN MAIN",
    "Git Push",
    "gIt PuSh",
    "RM -RF /tmp/x",
    "CURL https://example.com",
])
async def test_shouting_does_not_evade_a_deny(command):
    assert await allowed(command) is False, command


@pytest.mark.parametrize("command", [
    "PYTEST -q",
    "PyTest tests/",
])
async def test_an_allow_is_equally_case_blind(command):
    narrow = RulesPolicy([
        Rule(effect="allow", tools=["Bash"], command_matches=["pytest*"]),
    ])
    request = Request(
        action="tool",
        principal=Principal(id="U1", email="a@b.c", known=True),
        agent="bench", tool="Bash", tool_input={"command": command},
    )
    assert (await narrow.decide(request)).allow is True, command


async def test_a_path_deny_ignores_case_too():
    pol = RulesPolicy([
        Rule(effect="allow", tools=["Read"]),
        Rule(effect="deny", path_matches=["*/.env"]),
    ])

    async def ok(path):
        request = Request(
            action="tool",
            principal=Principal(id="U1", email="a@b.c", known=True),
            agent="bench", tool="Read", tool_input={"file_path": path},
        )
        return (await pol.decide(request)).allow

    assert await ok("/repo/.ENV") is False
    assert await ok(r"C:\Users\me\.Env") is False
