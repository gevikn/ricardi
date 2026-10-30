"""Snapshotting the grant store, and why a file copy will not do.

The store is the only state on the box that cannot be rebuilt from git: a
revoke that was recorded is not re-derivable from policy.toml, which is
exactly what makes it worth backing up. It is also in WAL mode, which is the
trap -- with a few rows and no checkpoint, `grants.db` on its own holds
nothing at all, not even the schema. Copying that file produces a snapshot
that opens cleanly and reports no grants, which is the worst shape a backup
can take: it fails on restore, long after the mistake.

So a snapshot goes through sqlite's backup API, and these tests pin the three
things that makes true -- rows still in the WAL arrive, the copy is verified
rather than assumed, and the copy is a real append-only store rather than a
dump of its rows.
"""

import sqlite3
from pathlib import Path

import pytest

from ricardi.grants import GrantError, GrantStore, backup_store


def _store_with_rows(tmp_path: Path, rows: int = 2) -> GrantStore:
    """A store left open, so its newest rows are still in the WAL."""
    store = GrantStore.open(tmp_path / "grants.db")
    for i in range(rows):
        store.add(person=f"p{i}@example.com", name="ricardi", verb="ask", by="owner")
    return store


def _rows(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM grant_log").fetchone()[0]
    finally:
        conn.close()


def test_a_file_copy_of_the_database_captures_nothing(tmp_path):
    """The failure this feature exists to avoid, asserted rather than assumed.

    If sqlite ever changes so that a small WAL is checkpointed eagerly, this
    test fails and the warnings written around this code become wrong. That is
    worth knowing, so it is a test and not a comment.
    """
    store = _store_with_rows(tmp_path)
    try:
        naive = tmp_path / "naive.db"
        naive.write_bytes((tmp_path / "grants.db").read_bytes())
        with pytest.raises(sqlite3.DatabaseError):
            _rows(naive)
    finally:
        store.close()


def test_backup_captures_rows_still_in_the_wal(tmp_path):
    store = _store_with_rows(tmp_path, rows=3)
    try:
        snapshot = backup_store(store, tmp_path / "backups", stamp="20261030T000000Z")
    finally:
        store.close()
    assert snapshot == tmp_path / "backups" / "grants-20261030T000000Z.db"
    assert _rows(snapshot) == 3


def test_the_snapshot_is_a_store_and_not_a_dump(tmp_path):
    """A restored snapshot must keep the guarantee the original enforces."""
    store = _store_with_rows(tmp_path)
    try:
        snapshot = backup_store(store, tmp_path / "backups", stamp="20261030T000001Z")
    finally:
        store.close()
    restored = GrantStore.open(snapshot)
    try:
        assert len(restored.history()) == 2
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            restored._conn.execute("UPDATE grant_log SET granted = 0")
    finally:
        restored.close()


def test_backup_keeps_only_the_newest(tmp_path):
    store = _store_with_rows(tmp_path, rows=1)
    try:
        for n in range(5):
            backup_store(store, tmp_path / "backups", stamp=f"2026103{n}T000000Z",
                         keep=3)
    finally:
        store.close()
    kept = sorted(p.name for p in (tmp_path / "backups").glob("grants-*.db"))
    assert kept == ["grants-20261032T000000Z.db", "grants-20261033T000000Z.db",
                    "grants-20261034T000000Z.db"]


def test_rotation_leaves_anything_it_did_not_write(tmp_path):
    """Pruning is scoped to its own filenames: a person's own copy stays."""
    store = _store_with_rows(tmp_path, rows=1)
    keepsake = tmp_path / "backups" / "before-the-migration.db"
    keepsake.parent.mkdir(parents=True)
    keepsake.write_text("mine")
    try:
        for n in range(3):
            backup_store(store, tmp_path / "backups", stamp=f"2026103{n}T000000Z",
                         keep=1)
    finally:
        store.close()
    assert keepsake.exists()


def test_a_copy_that_lost_rows_is_an_error_not_a_backup(tmp_path, monkeypatch):
    """Verification is the point: a snapshot nobody checked is a rumour."""
    store = _store_with_rows(tmp_path, rows=2)
    import ricardi.grants as grants_mod

    def _empty(_conn, dest: Path) -> None:
        sqlite3.connect(dest).close()

    monkeypatch.setattr(grants_mod, "_copy_database", _empty)
    try:
        with pytest.raises(GrantError, match="rows"):
            backup_store(store, tmp_path / "backups", stamp="20261030T000002Z")
    finally:
        store.close()
    assert not list((tmp_path / "backups").glob("grants-*.db"))


def test_a_closed_store_cannot_be_snapshotted(tmp_path):
    store = _store_with_rows(tmp_path, rows=1)
    store.close()
    with pytest.raises(GrantError):
        backup_store(store, tmp_path / "backups", stamp="20261030T000003Z")


# --- the CLI verb, which is what cron on the box actually calls -------------


def _grants_cli(tmp_path: Path, *argv: str) -> int:
    from ricardi.cli import main

    return main(["grants", *argv, "--state-dir", str(tmp_path)])


def test_cli_backup_defaults_to_a_backups_dir_beside_the_store(tmp_path, capsys):
    store = _store_with_rows(tmp_path, rows=2)
    store.close()
    assert _grants_cli(tmp_path, "backup") == 0
    written = list((tmp_path / "backups").glob("grants-*.db"))
    assert len(written) == 1
    assert _rows(written[0]) == 2
    assert str(written[0]) in capsys.readouterr().out


def test_cli_backup_takes_a_destination_and_a_keep_count(tmp_path, capsys):
    store = _store_with_rows(tmp_path, rows=1)
    store.close()
    elsewhere = tmp_path / "elsewhere"
    for _ in range(2):
        assert _grants_cli(tmp_path, "backup", "--to", str(elsewhere), "--keep", "1") == 0
    assert len(list(elsewhere.glob("grants-*.db"))) == 1


def test_cli_backup_on_a_corrupt_store_fails_rather_than_writing_one(tmp_path, capsys):
    (tmp_path / "grants.db").write_text("not a database", encoding="utf-8")
    assert _grants_cli(tmp_path, "backup") == 2
    assert "ricardi:" in capsys.readouterr().err
    assert not (tmp_path / "backups").exists()


def test_a_snapshot_is_one_self_contained_file(tmp_path):
    """No sidecars, because a backup you have to copy three of is a trap.

    The live store is three files and that is the hazard documented in
    deploying.md. A snapshot must not reproduce it: whoever restores one will
    copy a single file, so the snapshot is taken out of WAL mode before it is
    closed, and a byte-for-byte copy of it has to carry every row.
    """
    store = _store_with_rows(tmp_path, rows=2)
    try:
        snapshot = backup_store(store, tmp_path / "backups", stamp="20261030T000004Z")
    finally:
        store.close()

    beside = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert beside == ["grants-20261030T000004Z.db"]

    lone = tmp_path / "carried-away.db"
    lone.write_bytes(snapshot.read_bytes())
    assert _rows(lone) == 2
