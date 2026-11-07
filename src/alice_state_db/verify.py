"""Monthly restore-sanity check for ``alice-state.db`` backups.

Runs independently of the backup and restore paths (so a bug in
either doesn't mask the other). Procedure:

1. Download the newest snapshot (and its ``.sha256`` manifest) from
   NAS to a tmp dir.
2. Verify sha256 matches the manifest. Any mismatch is a hard fail.
3. ``PRAGMA integrity_check`` against the fetched copy; any result
   other than ``"ok"`` is a hard fail.
4. Row-count delta against the live DB on a list of important
   tables (default: ``items``, ``state_machine``, ``transitions``).
   If the delta exceeds 5% for any table, emit a
   ``conflict-candidate`` note to ``inner/notes/`` so thinking can
   investigate. A large delta is not itself a backup failure — the
   live DB just drifted since the backup — but the delta is worth
   surfacing so a human can decide.

Any hard fail triggers a signal/notification via an injected
callback (default: no-op logging). Speaking-side wiring is the
caller's job; this module stays pure so it can be unit-tested
without network / MCP.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import pathlib
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Sequence

from alice_state_db.backup import (
    BackupError,
    RemotePath,
    Transport,
)
from alice_state_db.restore import (
    DEFAULT_TARGET,
    _default_transport,
    find_latest_valid_snapshot,
    integrity_check,
)


LOG = logging.getLogger("alice_state_db.verify")


DEFAULT_IMPORTANT_TABLES = ("items", "state_machine", "transitions")
DEFAULT_DELTA_THRESHOLD = 0.05  # 5%

NotifyFn = Callable[[str], None]


@dataclasses.dataclass
class VerifyResult:
    status: str  # "ok" | "no_snapshot" | "integrity_failed" | "sha_failed"
    snapshot: str | None
    sha256: str | None
    integrity: str | None
    row_deltas: dict[str, dict[str, int | float]]
    conflict_note_path: str | None
    error: str | None = None


def _row_count(conn: sqlite3.Connection, table: str) -> int | None:
    """Return COUNT(*) for ``table``, or ``None`` if the table isn't
    present (e.g. a pre-SM-v2 backup against today's live DB).
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?;",
        (table,),
    ).fetchone()
    if not row:
        return None
    return conn.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]


def compute_row_deltas(
    backup_db: pathlib.Path,
    live_db: pathlib.Path,
    tables: Sequence[str] = DEFAULT_IMPORTANT_TABLES,
) -> dict[str, dict[str, int | float]]:
    """For each table, return ``{"backup": n, "live": n, "delta": frac}``.

    ``delta`` is ``abs(live - backup) / max(backup, 1)``. Tables
    missing on either side are reported with ``None`` values and a
    sentinel ``delta`` of ``-1.0``.
    """
    out: dict[str, dict[str, int | float]] = {}
    backup_conn = sqlite3.connect(f"file:{backup_db}?mode=ro", uri=True)
    live_conn: sqlite3.Connection | None = None
    if live_db.exists():
        live_conn = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
    try:
        for table in tables:
            b = _row_count(backup_conn, table)
            live = _row_count(live_conn, table) if live_conn is not None else None
            if b is None or live is None:
                out[table] = {
                    "backup": -1 if b is None else b,
                    "live": -1 if live is None else live,
                    "delta": -1.0,
                }
                continue
            delta = abs(live - b) / max(b, 1)
            out[table] = {"backup": b, "live": live, "delta": delta}
    finally:
        backup_conn.close()
        if live_conn is not None:
            live_conn.close()
    return out


def write_conflict_note(
    mind: pathlib.Path,
    body: str,
    *,
    now: dt.datetime,
) -> pathlib.Path:
    """Drop a ``conflict-candidate`` note the memory worker will drain."""
    notes_dir = mind / "inner" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    note = notes_dir / f"conflict-candidate-state-db-verify-{stamp}.md"
    frontmatter = (
        "---\n"
        f"created: {now.replace(microsecond=0).isoformat()}Z\n"
        "tag: conflict-candidate\n"
        "source: alice_state_db.verify\n"
        "---\n\n"
    )
    note.write_text(frontmatter + body, encoding="utf-8")
    return note


def run_verify(
    *,
    dest: str,
    live_db: pathlib.Path = DEFAULT_TARGET,
    mind: pathlib.Path | None = None,
    tables: Sequence[str] = DEFAULT_IMPORTANT_TABLES,
    delta_threshold: float = DEFAULT_DELTA_THRESHOLD,
    transport: Transport | None = None,
    workdir: pathlib.Path | None = None,
    now: dt.datetime | None = None,
    notify: NotifyFn | None = None,
) -> VerifyResult:
    if now is None:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if transport is None:
        transport = _default_transport(dest)
    remote_root = RemotePath.parse(dest)

    cleanup: tempfile.TemporaryDirectory[str] | None = None
    if workdir is None:
        cleanup = tempfile.TemporaryDirectory(
            prefix="alice-state-db-verify-"
        )
        workdir = pathlib.Path(cleanup.name)

    try:
        found = find_latest_valid_snapshot(transport, remote_root, workdir)
        if found is None:
            result = VerifyResult(
                status="no_snapshot",
                snapshot=None,
                sha256=None,
                integrity=None,
                row_deltas={},
                conflict_note_path=None,
                error="no snapshot on NAS verified sha256",
            )
            if notify is not None:
                notify(f"state-db verify: {result.error}")
            return result

        snap, local_db, sha = found
        try:
            integrity = integrity_check(local_db)
        except BackupError as exc:
            result = VerifyResult(
                status="integrity_failed",
                snapshot=snap.relpath,
                sha256=sha,
                integrity=None,
                row_deltas={},
                conflict_note_path=None,
                error=str(exc),
            )
            if notify is not None:
                notify(f"state-db verify: {result.error}")
            return result

        if integrity != "ok":
            result = VerifyResult(
                status="integrity_failed",
                snapshot=snap.relpath,
                sha256=sha,
                integrity=integrity,
                row_deltas={},
                conflict_note_path=None,
                error=f"integrity_check returned {integrity!r}",
            )
            if notify is not None:
                notify(f"state-db verify: {result.error}")
            return result

        deltas = compute_row_deltas(local_db, live_db, tables=tables)
        offenders = {
            t: d for t, d in deltas.items()
            if isinstance(d.get("delta"), (int, float)) and d["delta"] > delta_threshold
        }
        conflict_note_path: str | None = None
        if offenders and mind is not None:
            body_lines = [
                f"# state-db row-count delta exceeded {delta_threshold:.0%}",
                "",
                f"Snapshot: `{snap.relpath}`",
                f"sha256: `{sha}`",
                "",
                "| Table | Backup | Live | Delta |",
                "| --- | ---: | ---: | ---: |",
            ]
            for table, d in sorted(offenders.items()):
                body_lines.append(
                    f"| `{table}` | {d['backup']} | {d['live']} | "
                    f"{d['delta']:.2%} |"
                )
            body_lines.append("")
            body_lines.append(
                "This is a drift indicator, not necessarily a failure. "
                "The live DB has moved since the backup; thinking should "
                "sanity-check that the direction and magnitude make sense."
            )
            note = write_conflict_note(
                mind, "\n".join(body_lines), now=now
            )
            conflict_note_path = str(note)

        return VerifyResult(
            status="ok",
            snapshot=snap.relpath,
            sha256=sha,
            integrity=integrity,
            row_deltas=deltas,
            conflict_note_path=conflict_note_path,
        )
    finally:
        if cleanup is not None:
            cleanup.cleanup()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alice-state-db-verify",
        description=(
            "Monthly sanity check: fetch latest NAS snapshot, verify "
            "sha256 + integrity_check, compare row counts to live DB."
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
        "--live",
        type=pathlib.Path,
        default=DEFAULT_TARGET,
        help="live DB path for row-count comparison",
    )
    parser.add_argument(
        "--mind",
        type=pathlib.Path,
        default=pathlib.Path(
            os.environ.get("ALICE_MIND", "/home/alice/alice-mind")
        ),
        help="alice-mind root for conflict-candidate note emission",
    )
    parser.add_argument(
        "--tables",
        default=",".join(DEFAULT_IMPORTANT_TABLES),
        help="comma-separated list of tables to compare",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_DELTA_THRESHOLD,
        help="row-count delta threshold (fraction, default 0.05)",
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

    result = run_verify(
        dest=args.dest,
        live_db=args.live,
        mind=args.mind,
        tables=tuple(t.strip() for t in args.tables.split(",") if t.strip()),
        delta_threshold=args.threshold,
    )
    print(json.dumps(dataclasses.asdict(result), sort_keys=True))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
