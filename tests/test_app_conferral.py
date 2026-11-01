"""grant and revoke from Slack: parse, ask `decide`, then write.

Every decision here goes through the real RulesPolicy behind an AuditedPolicy,
so a test that passes has been through the escalation check and left an audit
line, not through a stub that says yes.
"""

import asyncio
import contextlib
import json
import logging

import pytest

from ricardi import cli, readiness
from ricardi.app import Ricardi
from ricardi.audit import AuditedPolicy, AuditLog
from ricardi.backends.base import Agent
from ricardi.backends.fake import FakeBackend
from ricardi.grants import GrantError, GrantStore
from ricardi.policy import Rule, RulesPolicy
from ricardi.store import ThreadBindings
from ricardi.transports.base import Inbound

ROSTER = {
    "U1": "owner@example.com",
    "U2": "bob@example.com",
    "U3": "carol@example.com",
    "U4": "dave@example.com",
}
RULES = [
    Rule(effect="allow", name="everyone", actions=("ask", "grant", "revoke")),
]


class Recorder:
    def __init__(self):
        self.sent = []

    async def start(self, on_message):
        self.on_message = on_message

    async def send(self, channel, thread, text):
        self.sent.append((channel, thread, text))

    async def close(self):
        return None


@pytest.fixture
def world(tmp_path):
    store = GrantStore.open(tmp_path / "g.db")
    inner = RulesPolicy(
        rules=list(RULES), roster=ROSTER, grants=store,
        owners={"n1": ("owner@example.com",)},
    )
    audit_path = tmp_path / "audit.jsonl"
    policy = AuditedPolicy(inner, AuditLog(audit_path))
    transport = Recorder()
    backend = FakeBackend([Agent(name="n1", description="", cwd=str(tmp_path))])
    app = Ricardi(
        transport=transport, backend=backend, ack_after=30.0, policy=policy,
        grants=store, bindings=ThreadBindings(tmp_path / "threads.json"),
    )
    yield app, store, transport, audit_path, tmp_path
    store.close()


def inbound(text, user="U1", thread="t1"):
    return Inbound(text=text, thread=thread, channel="c1", user=user)


def said(transport):
    return transport.sent[-1][2] if transport.sent else ""


async def test_grant_writes_a_row_and_confirms_in_the_channel(world):
    app, store, transport, *_ = world
    await app.handle(inbound("grant <@U2> read on n1", user="U1"))
    assert store.tier("bob@example.com", "n1") == "read"
    assert "read" in said(transport) and "n1" in said(transport)


async def test_the_tier_is_read_however_it_was_typed(world):
    app, store, *_ = world
    await app.handle(inbound("grant <@U2>  Read  on n1", user="U1"))
    assert store.tier("bob@example.com", "n1") == "read"


async def test_a_refused_grant_writes_nothing(world):
    app, store, transport, *_ = world
    await app.handle(inbound("grant <@U2> write on n1", user="U3"))
    assert store.history() == ()
    assert said(transport)


async def test_a_non_tier_is_refused_by_the_check_not_the_parser(world):
    app, store, transport, audit, _ = world
    await app.handle(inbound("grant <@U2> admin on n1", user="U1"))
    assert store.history() == ()
    last = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
    assert last["rule"] == "not a tier"


async def test_revoke_supersedes_rather_than_deleting(world):
    app, store, *_ = world
    await app.handle(inbound("grant <@U2> read on n1", user="U1"))
    await app.handle(inbound("revoke <@U2> on n1", user="U1"))
    assert len(store.history()) == 2
    assert store.tier("bob@example.com", "n1") == ""


async def test_revoke_clears_every_tier_held_and_leaves_ask_alone(world):
    app, store, *_ = world
    store.add(person="bob@example.com", name="n1", verb="ask", by="t")
    await app.handle(inbound("grant <@U2> read on n1", user="U1"))
    await app.handle(inbound("grant <@U2> write on n1", user="U1"))
    await app.handle(inbound("revoke <@U2> on n1", user="U1"))
    assert store.tier("bob@example.com", "n1") == ""
    assert store.holds("bob@example.com", "n1", "ask") is True


async def test_revoking_what_is_not_held_says_so_and_writes_nothing(world):
    app, store, transport, *_ = world
    await app.handle(inbound("revoke <@U2> on n1", user="U1"))
    assert store.history() == ()
    assert "no tier" in said(transport).lower()


async def test_a_delegate_cannot_give_more_than_they_hold(world):
    app, store, *_ = world
    store.add(person="carol@example.com", name="n1", verb="grant", by="t")
    store.add(person="carol@example.com", name="n1", verb="read", by="t")
    await app.handle(inbound("grant <@U2> write on n1", user="U3"))
    assert store.tier("bob@example.com", "n1") == ""
    await app.handle(inbound("grant <@U2> read on n1", user="U3"))
    assert store.tier("bob@example.com", "n1") == "read"


async def test_an_unparseable_grant_explains_the_shape(world):
    app, store, transport, *_ = world
    await app.handle(inbound("grant bob everything", user="U1"))
    assert "grant @person" in said(transport)
    assert store.history() == ()


def test_the_router_still_parses_a_grant():
    from ricardi import router

    c = router.parse("grant <@U2> read on n1")
    assert (c.kind, c.user, c.tier, c.agent) == ("grant", "U2", "read", "n1")


@pytest.mark.parametrize("text", [
    "grant-bot: hi", "revoke-bot: hi", "tell grant-bot hi",
])
def test_an_agent_named_after_the_verb_is_still_addressable(text):
    from ricardi import router

    c = router.parse(text)
    assert c.kind == "tell" and c.agent in {"grant-bot", "revoke-bot"}


@pytest.mark.parametrize("text", ["grant-bot hello", "revoke-bot hello"])
def test_a_verb_with_a_hyphen_is_not_a_command(text):
    from ricardi import router

    assert router.parse(text).kind == "say"


def test_flags_survive_a_malformed_grant():
    from ricardi import router

    assert router.parse("-ro grant bob").flags == ("ro",)


async def test_an_unparseable_revoke_explains_the_shape(world):
    app, store, transport, *_ = world
    await app.handle(inbound("revoke bob", user="U1"))
    assert "revoke @person" in said(transport)


async def test_a_slack_id_that_is_not_on_the_roster_is_named_as_the_problem(world):
    app, store, transport, *_ = world
    await app.handle(inbound("grant <@U9> read on n1", user="U1"))
    assert "roster" in said(transport) and store.history() == ()


async def test_the_decision_is_audited_whether_or_not_it_allowed(world):
    app, store, transport, audit, _ = world
    await app.handle(inbound("grant <@U2> read on n1", user="U1"))
    await app.handle(inbound("grant <@U2> read on n1", user="U3"))
    entries = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines()]
    grants = [e for e in entries if e["action"] == "grant"]
    assert [e["allow"] for e in grants] == [True, False]


async def test_a_store_error_refuses_rather_than_claiming_success(world, monkeypatch):
    app, store, transport, *_ = world

    def boom(**_):
        raise GrantError("disk full")

    monkeypatch.setattr(store, "add", boom)
    await app.handle(inbound("grant <@U2> read on n1", user="U1"))
    assert "couldn't" in said(transport).lower()
    assert "now holds" not in said(transport).lower()


async def test_a_store_error_on_revoke_is_not_success_either(world, monkeypatch):
    app, store, transport, *_ = world
    await app.handle(inbound("grant <@U2> read on n1", user="U1"))

    def boom(**_):
        raise GrantError("disk full")

    monkeypatch.setattr(store, "add", boom)
    await app.handle(inbound("revoke <@U2> on n1", user="U1"))
    assert "couldn't" in said(transport).lower()
    assert store.tier("bob@example.com", "n1") == "read"


async def test_a_name_nobody_runs_is_reported_and_writes_nothing(world):
    app, store, transport, *_ = world
    app.policy._inner.owners["ghost"] = ("owner@example.com",)
    await app.handle(inbound("grant <@U2> read on ghost", user="U1"))
    assert store.history() == ()
    reply = said(transport).lower()
    assert "ghost" in reply and "no agent" in reply and "nothing was recorded" in reply
    assert "now holds" not in reply and "no longer" not in reply


async def test_no_store_means_no_grant_is_claimed(tmp_path):
    transport = Recorder()
    policy = RulesPolicy(rules=list(RULES), roster=ROSTER, owners={"n1": ("owner@example.com",)})
    app = Ricardi(
        transport=transport,
        backend=FakeBackend([Agent(name="n1", description="", cwd=str(tmp_path))]),
        policy=policy,
    )
    await app.handle(inbound("grant <@U2> read on n1", user="U1"))
    assert "couldn't" in said(transport).lower()


async def test_a_tier_is_never_written_to_a_thread_binding(world):
    # Flags stick to a thread by design; a tier must not, or one reader's tier
    # would follow the thread to whoever replies next.
    app, store, transport, _, tmp = world
    store.add(person="bob@example.com", name="n1", verb="ask", by="t")
    store.add(person="bob@example.com", name="n1", verb="read", by="t")
    await app.handle(inbound("tell n1 hello", user="U2", thread="tb"))
    await app.handle(inbound("and again", user="U2", thread="tb"))
    snapshot = json.loads((tmp / "threads.json").read_text(encoding="utf-8"))
    assert "tb" in snapshot
    assert "tier" not in json.dumps(snapshot).lower()
    assert "read" not in json.dumps(snapshot)
    assert set(snapshot["tb"]) <= {"agent", "flags"}


def test_an_owner_missing_from_the_roster_is_found():
    policy = RulesPolicy(
        rules=list(RULES), roster=ROSTER,
        owners={"n1": ("owner@example.com", "typo@example.com")},
    )
    assert cli.unrostered_owners(policy) == [("n1", "typo@example.com")]


def test_a_rostered_owner_is_not_reported():
    policy = RulesPolicy(
        rules=list(RULES), roster=ROSTER, owners={"n1": ("owner@example.com",)}
    )
    assert cli.unrostered_owners(policy) == []


def test_the_warning_names_the_owner_and_the_name(caplog):
    policy = RulesPolicy(
        rules=list(RULES), roster=ROSTER, owners={"n1": ("typo@example.com",)}
    )
    with caplog.at_level(logging.WARNING, logger="ricardi"):
        cli.warn_unrostered_owners(policy)
    assert "typo@example.com" in caplog.text and "n1" in caplog.text


async def test_serve_warns_and_still_starts_and_hands_the_app_the_store(tmp_path, caplog):
    from test_cli_readiness import POLICY, _args

    body = POLICY + """
[[owner]]
agent = "solo"
email = "typo@example.com"
"""
    state, args = _args(tmp_path, policy_body=body)
    seen = {}
    real = cli.Ricardi

    def spy(**kw):
        seen["grants"] = kw.get("grants")
        return real(**kw)

    cli.Ricardi = spy
    task = asyncio.create_task(cli._serve(args))
    try:
        with caplog.at_level(logging.WARNING, logger="ricardi"):
            for _ in range(250):
                if readiness.path_for(state).exists():
                    break
                await asyncio.sleep(0.02)
            assert readiness.path_for(state).exists(), "a roster typo stopped startup"
    finally:
        cli.Ricardi = real
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert "typo@example.com" in caplog.text
    assert isinstance(seen["grants"], GrantStore)
