"""Restore ``alice-state.db`` from the newest valid NAS snapshot.

This is the companion to :mod:`alice_state_db.backup`. It assumes
the backup layout ``YYYY/MM/DD/alice-state-db-YYYYMMDD-HHMM.db`` +
sibling ``.sha256`` manifest.

Procedure (never bypass):

1. **Find latest valid snapshot** — enumerate the NAS, parse names
   into timestamps, pull the newest ``.db`` + ``.sha256`` to a tmp
   dir, verify the sha256 locally. If the newest fails verification,
   walk back to the next-newest. Fail loudly if none verify.
2. **PRAGMA integrity_check** — a clean checksum proves the bytes
   survived transport but not that the DB is valid. Run the sqlite
   integrity check locally against the fetched file; abort on any
   result other than literal ``"ok"``.
3. **Safety copy** — move the current ``alice-state.db`` (and its
   ``-wal`` / ``-shm`` sidecars, if present) to
   ``<safety-dir>/alice-state.db.pre-restore-<ts>`` so a human can
   roll back if the restored DB turns out to be unexpectedly stale.
4. **Atomic swap** — move the verified copy into place via
   :func:`os.replace`. This is same-filesystem atomic; we fetched
   the snapshot into a tmp dir on the restore host so the caller
   must ensure the restore tmp dir and ``alice-state.db`` share a
   mount. We default the fetch tmp dir to a sibling of the target
   DB to make that true.

The script refuses to overwrite a live DB that's been opened in WAL
mode without the operator explicitly passing ``--force`` — a running
writer would be stranded with its journal dangling. In the normal
case the operator stops the alice container, runs restore, then
restarts.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence

from alice_state_db.backup import (
    BackupError,
    LocalTransport,
    ParsedSnapshot,
    RemotePath,
    SSHRsyncTransport,
    Transport,
    _local_sha256,
    parse_snapshots,
)


LOG = logging.getLogger("alice_state_db.restore")


DEFAULT_TARGET = pathlib.Path(
    os.environ.get(
        "ALICE_STATE_DB_SOURCE",
        "/home/alice/alice-mind/inner/state/alice-state.db",
    )
)


@dataclasses.dataclass
class RestoreResult:
    status: str  # "ok" | "no_valid_snapshot" | "fetch_failed" | "integrity_failed"
    snapshot: str | None
    sha256: str | None
    integrity: str | None
    safety_copy: str | None
    error: str | None = None


class _SSHWithFetch(SSHRsyncTransport):
    """Extends the shared SSH transport with a ``download`` primitive
    the restore/verify path needs. Kept separate so the backup
    Transport Protocol stays minimal."""

    def download(self, remote: RemotePath, local: pathlib.Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        if remote.is_local:
            shutil.copy2(pathlib.Path(remote.path), local)
            return
        import subprocess as _sp
        res = _sp.run(
            ["rsync", "-a", "--partial", remote.full, str(local)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise BackupError(
                f"rsync download failed ({res.returncode}): {res.stderr.strip()}"
            )


class _LocalWithFetch(LocalTransport):
    def download(self, remote: RemotePath, local: pathlib.Path) -> None:
        assert remote.is_local
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pathlib.Path(remote.path), local)


def _default_transport(dest: str) -> Transport:
    """Pick LocalTransport for local dests, SSH+rsync for remote.

    Both variants extend the shared :class:`Transport` protocol with
    an extra ``download`` method the restore/verify flow needs.
    """
    remote = RemotePath.parse(dest)
    return _LocalWithFetch() if remote.is_local else _SSHWithFetch()


def find_latest_valid_snapshot(
    transport: Transport,
    remote_root: RemotePath,
    workdir: pathlib.Path,
) -> tuple[ParsedSnapshot, pathlib.Path, str] | None:
    """Download snapshots newest-first until one verifies sha256.

    Returns ``(snap, local_path, sha256)`` or ``None`` if the NAS is
    empty / every candidate fails verification.
    """
    entries = transport.list_remote(remote_root)
    snapshots = parse_snapshots(entries)
    if not snapshots:
        return None
    for snap in reversed(snapshots):
        remote_db = remote_root.child(snap.relpath)
        remote_sha = remote_root.child(snap.relpath + ".sha256")
        local_db = workdir / pathlib.PurePosixPath(snap.relpath).name
        local_sha_path = workdir / (
            pathlib.PurePosixPath(snap.relpath).name + ".sha256"
        )
        try:
            transport.download(remote_db, local_db)  # type: ignore[attr-defined]
            transport.download(remote_sha, local_sha_path)  # type: ignore[attr-defined]
        except BackupError as exc:
            LOG.warning("fetch failed for %s: %s", snap.relpath, exc)
            continue
        expected = _parse_manifest(local_sha_path)
        if expected is None:
            LOG.warning("manifest unreadable: %s", local_sha_path)
            continue
        actual = _local_sha256(local_db)
        if actual != expected:
            LOG.warning(
                "sha mismatch for %s: manifest=%s actual=%s",
                snap.relpath,
                expected,
                actual,
            )
            continue
        return snap, local_db, actual
    return None


def _parse_manifest(path: pathlib.Path) -> str | None:
    try:
        text = path.read_text(encoding="ascii")
    except OSError:
        return None
    line = text.strip().splitlines()[0] if text.strip() else ""
    if not line:
        return None
    return line.split()[0]


def integrity_check(db_path: pathlib.Path) -> str:
    """Run ``PRAGMA integrity_check`` and return the result string.

    SQLite returns ``"ok"`` for a clean DB. Any other value — or an
    exception, which we re-raise as :class:`BackupError` — means we
    will NOT swap this file in.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check;").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"integrity_check raised: {exc}")
    if not row:
        raise BackupError("integrity_check returned no rows")
    return str(row[0])


def safety_copy_live(
    target: pathlib.Path,
    safety_dir: pathlib.Path,
    now: dt.datetime,
) -> pathlib.Path | None:
    """Move the live DB + sidecars aside. Returns the staged path.

    Returns ``None`` if the live DB doesn't exist (first-time restore).
    """
    if not target.exists():
        return None
    safety_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    staged = safety_dir / f"{target.name}.pre-restore-{stamp}"
    shutil.move(str(target), str(staged))
    # Sidecars are only present with WAL — move them along so SQLite
    # doesn't try to recover a dangling journal against the new file.
    for suffix in ("-wal", "-shm"):
        side = target.with_name(target.name + suffix)
        if side.exists():
            shutil.move(str(side), str(staged) + suffix)
    return staged


def atomic_swap(new_db: pathlib.Path, target: pathlib.Path) -> None:
    """Move ``new_db`` → ``target`` with POSIX atomicity.

    Caller MUST have passed integrity_check on ``new_db`` already.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(new_db), str(target))


def run_restore(
    *,
    dest: str,
    target: pathlib.Path = DEFAULT_TARGET,
    safety_dir: pathlib.Path | None = None,
    transport: Transport | None = None,
    workdir: pathlib.Path | None = None,
    now: dt.datetime | None = None,
    force: bool = False,
) -> RestoreResult:
    if now is None:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if safety_dir is None:
        safety_dir = target.parent / "pre-restore"
    if transport is None:
        transport = _default_transport(dest)
    remote_root = RemotePath.parse(dest)

    # Operator-safety: refuse to overwrite a live DB whose WAL
    # sidecar is still around unless --force. The container should
    # be stopped before restore.
    wal = target.with_name(target.name + "-wal")
    if wal.exists() and not force:
        return RestoreResult(
            status="live_wal_present",
            snapshot=None,
            sha256=None,
            integrity=None,
            safety_copy=None,
            error=(
                f"{wal} exists — stop the alice container first "
                f"or pass --force"
            ),
        )

    cleanup_dir: tempfile.TemporaryDirectory[str] | None = None
    if workdir is None:
        cleanup_dir = tempfile.TemporaryDirectory(
            prefix="alice-state-db-restore-",
            dir=str(target.parent),
        )
        workdir = pathlib.Path(cleanup_dir.name)

    try:
        found = find_latest_valid_snapshot(transport, remote_root, workdir)
        if found is None:
            return RestoreResult(
                status="no_valid_snapshot",
                snapshot=None,
                sha256=None,
                integrity=None,
                safety_copy=None,
                error="no snapshot on NAS verified sha256",
            )
        snap, local_db, sha = found
        try:
            integrity = integrity_check(local_db)
        except BackupError as exc:
            return RestoreResult(
                status="integrity_failed",
                snapshot=snap.relpath,
                sha256=sha,
                integrity=None,
                safety_copy=None,
                error=str(exc),
            )
        if integrity != "ok":
            return RestoreResult(
                status="integrity_failed",
                snapshot=snap.relpath,
                sha256=sha,
                integrity=integrity,
                safety_copy=None,
                error=f"integrity_check returned {integrity!r}",
            )
        staged = safety_copy_live(target, safety_dir, now)
        atomic_swap(local_db, target)
        return RestoreResult(
            status="ok",
            snapshot=snap.relpath,
            sha256=sha,
            integrity=integrity,
            safety_copy=str(staged) if staged else None,
        )
    finally:
        if cleanup_dir is not None:
            cleanup_dir.cleanup()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alice-state-db-restore",
        description=(
            "Pull the newest valid alice-state.db snapshot from NAS "
            "and atomically swap it over the live DB."
        ),
    )
    parser.add_argument(
        "--source",
        dest="dest",
        default=os.environ.get(
            "ALICE_STATE_DB_BACKUP_DEST",
            "cronqj@10.20.30.14:/mnt/pool0/backups/alice-state-db/",
        ),
        help="NAS source (user@host:/abs/path or /local/abs/path)",
    )
    parser.add_argument(
        "--target",
        type=pathlib.Path,
        default=DEFAULT_TARGET,
        help=f"live DB path to overwrite (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--safety-dir",
        type=pathlib.Path,
        default=None,
        help=(
            "where to stash the current DB before swap "
            "(default: <target_dir>/pre-restore)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow restore even if a WAL sidecar is present",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = run_restore(
        dest=args.dest,
        target=args.target,
        safety_dir=args.safety_dir,
        force=args.force,
    )
    print(json.dumps(dataclasses.asdict(result), sort_keys=True))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
