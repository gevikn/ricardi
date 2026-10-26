"""The decision point.

Most of these are about one thing: that the answer is no unless something
says yes, in every direction it could go wrong.
"""

import pytest

from ricardi import policy as P


def rule(**kw):
    kw.setdefault("effect", "allow")
    return P.Rule(**kw)


def ask(email="you@example.com", channel="C1", agent="scout", known=True):
    return P.Request(
        action="ask",
        principal=P.Principal(id="U1", email=email, known=known),
        resource=P.Resource(channel_id=channel),
        agent=agent,
    )


def tool(name="Bash", **tool_input):
    return P.Request(
        action="tool",
        principal=P.Principal(id="U1", email="you@example.com", known=True),
        agent="scout",
        tool=name,
        tool_input=tool_input,
    )


# -- the default ----------------------------------------------------------
async def test_nothing_is_allowed_by_an_empty_policy():
    assert (await P.RulesPolicy().decide(ask())).allow is False


async def test_a_request_no_rule_mentions_is_denied():
    pol = P.RulesPolicy([rule(agents=["deployer"])])
    assert (await pol.decide(ask(agent="scout"))).allow is False


async def test_a_closed_policy_denies_and_says_why():
    decision = await P.ClosedPolicy("the file is broken").decide(ask())
    assert decision.allow is False
    assert "broken" in decision.reason


# -- combining ------------------------------------------------------------
async def test_deny_beats_allow_whatever_the_order():
    allow = rule(tools=["Bash"])
    deny = P.Rule(effect="deny", tools=["Bash"], command_matches=["*push*"])
    for rules in ([allow, deny], [deny, allow]):
        pol = P.RulesPolicy(rules)
        assert (await pol.decide(tool(command="git push origin main"))).allow is False
        assert (await pol.decide(tool(command="git status"))).allow is True


async def test_obligations_from_every_matching_allow_are_collected():
    pol = P.RulesPolicy([
        rule(tools=["Bash"], obligations=["announce"]),
        rule(agents=["scout"], obligations=["log", "announce"]),
    ])
    decision = await pol.decide(tool(command="pytest"))
    assert decision.allow is True
    assert set(decision.obligations) == {"announce", "log"}


# -- matching dimensions --------------------------------------------------
async def test_an_unnamed_dimension_matches_anything():
    pol = P.RulesPolicy([rule(actions=["ask"])])
    assert (await pol.decide(ask(agent="anything", channel="C9"))).allow is True


async def test_every_named_dimension_must_match():
    pol = P.RulesPolicy([rule(actions=["ask"], agents=["scout"], channels=["C1"])])
    assert (await pol.decide(ask(agent="scout", channel="C1"))).allow is True
    assert (await pol.decide(ask(agent="scout", channel="C2"))).allow is False
    assert (await pol.decide(ask(agent="other", channel="C1"))).allow is False


async def test_users_match_as_globs_and_ignore_case():
    pol = P.RulesPolicy([rule(actions=["ask"], users=["*@example.com"])])
    assert (await pol.decide(ask(email="You@Example.com"))).allow is True
    assert (await pol.decide(ask(email="someone@elsewhere.com"))).allow is False


async def test_someone_with_no_email_matches_no_user_rule():
    # Not on the roster means no email, which must not match "*".
    pol = P.RulesPolicy([rule(actions=["ask"], users=["*"])])
    assert (await pol.decide(ask(email="", known=False))).allow is False


# -- paths ----------------------------------------------------------------
async def test_a_write_inside_the_allowed_root_is_permitted(tmp_path):
    root = tmp_path / "scratch"
    root.mkdir()
    pol = P.RulesPolicy([rule(tools=["Write"], path_under=str(root))])
    inside = str(root / "note.txt")
    assert (await pol.decide(tool("Write", file_path=inside))).allow is True


async def test_a_write_outside_the_allowed_root_is_denied(tmp_path):
    root = tmp_path / "scratch"
    root.mkdir()
    pol = P.RulesPolicy([rule(tools=["Write"], path_under=str(root))])
    outside = str(tmp_path / "elsewhere.txt")
    assert (await pol.decide(tool("Write", file_path=outside))).allow is False


async def test_dot_dot_cannot_climb_out_of_the_allowed_root(tmp_path):
    root = tmp_path / "scratch"
    root.mkdir()
    pol = P.RulesPolicy([rule(tools=["Write"], path_under=str(root))])
    # Textually under scratch, actually the parent. The resolve is the point.
    escape = str(root / ".." / "secrets.txt")
    assert (await pol.decide(tool("Write", file_path=escape))).allow is False


async def test_a_path_rule_does_not_match_a_call_with_no_path(tmp_path):
    # An unsatisfiable condition must not grant anything.
    pol = P.RulesPolicy([rule(tools=["Bash"], path_under=str(tmp_path))])
    assert (await pol.decide(tool("Bash", command="ls"))).allow is False


async def test_path_matches_catches_a_credential_file_whatever_the_tool():
    pol = P.RulesPolicy([
        rule(tools=["Read"]),
        P.Rule(effect="deny", path_matches=["*/.env", "*id_rsa*"]),
    ])
    assert (await pol.decide(tool("Read", file_path="/home/me/app/.env"))).allow is False
    assert (await pol.decide(tool("Read", file_path="/home/me/.ssh/id_rsa"))).allow is False
    assert (await pol.decide(tool("Read", file_path="/home/me/app/main.py"))).allow is True


async def test_path_matches_handles_windows_separators():
    pol = P.RulesPolicy([rule(tools=["Read"]), P.Rule(effect="deny", path_matches=["*/.env"])])
    request = tool("Read", file_path=r"C:\Users\me\app\.env")
    assert (await pol.decide(request)).allow is False


# -- commands -------------------------------------------------------------
async def test_commands_match_as_globs_after_whitespace_is_flattened():
    pol = P.RulesPolicy([rule(tools=["Bash"], command_matches=["pytest*"])])
    assert (await pol.decide(tool(command="pytest   -q    tests/"))).allow is True
    assert (await pol.decide(tool(command="rm -rf /"))).allow is False


async def test_a_command_rule_does_not_match_a_tool_without_one():
    pol = P.RulesPolicy([rule(command_matches=["*"])])
    assert (await pol.decide(tool("Read", file_path="/tmp/x"))).allow is False


# -- loading --------------------------------------------------------------
def write(tmp_path, body):
    p = tmp_path / "policy.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_a_policy_loads_with_its_roster(tmp_path):
    path = write(tmp_path, """
[[identity]]
slack = "U1"
email = "you@example.com"

[[rule]]
name = "ask"
effect = "allow"
actions = ["ask"]
""")
    pol = P.load(path)
    assert pol.roster == {"U1": "you@example.com"}
    assert pol.principal("U1").email == "you@example.com"
    assert pol.principal("U1").known is True
    assert pol.principal("U-unknown").known is False


def test_a_missing_policy_says_so(tmp_path):
    with pytest.raises(P.PolicyError, match="No policy"):
        P.load(tmp_path / "nope.toml")


def test_a_policy_with_no_rules_is_refused(tmp_path):
    # It would deny everything, which is almost certainly not what was meant.
    path = write(tmp_path, '[[identity]]\nslack = "U1"\nemail = "a@b.c"\n')
    with pytest.raises(P.PolicyError, match="no rules"):
        P.load(path)


def test_a_rule_with_a_nonsense_effect_is_refused(tmp_path):
    path = write(tmp_path, '[[rule]]\nname = "x"\neffect = "maybe"\n')
    with pytest.raises(P.PolicyError, match="allow or deny"):
        P.load(path)


def test_an_identity_missing_half_of_itself_is_refused(tmp_path):
    path = write(tmp_path, '[[identity]]\nslack = "U1"\n\n[[rule]]\neffect = "allow"\n')
    with pytest.raises(P.PolicyError, match="slack and email"):
        P.load(path)


def test_a_duplicate_identity_is_refused(tmp_path):
    path = write(tmp_path, """
[[identity]]
slack = "U1"
email = "a@b.c"

[[identity]]
slack = "U1"
email = "c@d.e"

[[rule]]
effect = "allow"
""")
    with pytest.raises(P.PolicyError, match="twice"):
        P.load(path)


def test_the_shipped_example_loads_and_denies_the_obvious_things():
    """The example is what people copy, so it has to be right."""
    pol = P.load("policy.example.toml")
    assert pol.roster

    import asyncio

    async def decide(req):
        return await pol.decide(req)

    me = P.Principal(id="U00000000000", email="you@example.com", known=True)
    here = P.Resource(channel_id="C00000000000")

    def t(name, **ti):
        return P.Request(action="tool", principal=me, resource=here, agent="shared",
                         tool=name, tool_input=ti)

    assert asyncio.run(
        decide(t("Read", file_path="~/repos/shared/main.py"))
    ).allow is True
    assert asyncio.run(decide(t("Bash", command="pytest -q"))).allow is True
    assert asyncio.run(decide(t("Bash", command="git push origin main"))).allow is False
    assert asyncio.run(decide(t("Bash", command="rm -rf /"))).allow is False
    assert asyncio.run(decide(t("Read", file_path="/repo/.env"))).allow is False
    # Reading is bounded by path, not merely by the deny list below it: a file
    # nobody thought to name is still outside the worktree, so still refused.
    assert asyncio.run(decide(t("Read", file_path="/repo/main.py"))).allow is False
    # An announced command carries the obligation that makes it visible.
    assert "announce" in asyncio.run(decide(t("Bash", command="pytest -q"))).obligations

    # The rules that grant change say who and where. A rule that forgot either
    # would grant far more than it reads as granting, so both are asserted.
    def elsewhere(name, **ti):
        return P.Request(action="tool", principal=me, resource=here, agent="other",
                         tool=name, tool_input=ti)

    def stranger(name, **ti):
        return P.Request(
            action="tool",
            principal=P.Principal(id="U99999999999", email="nobody@example.com"),
            resource=here, agent="shared", tool=name, tool_input=ti,
        )

    assert asyncio.run(decide(elsewhere("Bash", command="pytest -q"))).allow is False
    assert asyncio.run(decide(elsewhere("Write", file_path="/repo/x"))).allow is False
    assert asyncio.run(decide(stranger("Bash", command="pytest -q"))).allow is False
    assert asyncio.run(decide(stranger("Write", file_path="/repo/x"))).allow is False
    # Reading is bounded by path, not by who asks. Another agent reading
    # inside a worktree is fine -- reading changes nothing, and scoping it per
    # agent would buy nothing. Reading *outside* every worktree is refused, so
    # a file nobody thought to deny is still out of reach.
    assert asyncio.run(
        decide(elsewhere("Read", file_path="~/repos/shared/main.py"))
    ).allow is True
    assert asyncio.run(decide(elsewhere("Read", file_path="/repo/main.py"))).allow is False


def test_the_example_lets_the_listed_people_ask_in_the_listed_channel():
    import asyncio

    pol = P.load("policy.example.toml")
    req = P.Request(
        action="ask",
        principal=P.Principal(id="U00000000000", email="you@example.com", known=True),
        resource=P.Resource(channel_id="C00000000000"),
        agent="scout",
    )
    assert asyncio.run(pol.decide(req)).allow is True

    stranger = P.Request(
        action="ask",
        principal=P.Principal(id="U-nope"),
        resource=P.Resource(channel_id="C00000000000"),
        agent="scout",
    )
    assert asyncio.run(pol.decide(stranger)).allow is False


# -- the shell is not called the same thing everywhere --------------------
async def test_a_deny_without_a_tool_name_catches_every_shell():
    """Regression: the shell tool is Bash on unix and PowerShell on Windows.

    A deny scoped to one of them silently covers nothing on the other, and a
    missed deny is worse than a missed allow.
    """
    pol = P.RulesPolicy([
        rule(tools=["Bash", "PowerShell"]),
        P.Rule(effect="deny", command_matches=["*git push*"]),
    ])
    for shell in ("Bash", "PowerShell"):
        assert (await pol.decide(tool(shell, command="git push origin main"))).allow is False
        assert (await pol.decide(tool(shell, command="pytest -q"))).allow is True


async def test_an_allow_naming_one_shell_does_not_cover_the_other():
    # Documents the trap rather than hiding it: naming only Bash means a
    # Windows agent gets nothing, which is safe but surprising.
    pol = P.RulesPolicy([rule(tools=["Bash"])])
    assert (await pol.decide(tool("Bash", command="ls"))).allow is True
    assert (await pol.decide(tool("PowerShell", command="ls"))).allow is False


def test_the_shipped_example_permits_and_forbids_the_same_things_on_both_shells():
    import asyncio

    pol = P.load("policy.example.toml")
    me = P.Principal(id="U00000000000", email="you@example.com", known=True)

    def t(name, command):
        return P.Request(
            action="tool", principal=me, resource=P.Resource(channel_id="C00000000000"),
            agent="shared", tool=name, tool_input={"command": command},
        )

    for shell in ("Bash", "PowerShell"):
        assert asyncio.run(pol.decide(t(shell, "pytest -q"))).allow is True, shell
        assert asyncio.run(pol.decide(t(shell, "git push origin main"))).allow is False, shell
        assert asyncio.run(pol.decide(t(shell, "rm -rf /"))).allow is False, shell
        assert asyncio.run(pol.decide(t(shell, "cat .env"))).allow is False, shell


# -- the platform-neutral shell alias --------------------------------------
async def test_the_shell_alias_covers_every_platforms_shell():
    """Writing `tools = ["shell"]` is what people mean and what they forget."""
    pol = P.RulesPolicy([rule(tools=["shell"])])
    for name in P.SHELL_TOOLS:
        assert (await pol.decide(tool(name, command="ls"))).allow is True, name
    assert (await pol.decide(tool("Read", file_path="/x"))).allow is False


async def test_the_alias_is_case_insensitive_like_every_other_pattern():
    pol = P.RulesPolicy([rule(tools=["SHELL"])])
    assert (await pol.decide(tool("Bash", command="ls"))).allow is True


async def test_the_alias_mixes_with_ordinary_tool_names():
    pol = P.RulesPolicy([rule(tools=["shell", "Read"])])
    assert (await pol.decide(tool("PowerShell", command="ls"))).allow is True
    assert (await pol.decide(tool("Read", file_path="/x"))).allow is True
    assert (await pol.decide(tool("Write", file_path="/x"))).allow is False


async def test_a_deny_can_use_the_alias_too():
    pol = P.RulesPolicy([
        rule(tools=["shell", "Read"]),
        P.Rule(effect="deny", tools=["shell"], command_matches=["*push*"]),
    ])
    for name in P.SHELL_TOOLS:
        assert (await pol.decide(tool(name, command="git push"))).allow is False, name


def test_the_shipped_example_uses_the_alias_not_a_platform_name():
    # The example is what people copy; a copied rule naming one shell is the
    # trap this alias exists to close.
    import io

    body = io.open("policy.example.toml", encoding="utf-8").read()
    assert 'tools           = ["shell"]' in body
    assert '"PowerShell"' not in body


# The credential paths that this example used to hand out. Each one was
# reachable because "read anything in the repo" named no path_under, and the
# deny list missed them: `*id_rsa*` does not match `id_ed25519`, and
# `*/credentials*` does not match `/.credentials.json`. Both layers are tested
# here, because either one alone would be a single point of failure.
EXPOSED_BEFORE = [
    "~/.claude/.credentials.json",
    "~/.ssh/id_ed25519",
    "~/.ssh/id_rsa",
    "~/.aws/credentials",
    "~/.gh/hosts.yml",
    "~/.netrc",
    "/etc/shadow",
    "~/repos/shared/deploy.pem",
]


@pytest.mark.parametrize("path", EXPOSED_BEFORE)
@pytest.mark.parametrize("policy_file", ["policy.example.toml", "docker/policy.example.toml"])
def test_neither_example_hands_out_a_credential_file(policy_file, path):
    import asyncio

    pol = P.load(policy_file)
    req = P.Request(
        action="tool",
        principal=P.Principal(id="U11111111111", email="them@example.com", known=True),
        resource=P.Resource(channel_id="C00000000000"),
        agent="shared",
        tool="Read",
        tool_input={"file_path": path},
    )
    assert asyncio.run(pol.decide(req)).allow is False, path


@pytest.mark.parametrize("policy_file", ["policy.example.toml", "docker/policy.example.toml"])
def test_neither_example_lets_a_search_run_from_the_root(policy_file):
    """`Grep` with no path cannot satisfy path_under, and must fail closed."""
    import asyncio

    pol = P.load(policy_file)

    def grep(**ti):
        return P.Request(
            action="tool",
            principal=P.Principal(id="U11111111111", email="them@example.com", known=True),
            resource=P.Resource(channel_id="C00000000000"),
            agent="scout",
            tool="Grep",
            tool_input=ti,
        )

    assert asyncio.run(pol.decide(grep(pattern="sk-ant", path="/"))).allow is False
    assert asyncio.run(pol.decide(grep(pattern="sk-ant"))).allow is False
