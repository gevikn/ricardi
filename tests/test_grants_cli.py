"""The admin's hands on the grant store: list, seed, add, revoke.

The deploy that starts enforcing grants is only safe if the live store was
seeded first, and this is the only way to do that. Two things here matter
beyond the commands: a corrupt store must stop the command rather than print
an empty list, and adding a subcommand must not change how the container
(which passes flags and no subcommand) is launched.
"""

from __future__ import annotations

import argparse

import pytest

from ricardi import cli
from ricardi.cli import main
from ricardi.grants import GrantStore

POLICY = """
[[identity]]
slack = "U1"
email = "a@b.com"

[[identity]]
slack = "U2"
email = "c@d.com"

[[rule]]
name    = "everyone asks"
effect  = "allow"
actions = ["ask"]
agents  = ["shared"]
"""


def _db(tmp_path):
    return tmp_path / "grants.db"


def _store(tmp_path) -> GrantStore:
    return GrantStore.open(_db(tmp_path))


def _grants(tmp_path, *argv: str) -> int:
    return main(["grants", *argv, "--state-dir", str(tmp_path)])


def test_grants_list_prints_current_grants_not_history(tmp_path, capsys):
    s = _store(tmp_path)
    s.add(person="a@b.com", name="shared", verb="ask", by="admin@b.com")
    s.add(person="c@d.com", name="shared", verb="ask", by="admin@b.com")
    s.add(person="c@d.com", name="shared", verb="ask", by="admin@b.com", granted=False)
    s.close()
    assert _grants(tmp_path, "list") == 0
    out = capsys.readouterr().out
    assert "a@b.com" in out
    assert "c@d.com" not in out


def test_grants_seed_reports_how_many_rows_it_wrote(tmp_path, capsys):
    agents = tmp_path / "agents.toml"
    agents.write_text(
        f'[ricardi]\nstate_dir = "{tmp_path.as_posix()}"\n\n'
        f'[[agent]]\nname = "shared"\ncwd = "{tmp_path.as_posix()}"\n',
        encoding="utf-8",
    )
    policy = tmp_path / "policy.toml"
    policy.write_text(POLICY, encoding="utf-8")
    # No --state-dir: it must come from the --agents settings.
    rc = main(["--agents", str(agents), "--policy", str(policy),
               "grants", "seed", "--by", "admin@b.com"])
    assert rc == 0
    assert "2" in capsys.readouterr().out
    assert _store(tmp_path).holds("a@b.com", "shared", "ask") is True


def test_grants_add_normalises_the_email(tmp_path):
    assert _grants(tmp_path, "add", "--person", "  A@B.com ", "--name", "shared",
                   "--verb", "ask", "--by", "admin@b.com") == 0
    assert _store(tmp_path).holds("a@b.com", "shared", "ask") is True


def test_grants_revoke_leaves_the_history_intact(tmp_path):
    args = ["--person", "a@b.com", "--name", "shared", "--verb", "ask",
            "--by", "admin@b.com"]
    assert _grants(tmp_path, "add", *args) == 0
    assert _grants(tmp_path, "revoke", *args) == 0
    db = _db(tmp_path)
    assert GrantStore.open(db).holds("a@b.com", "shared", "ask") is False
    assert len(GrantStore.open(db).history()) == 2


def test_grants_commands_fail_closed_on_a_corrupt_store(tmp_path, capsys):
    _db(tmp_path).write_bytes(b"this is not a sqlite database" * 50)
    assert main(["grants", "list", "--state-dir", str(tmp_path)]) != 0
    assert "grants.db" in capsys.readouterr().err


def test_a_launch_with_flags_and_no_subcommand_still_parses(monkeypatch):
    # The container runs `ricardi --transport slack ...` and nothing else.
    seen: dict[str, argparse.Namespace] = {}

    async def fake_serve(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "_serve", fake_serve)
    assert main(["--backend", "fake", "--transport", "local", "--env", "nope"]) == 0
    assert seen["args"].command is None
    assert seen["args"].backend == "fake"


def test_state_dir_works_before_the_action_too(tmp_path, capsys):
    # The SUPPRESS parent-parser arrangement exists only for this form.
    s = _store(tmp_path)
    s.add(person="a@b.com", name="shared", verb="ask", by="admin@b.com")
    s.close()
    assert main(["grants", "--state-dir", str(tmp_path), "list"]) == 0
    assert "a@b.com" in capsys.readouterr().out
