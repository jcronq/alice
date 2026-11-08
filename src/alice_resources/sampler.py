"""Resource sampler — CPU%/RSS per process + Claude token usage per turn.

Runs as a long-lived loop under s6 (``alice-resource-sampler``). Two
cadences:

* Process sampling (``PS_INTERVAL``, default 30 s) — parse ``ps -eo``
  output, keep rows matching :data:`PROCESS_PATTERNS`, correlate each
  row's cwd / argv against ``workers.worktree_path`` to attach a
  ``bg_id`` where possible, append to ``resource_samples``.
* Token sampling (``TOKEN_INTERVAL``, default 60 s) — walk
  ``~/.claude/projects/**/<session>.jsonl`` for files whose mtime is
  within ``TOKEN_SCAN_WINDOW_SEC`` (default 10 min), tail the new
  assistant turns, upsert into ``token_usage`` keyed on
  ``(session_path, turn_index)``.

The loop reconnects to SQLite on every iteration so a dropped connection
(DB rotation, FS remount) recovers on the next tick. Any per-iteration
exception is logged to stderr but never crashes the loop — a sampler
that stops silently is far worse than one that logs noise.

Design constraints:

* No long-lived DB transactions — one short write per tick.
* No FS watches / inotify — simpler to reason about, and the cadences
  are coarse enough that polling is cheap.
* Process matching is substring-based rather than regex-anchored; false
  positives on command lines are tolerable because the Resources page
  dedupes at the chart layer.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

log = logging.getLogger("alice_resources.sampler")


# ─── tunables ──────────────────────────────────────────────────────

PS_INTERVAL = int(os.environ.get("ALICE_RESOURCE_PS_INTERVAL", "30"))
TOKEN_INTERVAL = int(os.environ.get("ALICE_RESOURCE_TOKEN_INTERVAL", "60"))
TOKEN_SCAN_WINDOW_SEC = int(
    os.environ.get("ALICE_RESOURCE_TOKEN_WINDOW", str(10 * 60))
)

#: Substrings that key the ``cmd_tag`` column. First match wins so
#: ordering matters — the more specific labels go first. ``claude`` is
#: last because it's the broadest match.
PROCESS_PATTERNS: Sequence[tuple[str, str]] = (
    ("alice_thinking", "thinking"),
    ("thinking_wake", "thinking"),
    ("alice_memory_worker", "memory_worker"),
    ("memory_worker_wake", "memory_worker"),
    ("alice_speaking", "speaking"),
    ("speaking.daemon", "speaking"),
    ("cozylobe", "cozylobe"),
    ("alice-cue-runner", "cue_runner"),
    ("node", "node"),
    ("claude", "claude"),
)


# ─── row shapes ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PsRow:
    """One parsed ``ps -eo`` row that matched a pattern.

    ``cmd_tag`` is the label assigned by :func:`tag_for_cmd`; ``args``
    is the full argv string so the correlator can grep for worktree
    paths.
    """

    pid: int
    ppid: int
    cpu_pct: float
    rss_kb: int
    comm: str
    args: str
    cmd_tag: str


@dataclass(frozen=True)
class TokenRow:
    """One parsed assistant turn from a transcript JSONL file."""

    turn_index: int
    model: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    ts: Optional[str]  # transcript's own ISO timestamp if present
    cwd: Optional[str]  # transcript-side cwd, used for bg_id correlation


# ─── pure parsers ──────────────────────────────────────────────────


def tag_for_cmd(args: str) -> Optional[str]:
    """Return the first matching cmd_tag, or ``None`` when none match.

    Case-sensitive substring scan — ``ps`` output keeps original casing
    for argv so the patterns can be precise.
    """
    for needle, tag in PROCESS_PATTERNS:
        if needle in args:
            return tag
    return None


def parse_ps_output(text: str) -> list[PsRow]:
    """Parse ``ps -eo pid,ppid,%cpu,rss,comm,args`` output.

    Rows that don't carry a known ``cmd_tag`` are dropped. The parser
    is intentionally tolerant: short lines, missing columns, non-ASCII
    argv all just get skipped.
    """
    out: list[PsRow] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        # Header row — ps prints "PID PPID %CPU RSS COMMAND COMMAND".
        if line.lstrip().startswith("PID "):
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            cpu = float(parts[2])
            rss = int(parts[3])
        except (TypeError, ValueError):
            continue
        comm = parts[4]
        args = parts[5]
        tag = tag_for_cmd(args)
        if tag is None:
            # Also try matching on comm (the short form) — handy for
            # processes whose argv is stripped (kernel threads etc.).
            tag = tag_for_cmd(comm)
        if tag is None:
            continue
        out.append(
            PsRow(
                pid=pid, ppid=ppid, cpu_pct=cpu, rss_kb=rss,
                comm=comm, args=args, cmd_tag=tag,
            )
        )
    return out


def parse_transcript_turns(
    jsonl_text: str, *, start_at: int = 0,
) -> list[TokenRow]:
    """Parse Claude Code transcript JSONL into token-usage rows.

    One row per ``type:"assistant"`` line that carries ``message.usage``.
    ``turn_index`` is the 0-based line number so the UNIQUE constraint
    on ``(session_path, turn_index)`` keeps re-scans idempotent.

    ``start_at`` lets a later scan skip lines that have already been
    processed — the caller can pass the max ``turn_index`` seen so far.
    """
    out: list[TokenRow] = []
    for idx, raw in enumerate(jsonl_text.splitlines()):
        if idx < start_at:
            continue
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        usage = msg.get("usage") or {}
        if not usage:
            continue
        # Reject synthetic / error turns — they have the model set to
        # ``<synthetic>`` with all-zero token counts. Harmless but
        # noisy; skip them so the table stays focused on real spend.
        model = msg.get("model")
        input_t = int(usage.get("input_tokens", 0) or 0)
        output_t = int(usage.get("output_tokens", 0) or 0)
        cache_cr = int(usage.get("cache_creation_input_tokens", 0) or 0)
        cache_rd = int(usage.get("cache_read_input_tokens", 0) or 0)
        if model == "<synthetic>" and (
            input_t == 0 and output_t == 0 and cache_cr == 0 and cache_rd == 0
        ):
            continue
        out.append(
            TokenRow(
                turn_index=idx,
                model=model,
                input_tokens=input_t,
                output_tokens=output_t,
                cache_creation_tokens=cache_cr,
                cache_read_tokens=cache_rd,
                ts=obj.get("timestamp"),
                cwd=obj.get("cwd"),
            )
        )
    return out


# ─── correlation ───────────────────────────────────────────────────


def bg_id_for_args(
    args: str, worker_paths: Mapping[str, str],
) -> Optional[str]:
    """Return the bg_id whose worktree_path appears in ``args``.

    ``worker_paths`` is a ``{bg_id: worktree_path}`` map. Longest path
    wins so a nested worktree (e.g. ``/a/b`` vs ``/a``) matches the
    deeper one — though in practice worktree_paths don't nest.
    """
    best: Optional[tuple[int, str]] = None
    for bg_id, path in worker_paths.items():
        if not path:
            continue
        if path in args:
            if best is None or len(path) > best[0]:
                best = (len(path), bg_id)
    return best[1] if best else None


def bg_id_for_cwd(
    cwd: Optional[str], worker_paths: Mapping[str, str],
) -> Optional[str]:
    """Correlate a transcript's ``cwd`` field to a bg_id via substring.

    Same rule as :func:`bg_id_for_args` — the transcript's cwd is often
    the worktree root (or a path under it), so longest-match wins.
    """
    if not cwd:
        return None
    best: Optional[tuple[int, str]] = None
    for bg_id, path in worker_paths.items():
        if not path:
            continue
        if cwd == path or cwd.startswith(path + "/") or path in cwd:
            if best is None or len(path) > best[0]:
                best = (len(path), bg_id)
    return best[1] if best else None


# ─── DB helpers ────────────────────────────────────────────────────


def _default_db_path() -> Path:
    return Path(
        os.environ.get(
            "ALICE_STATE_DB",
            str(
                Path(os.environ.get("ALICE_MIND", str(Path.home() / "alice-mind")))
                / "inner" / "state" / "alice-state.db"
            ),
        )
    )


def _default_projects_dir() -> Path:
    # Inside the alice container the host's ~/.claude is mounted RO at
    # ``/host-claude``. Outside the container we fall back to the
    # user's home. The env override is for tests.
    override = os.environ.get("ALICE_CLAUDE_PROJECTS_DIR")
    if override:
        return Path(override)
    candidate = Path("/host-claude/projects")
    if candidate.is_dir():
        return candidate
    return Path.home() / ".claude" / "projects"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    # The sampler is a writer — ensure WAL + FK semantics match the
    # rest of the state DB.
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def _load_worker_paths(conn: sqlite3.Connection) -> dict[str, str]:
    """``{bg_id: worktree_path}`` for every worker that recorded one."""
    rows = conn.execute(
        "SELECT bg_id, worktree_path FROM workers "
        "WHERE worktree_path IS NOT NULL AND worktree_path != ''"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _max_turn_index(conn: sqlite3.Connection, session_path: str) -> int:
    """Highest turn_index already recorded for ``session_path``.

    Returns ``-1`` when the session has no rows yet so the caller can
    scan from line 0.
    """
    row = conn.execute(
        "SELECT MAX(turn_index) FROM token_usage WHERE session_path = ?",
        (session_path,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else -1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─── sample ticks ──────────────────────────────────────────────────


def sample_processes(
    conn: sqlite3.Connection,
    *,
    ps_runner=None,
    now_iso: Optional[str] = None,
) -> int:
    """One process-sampling tick. Returns rows inserted."""
    ts = now_iso or _now_iso()
    runner = ps_runner or _default_ps_runner
    try:
        text = runner()
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ps sampler: runner failed: %s", exc)
        return 0
    rows = parse_ps_output(text)
    if not rows:
        return 0
    worker_paths = _load_worker_paths(conn)
    payload = []
    for r in rows:
        bg = bg_id_for_args(r.args, worker_paths) if r.cmd_tag == "claude" else None
        payload.append(
            (ts, bg, r.pid, r.cmd_tag, r.cpu_pct, r.rss_kb / 1024.0, "ps")
        )
    conn.executemany(
        "INSERT INTO resource_samples "
        "(ts, bg_id, pid, cmd_tag, cpu_pct, rss_mb, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    conn.commit()
    return len(payload)


def _default_ps_runner() -> str:
    proc = subprocess.run(
        ["ps", "-eo", "pid,ppid,%cpu,rss,comm,args"],
        capture_output=True, text=True, timeout=10,
    )
    return proc.stdout


def sample_tokens(
    conn: sqlite3.Connection,
    *,
    projects_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> int:
    """One token-sampling tick. Returns rows inserted."""
    base = projects_dir or _default_projects_dir()
    if not base.is_dir():
        return 0
    cutoff = (now or time.time()) - TOKEN_SCAN_WINDOW_SEC
    worker_paths = _load_worker_paths(conn)
    inserted = 0
    for path in base.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            log.warning("token sampler: read %s failed: %s", path, exc)
            continue
        session_path = str(path)
        start_at = _max_turn_index(conn, session_path) + 1
        try:
            turns = parse_transcript_turns(text, start_at=start_at)
        except Exception as exc:  # defensive — malformed line somehow
            log.warning("token sampler: parse %s failed: %s", path, exc)
            continue
        if not turns:
            continue
        payload = []
        for t in turns:
            bg = bg_id_for_cwd(t.cwd, worker_paths)
            payload.append(
                (
                    t.ts or _now_iso(),
                    bg,
                    session_path,
                    t.turn_index,
                    t.model,
                    t.input_tokens,
                    t.output_tokens,
                    t.cache_creation_tokens,
                    t.cache_read_tokens,
                )
            )
        # UNIQUE(session_path, turn_index) makes this idempotent — a
        # torn write or a race between the parser's ``start_at`` and
        # the file growing just gets ignored.
        conn.executemany(
            "INSERT OR IGNORE INTO token_usage "
            "(ts, bg_id, session_path, turn_index, model, "
            "input_tokens, output_tokens, "
            "cache_creation_tokens, cache_read_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        conn.commit()
        inserted += len(payload)
    return inserted


# ─── loop entry point ──────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:  # noqa: ARG001
    """Long-lived sampler loop. Returns only on SIGTERM / fatal error."""
    logging.basicConfig(
        level=os.environ.get("ALICE_RESOURCE_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    # Lazy import so test envs don't need the full state_db package on
    # path just to parse a ps fixture.
    from alice_state_db.schema import apply_schema

    db_path = _default_db_path()
    projects_dir = _default_projects_dir()
    log.info("alice-resource-sampler: db=%s projects=%s", db_path, projects_dir)

    last_token_tick = 0.0
    while True:
        try:
            conn = _connect(db_path)
            try:
                apply_schema(conn)
                n_ps = sample_processes(conn)
                now = time.time()
                n_tok = 0
                if now - last_token_tick >= TOKEN_INTERVAL:
                    n_tok = sample_tokens(conn, projects_dir=projects_dir, now=now)
                    last_token_tick = now
                log.debug(
                    "tick: ps=%d tokens=%d db=%s", n_ps, n_tok, db_path
                )
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — loop MUST keep running
            log.exception("sampler tick failed: %s", exc)
        time.sleep(PS_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
