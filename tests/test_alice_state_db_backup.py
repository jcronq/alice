"""Tests for ``alice_state_db.backup`` / ``restore`` / ``verify`` (issue #831).

The real deployment ships bytes to a NAS over rsync + ssh. For the
unit-test surface we inject :class:`LocalTransport` and treat a tmp
directory as the NAS — that exercises every code path (list,
upload, verify, delete) without needing a network peer. The
transport protocol is deliberately narrow so the local/ssh pair
stay exchangeable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sqlite3

import pytest

from alice_state_db import backup as backup_mod
from alice_state_db import restore as restore_mod
from alice_state_db import verify as verify_mod


# ---------------------------------------------------------------------------
# Fixtures


def _seed_live_db(path: pathlib.Path, *, rows: int = 3) -> None:
    """Create a small sqlite DB with the SM v2 table shapes the verify
    path cares about. We don't rely on ``alice_state_db.schema`` so
    these tests don't drag in the full project schema when iterating.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, body TEXT);")
        conn.execute(
            "CREATE TABLE state_machine (id INTEGER PRIMARY KEY, state TEXT);"
        )
        conn.execute(
            "CREATE TABLE transitions (id INTEGER PRIMARY KEY, prev TEXT, next TEXT);"
        )
        for i in range(rows):
            conn.execute("INSERT INTO items (body) VALUES (?);", (f"item-{i}",))
            conn.execute(
                "INSERT INTO state_machine (state) VALUES (?);", (f"state-{i}",)
            )
            conn.execute(
                "INSERT INTO transitions (prev, next) VALUES (?, ?);",
                (f"s{i}", f"s{i+1}"),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def live_db(tmp_path: pathlib.Path) -> pathlib.Path:
    mind = tmp_path / "mind"
    (mind / "inner" / "state").mkdir(parents=True)
    db = mind / "inner" / "state" / "alice-state.db"
    _seed_live_db(db)
    return db


@pytest.fixture
def mind(live_db: pathlib.Path) -> pathlib.Path:
    return live_db.parents[2]


@pytest.fixture
def nas_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "nas" / "alice-state-db"
    root.mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# Snapshot + checksum


def test_snapshot_db_produces_vacuum_copy_with_matching_rows(
    live_db: pathlib.Path, tmp_path: pathlib.Path
):
    out = tmp_path / "snap.db"
    backup_mod.snapshot_db(live_db, out)
    assert out.exists()
    # Opening the snapshot should show the same rows as the live DB.
    with sqlite3.connect(out) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items;").fetchone()[0] == 3


def test_snapshot_db_refuses_existing_dest(
    live_db: pathlib.Path, tmp_path: pathlib.Path
):
    out = tmp_path / "snap.db"
    out.write_bytes(b"placeholder")
    with pytest.raises(backup_mod.BackupError, match="already exists"):
        backup_mod.snapshot_db(live_db, out)


def test_compute_sha256_matches_stdlib(tmp_path: pathlib.Path):
    path = tmp_path / "f"
    payload = b"hello alice\n"
    path.write_bytes(payload)
    assert backup_mod.compute_sha256(path) == hashlib.sha256(payload).hexdigest()


def test_write_sha256_manifest_shasum_compatible(tmp_path: pathlib.Path):
    db = tmp_path / "alice-state-db-20260101-0000.db"
    db.write_bytes(b"x")
    sha = hashlib.sha256(b"x").hexdigest()
    m = backup_mod.write_sha256_manifest(db, sha)
    assert m.read_text(encoding="ascii") == f"{sha}  {db.name}\n"


# ---------------------------------------------------------------------------
# Retention planning


def _snap(ts: str) -> backup_mod.ParsedSnapshot:
    stamp = dt.datetime.strptime(ts, "%Y%m%d-%H%M")
    rel = (
        f"{stamp:%Y}/{stamp:%m}/{stamp:%d}/"
        f"alice-state-db-{stamp:%Y%m%d-%H%M}.db"
    )
    return backup_mod.ParsedSnapshot(relpath=rel, ts=stamp)


def test_parse_snapshots_filters_non_matching():
    out = backup_mod.parse_snapshots(
        [
            "2026/01/01/alice-state-db-20260101-0000.db",
            "2026/01/01/alice-state-db-20260101-0100.db",
            "2026/01/01/garbage.db",
            "2026/01/01/alice-state-db-20260101-0000.db.sha256",
        ]
    )
    assert [s.ts.hour for s in out] == [0, 1]


def test_pick_retained_keeps_at_least_newest_with_empty_retention():
    snapshots = [_snap("20260101-0000"), _snap("20260101-0100")]
    retained = backup_mod.pick_retained(snapshots, {}, now=dt.datetime(2026, 1, 1))
    assert retained == {snapshots[-1].relpath}


def test_pick_retained_daily_bucket_keeps_latest_per_day():
    snapshots = [
        _snap("20260101-0000"),
        _snap("20260101-2300"),
        _snap("20260102-0100"),
        _snap("20260102-2300"),
        _snap("20260103-0000"),
    ]
    retained = backup_mod.pick_retained(
        snapshots, {"daily": 2}, now=dt.datetime(2026, 1, 3, 1)
    )
    # Two most-recent daily ends — Jan 3 and Jan 2 late — plus the
    # unconditional newest (same as Jan 3 end in this fixture).
    assert snapshots[-1].relpath in retained  # Jan 3 0000
    assert snapshots[3].relpath in retained  # Jan 2 2300
    assert snapshots[1].relpath not in retained  # Jan 1 2300 dropped
    assert snapshots[0].relpath not in retained


def test_plan_rotation_selects_snapshots_outside_window():
    snapshots = [_snap(f"202601{d:02d}-0000") for d in range(1, 11)]
    retention = {"daily": 3}
    now = dt.datetime(2026, 1, 11)
    to_delete = backup_mod.plan_rotation(snapshots, retention, now=now)
    deleted_names = {s.relpath for s in to_delete}
    # The newest 3 dailies survive, plus the always-keep newest
    # (which is the same set in this fixture).
    assert snapshots[-1].relpath not in deleted_names
    assert snapshots[-2].relpath not in deleted_names
    assert snapshots[-3].relpath not in deleted_names
    for s in snapshots[:-3]:
        assert s.relpath in deleted_names


# ---------------------------------------------------------------------------
# End-to-end backup via LocalTransport


def test_run_backup_writes_db_sha_and_backup_note(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    now = dt.datetime(2026, 2, 3, 14, 15)
    result = backup_mod.run_backup(
        source=live_db,
        dest=str(nas_root),
        mind=mind,
        retention={"daily": 7, "weekly": 4, "monthly": 12},
        transport=backup_mod.LocalTransport(),
        now=now,
    )
    assert result.status == "ok", result
    db_path = nas_root / "2026/02/03/alice-state-db-20260203-1415.db"
    sha_path = db_path.with_suffix(db_path.suffix + ".sha256")
    assert db_path.exists()
    assert sha_path.exists()
    hexdigest = backup_mod.compute_sha256(db_path)
    assert sha_path.read_text(encoding="ascii").split()[0] == hexdigest
    # Backup note landed in inner/notes/.
    notes = list((mind / "inner" / "notes").glob(
        "alice-state-db-backup-*.jsonl"
    ))
    assert len(notes) == 1
    entry = json.loads(notes[0].read_text(encoding="utf-8").splitlines()[0])
    assert entry["status"] == "ok"
    assert entry["sha256"] == hexdigest


def test_run_backup_prunes_only_after_verify(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    # Pre-populate the NAS with an old snapshot that should get
    # rotated out.
    old_dir = nas_root / "2026/01/01"
    old_dir.mkdir(parents=True)
    old_db = old_dir / "alice-state-db-20260101-0000.db"
    old_db.write_bytes(b"x")
    (old_dir / "alice-state-db-20260101-0000.db.sha256").write_text(
        f"{hashlib.sha256(b'x').hexdigest()}  {old_db.name}\n"
    )
    result = backup_mod.run_backup(
        source=live_db,
        dest=str(nas_root),
        mind=mind,
        retention={"daily": 1},
        transport=backup_mod.LocalTransport(),
        now=dt.datetime(2026, 2, 3, 14, 15),
    )
    assert result.status == "ok"
    # Old snapshot should be gone.
    assert not old_db.exists()
    # New snapshot should exist.
    new_db = nas_root / "2026/02/03/alice-state-db-20260203-1415.db"
    assert new_db.exists()
    assert len(result.deleted) == 1


def test_run_backup_snapshot_failure_does_not_touch_nas(
    tmp_path: pathlib.Path, nas_root: pathlib.Path
):
    bogus = tmp_path / "nope.db"  # does not exist
    result = backup_mod.run_backup(
        source=bogus,
        dest=str(nas_root),
        mind=None,
        transport=backup_mod.LocalTransport(),
        now=dt.datetime(2026, 2, 3, 14, 15),
    )
    assert result.status == "snapshot_failed"
    assert list(nas_root.rglob("*.db")) == []


class _CorruptingTransport(backup_mod.LocalTransport):
    """Flip one byte in the .db after upload — makes sha mismatch."""

    def upload(self, local, remote):
        super().upload(local, remote)
        if remote.full.endswith(".db"):
            path = pathlib.Path(remote.path)
            data = bytearray(path.read_bytes())
            data[0] ^= 0xFF
            path.write_bytes(bytes(data))


def test_run_backup_verify_mismatch_aborts_rotation(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    old_dir = nas_root / "2026/01/01"
    old_dir.mkdir(parents=True)
    old_db = old_dir / "alice-state-db-20260101-0000.db"
    old_db.write_bytes(b"x")
    result = backup_mod.run_backup(
        source=live_db,
        dest=str(nas_root),
        mind=mind,
        retention={"daily": 1},
        transport=_CorruptingTransport(),
        now=dt.datetime(2026, 2, 3, 14, 15),
    )
    assert result.status == "verify_failed"
    # Old snapshot MUST survive — we never prune on verify failure.
    assert old_db.exists()


# ---------------------------------------------------------------------------
# Restore


def test_restore_pulls_latest_valid_and_swaps_atomically(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    # Seed NAS with two backups; the newest should win.
    for ts in ("20260101-0000", "20260202-0000"):
        now = dt.datetime.strptime(ts, "%Y%m%d-%H%M")
        backup_mod.run_backup(
            source=live_db,
            dest=str(nas_root),
            mind=None,
            transport=backup_mod.LocalTransport(),
            now=now,
        )
    # Mutate the live DB so we can prove restore replaced it.
    with sqlite3.connect(live_db) as c:
        c.execute("INSERT INTO items (body) VALUES ('after-backup');")
        c.commit()
    assert sqlite3.connect(live_db).execute(
        "SELECT COUNT(*) FROM items;"
    ).fetchone()[0] == 4
    safety = mind / "safety"
    result = restore_mod.run_restore(
        dest=str(nas_root),
        target=live_db,
        safety_dir=safety,
        transport=restore_mod._LocalWithFetch(),
        now=dt.datetime(2026, 2, 3, 0, 0),
    )
    assert result.status == "ok", result
    assert result.snapshot.endswith("-20260202-0000.db")
    # Live DB should be back to 3 rows (the snapshot state).
    assert sqlite3.connect(live_db).execute(
        "SELECT COUNT(*) FROM items;"
    ).fetchone()[0] == 3
    # A safety copy exists with the post-insert mutation.
    staged = list(safety.glob("alice-state.db.pre-restore-*"))
    assert len(staged) == 1
    assert sqlite3.connect(staged[0]).execute(
        "SELECT COUNT(*) FROM items;"
    ).fetchone()[0] == 4


def test_restore_skips_corrupt_latest_falls_back_to_prior(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    # Seed one good + one corrupt snapshot (corrupt is newer).
    good_now = dt.datetime(2026, 1, 1, 0, 0)
    bad_now = dt.datetime(2026, 2, 2, 0, 0)
    backup_mod.run_backup(
        source=live_db,
        dest=str(nas_root),
        mind=None,
        transport=backup_mod.LocalTransport(),
        now=good_now,
    )
    backup_mod.run_backup(
        source=live_db,
        dest=str(nas_root),
        mind=None,
        transport=backup_mod.LocalTransport(),
        now=bad_now,
    )
    # Corrupt the newer .db without touching its .sha256 → mismatch.
    bad_db = (
        nas_root / "2026/02/02/alice-state-db-20260202-0000.db"
    )
    data = bytearray(bad_db.read_bytes())
    data[0] ^= 0xFF
    bad_db.write_bytes(bytes(data))

    result = restore_mod.run_restore(
        dest=str(nas_root),
        target=live_db,
        safety_dir=mind / "safety",
        transport=restore_mod._LocalWithFetch(),
        now=dt.datetime(2026, 2, 3, 0, 0),
    )
    assert result.status == "ok"
    assert result.snapshot.endswith("-20260101-0000.db")


def test_restore_refuses_when_wal_sidecar_present(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    backup_mod.run_backup(
        source=live_db,
        dest=str(nas_root),
        mind=None,
        transport=backup_mod.LocalTransport(),
        now=dt.datetime(2026, 1, 1, 0, 0),
    )
    # Create a dangling WAL to simulate a running writer.
    wal = live_db.with_name(live_db.name + "-wal")
    wal.write_bytes(b"\x00" * 32)

    result = restore_mod.run_restore(
        dest=str(nas_root),
        target=live_db,
        safety_dir=mind / "safety",
        transport=restore_mod._LocalWithFetch(),
        now=dt.datetime(2026, 2, 3, 0, 0),
    )
    assert result.status == "live_wal_present"


def test_restore_reports_no_valid_snapshot_when_nas_empty(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    result = restore_mod.run_restore(
        dest=str(nas_root),
        target=live_db,
        safety_dir=mind / "safety",
        transport=restore_mod._LocalWithFetch(),
        now=dt.datetime(2026, 2, 3, 0, 0),
    )
    assert result.status == "no_valid_snapshot"


# ---------------------------------------------------------------------------
# Verify


def test_verify_happy_path_no_conflict_note_when_rows_match(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    backup_mod.run_backup(
        source=live_db,
        dest=str(nas_root),
        mind=None,
        transport=backup_mod.LocalTransport(),
        now=dt.datetime(2026, 1, 1, 0, 0),
    )
    result = verify_mod.run_verify(
        dest=str(nas_root),
        live_db=live_db,
        mind=mind,
        transport=restore_mod._LocalWithFetch(),
        now=dt.datetime(2026, 1, 2, 0, 0),
    )
    assert result.status == "ok"
    assert result.integrity == "ok"
    assert result.conflict_note_path is None


def test_verify_emits_conflict_note_on_large_delta(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    # Backup now; then mutate live DB heavily.
    backup_mod.run_backup(
        source=live_db,
        dest=str(nas_root),
        mind=None,
        transport=backup_mod.LocalTransport(),
        now=dt.datetime(2026, 1, 1, 0, 0),
    )
    with sqlite3.connect(live_db) as c:
        for i in range(50):
            c.execute("INSERT INTO items (body) VALUES (?);", (f"big-{i}",))
        c.commit()
    result = verify_mod.run_verify(
        dest=str(nas_root),
        live_db=live_db,
        mind=mind,
        transport=restore_mod._LocalWithFetch(),
        delta_threshold=0.05,
        now=dt.datetime(2026, 1, 2, 0, 0),
    )
    assert result.status == "ok"
    assert result.conflict_note_path is not None
    note = pathlib.Path(result.conflict_note_path)
    assert note.exists()
    assert "items" in note.read_text(encoding="utf-8")
    # The note landed in inner/notes/ tagged conflict-candidate.
    assert "conflict-candidate" in note.read_text(encoding="utf-8")


def test_verify_fails_on_corrupt_snapshot(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    backup_mod.run_backup(
        source=live_db,
        dest=str(nas_root),
        mind=None,
        transport=backup_mod.LocalTransport(),
        now=dt.datetime(2026, 1, 1, 0, 0),
    )
    # Corrupt the .db beyond integrity_check recovery, but preserve
    # the manifest so sha-check passes and we reach integrity_check.
    db = nas_root / "2026/01/01/alice-state-db-20260101-0000.db"
    # Zero-out most of the file — sqlite will not open it.
    db.write_bytes(b"\x00" * db.stat().st_size)
    sha = hashlib.sha256(db.read_bytes()).hexdigest()
    db.with_suffix(db.suffix + ".sha256").write_text(
        f"{sha}  {db.name}\n"
    )
    notifications: list[str] = []
    result = verify_mod.run_verify(
        dest=str(nas_root),
        live_db=live_db,
        mind=mind,
        transport=restore_mod._LocalWithFetch(),
        now=dt.datetime(2026, 1, 2, 0, 0),
        notify=notifications.append,
    )
    assert result.status == "integrity_failed"
    assert notifications and "verify" in notifications[0]


def test_verify_reports_no_snapshot_when_nas_empty(
    live_db: pathlib.Path, mind: pathlib.Path, nas_root: pathlib.Path
):
    result = verify_mod.run_verify(
        dest=str(nas_root),
        live_db=live_db,
        mind=mind,
        transport=restore_mod._LocalWithFetch(),
        now=dt.datetime(2026, 1, 2, 0, 0),
    )
    assert result.status == "no_snapshot"


# ---------------------------------------------------------------------------
# Remote-path parsing


def test_remote_path_parse_local_vs_ssh():
    rp = backup_mod.RemotePath.parse("/tmp/x")
    assert rp.is_local
    assert rp.full == "/tmp/x"
    rp = backup_mod.RemotePath.parse("user@host:/tmp/x")
    assert not rp.is_local
    assert rp.full == "user@host:/tmp/x"
    # with_suffix_added keeps the host on the right side.
    rp2 = rp.with_suffix_added(".sha256")
    assert rp2.full == "user@host:/tmp/x.sha256"
