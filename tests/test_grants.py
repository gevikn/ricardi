from __future__ import annotations

import pytest

from ricardi.grants import GrantError, GrantStore


def test_a_grant_is_held_once_added(tmp_path):
    s = GrantStore.open(tmp_path / "grants.db")
    s.add(person="a@b.com", name="shared", verb="ask", by="owner@b.com")
    assert s.holds("a@b.com", "shared", "ask") is True


def test_nothing_is_held_in_an_empty_store(tmp_path):
    s = GrantStore.open(tmp_path / "grants.db")
    assert s.holds("a@b.com", "shared", "ask") is False


def test_a_revocation_supersedes_without_deleting(tmp_path):
    s = GrantStore.open(tmp_path / "grants.db")
    s.add(person="a@b.com", name="shared", verb="ask", by="o@b.com")
    s.add(person="a@b.com", name="shared", verb="ask", by="o@b.com", granted=False)
    assert s.holds("a@b.com", "shared", "ask") is False
    assert len(s.history()) == 2          # both rows survive
    assert s.current() == ()              # nothing currently granted


def test_the_latest_row_wins_by_insertion_not_by_timestamp(tmp_path):
    # Review Focus 2: clock skew must not resurrect a revoked grant.
    s = GrantStore.open(tmp_path / "grants.db")
    s.add(person="a@b.com", name="shared", verb="ask", by="o@b.com", at="2099-01-01T00:00:00Z")
    s.add(person="a@b.com", name="shared", verb="ask", by="o@b.com", granted=False, at="2000-01-01T00:00:00Z")
    assert s.holds("a@b.com", "shared", "ask") is False


def test_email_case_and_whitespace_do_not_make_a_second_person(tmp_path):
    # Review Focus 5.
    s = GrantStore.open(tmp_path / "grants.db")
    s.add(person="  Gevik@Example.COM ", name="shared", verb="ask", by="o@b.com")
    assert s.holds("gevik@example.com", "shared", "ask") is True


def test_a_missing_file_is_created_and_an_empty_one_is_valid(tmp_path):
    # Review Focus 3.
    path = tmp_path / "nested" / "grants.db"
    s = GrantStore.open(path)
    assert path.exists()
    assert s.current() == ()


def test_a_corrupt_store_refuses_to_open(tmp_path):
    path = tmp_path / "grants.db"
    path.write_bytes(b"this is not a database")
    with pytest.raises(GrantError):
        GrantStore.open(path)


def test_a_second_connection_can_read_while_another_holds_one(tmp_path):
    # Review Focus 4: the CLI runs beside a live container.
    path = tmp_path / "grants.db"
    a = GrantStore.open(path)
    a.add(person="a@b.com", name="shared", verb="ask", by="o@b.com")
    b = GrantStore.open(path)
    assert b.holds("a@b.com", "shared", "ask") is True
