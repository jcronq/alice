"""NAS backup for ``alice-state.db`` (issue #831).

The DB is the source of truth for SM v2 state. GitHub is being
deprecated as accidental backup, so corruption of ``alice-state.db``
without a restore path means permanent state loss. This module
produces a consistent snapshot on a configurable cadence, pushes it
to a remote NAS target, verifies the checksum on the remote side,
and rotates older snapshots per a configurable retention window.

Design:

* **Consistent snapshot.** We open a read-only connection to the
  live DB and run ``VACUUM INTO`` to a tmp file. That emits a clean
  copy without WAL/SHM sidecars and is safe against concurrent
  writers (SQLite holds an internal lock for the duration; the
  operation blocks writers for the ~hundreds-of-ms it takes on a
  4-MB DB).
* **Checksum.** Compute ``sha256`` locally and write a sibling
  ``<name>.sha256`` manifest in the standard ``shasum`` format
  (``<hex>  <basename>``). After upload we invoke ``sha256sum -c``
  on the remote to confirm bit-for-bit transport fidelity. A
  mismatch aborts rotation — we never prune a valid snapshot in
  favour of a corrupted one.
* **Retention.** Three buckets — daily/weekly/monthly — configurable
  via ``--retention`` JSON or ``ALICE_STATE_DB_BACKUP_RETENTION``.
  The rotation step enumerates the remote directory, groups by
  bucket, keeps the most recent N per bucket, and deletes the rest.
* **Transport abstraction.** A :class:`Transport` protocol wraps
  the three remote primitives (``upload``, ``verify_remote``,
  ``list_remote``, ``delete_remote``). The default implementation
  shells to ``rsync``/``ssh``. Tests inject a :class:`LocalTransport`
  that treats a tmp directory as the NAS.
* **Backup note.** Every run appends a JSON line to
  ``inner/notes/alice-state-db-backup-<date>.jsonl`` so the memory
  worker drains it into ``events.jsonl``. One line per snapshot
  carries ``(ts, size, sha256, status)``.

The module is also runnable as ``python -m alice_state_db.backup``
and shimmed from ``bin/alice-state-db-backup``.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from typing import Protocol


LOG = logging.getLogger("alice_state_db.backup")


# ---------------------------------------------------------------------------
# Defaults

DEFAULT_SOURCE = pathlib.Path(
    os.environ.get(
        "ALICE_STATE_DB_SOURCE",
        "/home/alice/alice-mind/inner/state/alice-state.db",
    )
)

DEFAULT_DEST = os.environ.get(
    "ALICE_STATE_DB_BACKUP_DEST",
    "cronqj@10.20.30.14:/mnt/pool0/backups/alice-state-db/",
)

DEFAULT_RETENTION: dict[str, int] = {"daily": 7, "weekly": 4, "monthly": 12}

# Snapshot filename pattern. ``YYYYMMDD-HHMM`` keeps lexical sort =
# chronological sort and matches what cron's hourly cadence produces.
_SNAPSHOT_RE = re.compile(
    r"^alice-state-db-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\.db$"
)


# ---------------------------------------------------------------------------
# Transport abstraction


@dataclasses.dataclass(frozen=True)
class RemotePath:
    """A ``user@host:/abs/path`` destination split into parts.

    ``host`` is ``None`` for a local filesystem path (used by tests
    and the local-only mode). Otherwise the full remote spec is
    rebuilt via :attr:`full`.
    """

    host: str | None  # ``user@host`` form, or ``None`` for local
    path: pathlib.PurePosixPath

    @classmethod
    def parse(cls, spec: str) -> "RemotePath":
        if ":" in spec and not spec.startswith("/"):
            host, _, path = spec.partition(":")
            return cls(host=host, path=pathlib.PurePosixPath(path))
        return cls(host=None, path=pathlib.PurePosixPath(spec))

    @property
    def is_local(self) -> bool:
        return self.host is None

    def child(self, *parts: str) -> "RemotePath":
        return RemotePath(host=self.host, path=self.path.joinpath(*parts))

    def with_suffix_added(self, suffix: str) -> "RemotePath":
        """Return the same path with ``suffix`` appended (e.g. ``.sha256``)."""
        return RemotePath(
            host=self.host,
            path=pathlib.PurePosixPath(str(self.path) + suffix),
        )

    @property
    def full(self) -> str:
        if self.host is None:
            return str(self.path)
        return f"{self.host}:{self.path}"


class Transport(Protocol):
    """Pluggable remote I/O so tests can inject a local filesystem."""

    def upload(self, local: pathlib.Path, remote: RemotePath) -> None: ...

    def verify_remote(self, remote_db: RemotePath, expected_sha256: str) -> bool: ...

    def list_remote(self, remote_root: RemotePath) -> list[str]: ...

    def delete_remote(self, remote: RemotePath) -> None: ...


class SSHRsyncTransport:
    """Real transport: ``rsync -a --partial`` + ``ssh`` for verify/list/delete.

    The remote must have ``sha256sum``, ``find``, and ``rm`` on
    ``PATH``. SSH auth is caller-managed — no key loading here.
    """

    def __init__(self, ssh_opts: Sequence[str] = ()) -> None:
        self._ssh_opts = list(ssh_opts)

    def _ssh(self, host: str, cmd: str) -> subprocess.CompletedProcess[str]:
        # NB: shell-quote in caller's cmd. We pass a single string so
        # the remote shell parses it — that lets us use pipes etc.
        return subprocess.run(
            ["ssh", *self._ssh_opts, host, cmd],
            capture_output=True,
            text=True,
            check=False,
        )

    def upload(self, local: pathlib.Path, remote: RemotePath) -> None:
        # ``rsync`` creates parent dirs under the destination with
        # --mkpath (rsync >=3.2). The NAS is Linux so we can rely on
        # it, but fall back to a pre-ssh mkdir for older rsync. The
        # cheap path: always run `ssh host mkdir -p` first.
        if remote.host is not None:
            parent = remote.path.parent
            res = self._ssh(remote.host, f"mkdir -p {_shq(str(parent))}")
            if res.returncode != 0:
                raise BackupError(
                    f"remote mkdir failed: {res.stderr.strip() or res.stdout.strip()}"
                )
        cmd = ["rsync", "-a", "--partial", str(local), remote.full]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise BackupError(
                f"rsync upload failed ({res.returncode}): {res.stderr.strip()}"
            )

    def verify_remote(self, remote_db: RemotePath, expected_sha256: str) -> bool:
        if remote_db.host is None:
            return _local_sha256(pathlib.Path(remote_db.path)) == expected_sha256
        # ``sha256sum -c`` on a sibling file would re-parse the
        # manifest; we just compute and string-compare to avoid
        # locale issues on the remote side.
        res = self._ssh(remote_db.host, f"sha256sum {_shq(str(remote_db.path))}")
        if res.returncode != 0:
            raise BackupError(
                f"remote sha256sum failed: {res.stderr.strip()}"
            )
        remote_hex = res.stdout.strip().split()[0] if res.stdout.strip() else ""
        return remote_hex == expected_sha256

    def list_remote(self, remote_root: RemotePath) -> list[str]:
        if remote_root.host is None:
            root = pathlib.Path(remote_root.path)
            if not root.exists():
                return []
            return sorted(
                str(p.relative_to(root))
                for p in root.rglob("alice-state-db-*.db")
            )
        cmd = (
            f"find {_shq(str(remote_root.path))} -type f "
            f"-name 'alice-state-db-*.db' -printf '%P\\n' 2>/dev/null || true"
        )
        res = self._ssh(remote_root.host, cmd)
        if res.returncode != 0:
            raise BackupError(
                f"remote list failed: {res.stderr.strip()}"
            )
        return sorted(line for line in res.stdout.splitlines() if line)

    def delete_remote(self, remote: RemotePath) -> None:
        if remote.host is None:
            path = pathlib.Path(remote.path)
            if path.exists():
                path.unlink()
            sha = path.with_suffix(path.suffix + ".sha256")
            if sha.exists():
                sha.unlink()
            return
        # Delete both the .db and the .sha256 sibling in one shot.
        p = _shq(str(remote.path))
        res = self._ssh(remote.host, f"rm -f {p} {p}.sha256")
        if res.returncode != 0:
            raise BackupError(
                f"remote rm failed: {res.stderr.strip()}"
            )


class LocalTransport:
    """Treat a local directory as the NAS. Used by tests."""

    def upload(self, local: pathlib.Path, remote: RemotePath) -> None:
        assert remote.is_local, "LocalTransport expects local RemotePath"
        dst = pathlib.Path(remote.path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, dst)

    def verify_remote(self, remote_db: RemotePath, expected_sha256: str) -> bool:
        assert remote_db.is_local
        return _local_sha256(pathlib.Path(remote_db.path)) == expected_sha256

    def list_remote(self, remote_root: RemotePath) -> list[str]:
        assert remote_root.is_local
        root = pathlib.Path(remote_root.path)
        if not root.exists():
            return []
        return sorted(
            str(p.relative_to(root))
            for p in root.rglob("alice-state-db-*.db")
        )

    def delete_remote(self, remote: RemotePath) -> None:
        assert remote.is_local
        path = pathlib.Path(remote.path)
        if path.exists():
            path.unlink()
        sha = path.with_suffix(path.suffix + ".sha256")
        if sha.exists():
            sha.unlink()


def _shq(s: str) -> str:
    """POSIX shell quote. Not using shlex.quote on ``str`` because
    we're building commands run on a remote shell, not spawning
    locally. Same behaviour, explicit intent."""
    if not s:
        return "''"
    if re.fullmatch(r"[A-Za-z0-9._/@=:+-]+", s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ---------------------------------------------------------------------------
# Exceptions


class BackupError(RuntimeError):
    """Raised on any step that would leave a snapshot in doubt."""


# ---------------------------------------------------------------------------
# Core primitives


def snapshot_db(source: pathlib.Path, dest: pathlib.Path) -> None:
    """Emit a consistent copy of ``source`` to ``dest`` via VACUUM INTO.

    ``dest`` must not exist (SQLite's VACUUM INTO refuses to overwrite).
    """
    if not source.exists():
        raise BackupError(f"source DB not found: {source}")
    if dest.exists():
        raise BackupError(f"destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # ``uri=True`` + ``mode=ro`` opens read-only — the VACUUM INTO
    # still works because SQLite internally opens a temp write
    # session for the output file. A readonly open on the source
    # means we can't accidentally take a schema-change path.
    uri = f"file:{source}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute(f"VACUUM INTO '{str(dest).replace(chr(39), chr(39) * 2)}'")
    finally:
        conn.close()


def compute_sha256(path: pathlib.Path) -> str:
    return _local_sha256(path)


def _local_sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sha256_manifest(db_path: pathlib.Path, sha256: str) -> pathlib.Path:
    """Write a ``<db>.sha256`` manifest in shasum-compatible format."""
    manifest = db_path.with_suffix(db_path.suffix + ".sha256")
    manifest.write_text(f"{sha256}  {db_path.name}\n", encoding="ascii")
    return manifest


# ---------------------------------------------------------------------------
# Remote path layout


def remote_snapshot_path(
    root: RemotePath,
    now: dt.datetime,
) -> tuple[RemotePath, str]:
    """``root`` + ``YYYY/MM/DD/alice-state-db-YYYYMMDD-HHMM.db``.

    Returns ``(remote_path, filename)``. The filename is returned as
    a convenience for manifest writing.
    """
    stamp = now.strftime("%Y%m%d-%H%M")
    filename = f"alice-state-db-{stamp}.db"
    sub = root.child(
        f"{now:%Y}",
        f"{now:%m}",
        f"{now:%d}",
        filename,
    )
    return sub, filename


# ---------------------------------------------------------------------------
# Retention


@dataclasses.dataclass(frozen=True)
class ParsedSnapshot:
    """A snapshot file name parsed to its timestamp + relative path."""

    relpath: str
    ts: dt.datetime


def parse_snapshots(entries: Iterable[str]) -> list[ParsedSnapshot]:
    """Parse remote-list output into snapshots, discarding non-matches."""
    out: list[ParsedSnapshot] = []
    for rel in entries:
        name = pathlib.PurePosixPath(rel).name
        m = _SNAPSHOT_RE.match(name)
        if not m:
            continue
        try:
            ts = dt.datetime(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
                int(m.group(5)),
            )
        except ValueError:
            continue
        out.append(ParsedSnapshot(relpath=rel, ts=ts))
    out.sort(key=lambda s: s.ts)
    return out


def pick_retained(
    snapshots: Sequence[ParsedSnapshot],
    retention: dict[str, int],
    *,
    now: dt.datetime,
) -> set[str]:
    """Return the set of ``relpath`` values to keep.

    Policy:

    * Daily bucket: for each UTC date represented in ``snapshots``,
      pick the latest snapshot on that date. Keep the ``daily`` most
      recent of those.
    * Weekly bucket: for each ISO year-week represented, pick the
      latest snapshot that week. Keep the ``weekly`` most recent.
    * Monthly bucket: for each ``YYYY-MM`` represented, pick the
      latest snapshot that month. Keep the ``monthly`` most recent.

    Union of the three sets is the retained set. Anything outside
    is prunable. We also always retain the single newest snapshot
    unconditionally — a brand-new deploy with retention-0 should
    still keep today's last backup.
    """
    retained: set[str] = set()
    if not snapshots:
        return retained

    # Newest snapshot wins — always keep.
    retained.add(snapshots[-1].relpath)

    def _latest_per(key_fn) -> list[ParsedSnapshot]:
        latest: dict[object, ParsedSnapshot] = {}
        for snap in snapshots:
            k = key_fn(snap)
            cur = latest.get(k)
            if cur is None or snap.ts > cur.ts:
                latest[k] = snap
        # Sorted newest-first so we can slice.
        return sorted(latest.values(), key=lambda s: s.ts, reverse=True)

    daily_count = retention.get("daily", 0)
    if daily_count:
        for snap in _latest_per(lambda s: s.ts.date())[:daily_count]:
            retained.add(snap.relpath)

    weekly_count = retention.get("weekly", 0)
    if weekly_count:
        for snap in _latest_per(lambda s: s.ts.isocalendar()[:2])[:weekly_count]:
            retained.add(snap.relpath)

    monthly_count = retention.get("monthly", 0)
    if monthly_count:
        for snap in _latest_per(lambda s: (s.ts.year, s.ts.month))[:monthly_count]:
            retained.add(snap.relpath)

    hourly_count = retention.get("hourly", 0)
    if hourly_count:
        for snap in list(reversed(snapshots))[:hourly_count]:
            retained.add(snap.relpath)

    return retained


def plan_rotation(
    snapshots: Sequence[ParsedSnapshot],
    retention: dict[str, int],
    *,
    now: dt.datetime,
) -> list[ParsedSnapshot]:
    """Return the list of snapshots to delete."""
    keep = pick_retained(snapshots, retention, now=now)
    return [s for s in snapshots if s.relpath not in keep]


# ---------------------------------------------------------------------------
# Backup note


def append_backup_note(
    mind: pathlib.Path,
    entry: dict,
    *,
    now: dt.datetime,
) -> pathlib.Path:
    """Append a JSON line to today's backup-note file.

    The memory worker scans ``inner/notes/*.jsonl`` as part of its
    drain pass; writing one file per date keeps the notes bounded.
    """
    notes_dir = mind / "inner" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_file = notes_dir / f"alice-state-db-backup-{now:%Y-%m-%d}.jsonl"
    with note_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return note_file


# ---------------------------------------------------------------------------
# Orchestration


@dataclasses.dataclass
class BackupResult:
    status: str  # "ok" | "verify_failed" | "upload_failed" | "snapshot_failed"
    snapshot_name: str | None
    remote_path: str | None
    size: int | None
    sha256: str | None
    deleted: list[str]
    error: str | None = None


def run_backup(
    *,
    source: pathlib.Path = DEFAULT_SOURCE,
    dest: str = DEFAULT_DEST,
    mind: pathlib.Path | None = None,
    retention: dict[str, int] | None = None,
    transport: Transport | None = None,
    now: dt.datetime | None = None,
    note_sink: bool = True,
) -> BackupResult:
    """Produce a snapshot, upload it, verify it, rotate older copies.

    Any failure before verify is terminal — we do NOT rotate. On a
    verify failure we keep the (presumably corrupt) remote file in
    place so a human can inspect, and still do not rotate.
    """
    if retention is None:
        env = os.environ.get("ALICE_STATE_DB_BACKUP_RETENTION")
        retention = json.loads(env) if env else dict(DEFAULT_RETENTION)
    if now is None:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    remote_root = RemotePath.parse(dest)
    if transport is None:
        transport = LocalTransport() if remote_root.is_local else SSHRsyncTransport()
    remote_file, filename = remote_snapshot_path(remote_root, now)

    with tempfile.TemporaryDirectory(prefix="alice-state-db-backup-") as tmp:
        local_db = pathlib.Path(tmp) / filename
        try:
            snapshot_db(source, local_db)
        except (BackupError, sqlite3.Error) as exc:
            result = BackupResult(
                status="snapshot_failed",
                snapshot_name=filename,
                remote_path=None,
                size=None,
                sha256=None,
                deleted=[],
                error=str(exc),
            )
            if mind is not None and note_sink:
                _emit_note(mind, result, now)
            return result

        sha = compute_sha256(local_db)
        manifest = write_sha256_manifest(local_db, sha)
        size = local_db.stat().st_size

        remote_manifest = remote_file.with_suffix_added(".sha256")
        try:
            transport.upload(local_db, remote_file)
            transport.upload(manifest, remote_manifest)
        except BackupError as exc:
            result = BackupResult(
                status="upload_failed",
                snapshot_name=filename,
                remote_path=remote_file.full,
                size=size,
                sha256=sha,
                deleted=[],
                error=str(exc),
            )
            if mind is not None and note_sink:
                _emit_note(mind, result, now)
            return result

        try:
            ok = transport.verify_remote(remote_file, sha)
        except BackupError as exc:
            result = BackupResult(
                status="verify_failed",
                snapshot_name=filename,
                remote_path=remote_file.full,
                size=size,
                sha256=sha,
                deleted=[],
                error=str(exc),
            )
            if mind is not None and note_sink:
                _emit_note(mind, result, now)
            return result

        if not ok:
            result = BackupResult(
                status="verify_failed",
                snapshot_name=filename,
                remote_path=remote_file.full,
                size=size,
                sha256=sha,
                deleted=[],
                error="sha256 mismatch on remote after upload",
            )
            if mind is not None and note_sink:
                _emit_note(mind, result, now)
            return result

    # Rotation only runs when the fresh snapshot verified. That
    # guarantees we never prune the last valid backup because a
    # partial upload looked newer.
    entries = transport.list_remote(remote_root)
    snapshots = parse_snapshots(entries)
    to_delete = plan_rotation(snapshots, retention, now=now)
    deleted: list[str] = []
    for snap in to_delete:
        try:
            transport.delete_remote(remote_root.child(snap.relpath))
            deleted.append(snap.relpath)
        except BackupError as exc:
            LOG.warning("rotation delete failed for %s: %s", snap.relpath, exc)

    result = BackupResult(
        status="ok",
        snapshot_name=filename,
        remote_path=remote_file.full,
        size=size,
        sha256=sha,
        deleted=deleted,
    )
    if mind is not None and note_sink:
        _emit_note(mind, result, now)
    return result


def _emit_note(
    mind: pathlib.Path,
    result: BackupResult,
    now: dt.datetime,
) -> None:
    entry = {
        "ts": now.replace(microsecond=0).isoformat() + "Z",
        "status": result.status,
        "snapshot": result.snapshot_name,
        "remote": result.remote_path,
        "size": result.size,
        "sha256": result.sha256,
        "deleted_count": len(result.deleted),
        "error": result.error,
    }
    try:
        append_backup_note(mind, entry, now=now)
    except OSError as exc:
        LOG.warning("backup note write failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI


def _default_mind() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "ALICE_MIND", "/home/alice/alice-mind"
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alice-state-db-backup",
        description=(
            "Snapshot alice-state.db, push it to NAS, verify, rotate."
        ),
    )
    parser.add_argument(
        "--source",
        type=pathlib.Path,
        default=DEFAULT_SOURCE,
        help=f"live DB path (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--dest",
        default=DEFAULT_DEST,
        help=(
            "NAS target as user@host:/abs/path or /local/abs/path "
            f"(default: {DEFAULT_DEST})"
        ),
    )
    parser.add_argument(
        "--mind",
        type=pathlib.Path,
        default=_default_mind(),
        help="alice-mind root for backup-note emission",
    )
    parser.add_argument(
        "--retention",
        default=None,
        help=(
            "retention policy as JSON "
            f"(default: {json.dumps(DEFAULT_RETENTION)})"
        ),
    )
    parser.add_argument(
        "--no-note",
        action="store_true",
        help="skip the inner/notes/ backup-note write",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG-level logging",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    retention = json.loads(args.retention) if args.retention else None

    result = run_backup(
        source=args.source,
        dest=args.dest,
        mind=args.mind,
        retention=retention,
        note_sink=not args.no_note,
    )

    print(json.dumps(dataclasses.asdict(result), sort_keys=True))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
