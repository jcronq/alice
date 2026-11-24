"""Running now — SM-task-driven render of what sub-agents are doing right now.

Design: ``cortex-memory/designs/2026-11-24-subagent-driven-running-now.md``.

PR #2 (this file) replaces the pre-existing worktree+ps+build-log scraper with
a deterministic join over three inputs that the hook layer now owns:

1. **SM store** — ``task --json list --status building`` emits one JSON object
   per line (NDJSON). The CLI shim lives at ``/home/alice/alice/bin/task`` and
   execs ``python -m alice_forge.task_cli`` under the hood. A missing / failed
   CLI falls through to a direct read of ``inner/tasks/task-*/task.yaml``.
2. **Start log** — ``/state/worker/alice-task-dispatch.jsonl`` and its
   ``.jsonl.1`` rotation. Written by the PreToolUse Agent hook (in-process
   ``Agent(...)`` dispatches) and the new steerable-dispatch helper
   (``src/alice_speaking/internal/steerable_dispatch.py``, subprocess spawns).
3. **Stop log** — ``/state/worker/alice-subagent-stop.jsonl`` and its
   rotation. Written by ``subagentstop-emit.py`` (SubagentStop hook), the
   session-end reaper, and the periodic orphan reaper.

Join:
    - open_dispatches = starts whose ``tool_call_id`` is not in stops
    - group by ``task_id`` → ``agents`` list per task
    - task is ``active`` if ≥1 open dispatch, ``idle`` otherwise

Idle ``building`` tasks are rendered muted so Jason can see tasks that are
stuck in-state without an open sub-agent — the "worktrees never clean up"
complaint from the 2026-11-24 morning verdict, in a different form.

Every function is deterministic + injectable so the tests don't call live
tools; see ``tests/test_status_site_running_now.py`` for the full matrix.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from status_site.sources.common import staleness_footer

SOURCE_NAME = "subagent-events"

# Default paths — overridable by env / direct kwargs so tests can point at
# tmp_path fixtures. The status-site container pins
# ``ALICE_MIND=/srv/mind`` so ``_tasks_dir`` resolves inside the bind-mount.
_DEFAULT_START_LOG = Path("/state/worker/alice-task-dispatch.jsonl")
_DEFAULT_STOP_LOG = Path("/state/worker/alice-subagent-stop.jsonl")
# Task CLI resolution — host path first, then PATH. ``_default_task_bin`` can
# be overridden via the ``TASK_BIN`` env var (the shim at ``bin/task`` reads it
# too), which the test suite uses to point at a stub.
_DEFAULT_TASK_BIN = "/home/alice/alice/bin/task"

SubprocessRunner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=10)


# ─── Event-log readers ──────────────────────────────────────────────


def _read_jsonl_pair(primary: Path) -> list[dict]:
    """Read ``primary`` and ``primary.name + ".1"`` rotation, newest first.

    Returns rows from both files flattened into a single list, in file order
    (older file first, so the newest rows end up at the tail — matching the
    append-only contract). Each file is tolerant of blank lines and
    malformed JSON (skip-and-continue so one bad line can't poison the
    whole section).
    """
    files: list[Path] = []
    prior = primary.with_suffix(primary.suffix + ".1")
    if prior.exists():
        files.append(prior)
    if primary.exists():
        files.append(primary)

    out: list[dict] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def read_start_log(path: Path | None = None) -> list[dict]:
    return _read_jsonl_pair(path or _DEFAULT_START_LOG)


def read_stop_log(path: Path | None = None) -> list[dict]:
    return _read_jsonl_pair(path or _DEFAULT_STOP_LOG)


# ─── SM task reader ─────────────────────────────────────────────────


@dataclass(frozen=True)
class SmTask:
    """Minimal frontmatter projection the join needs. More fields are
    present in the CLI output, but the renderer only touches these."""
    id: str
    title: str
    status: str
    updated: str  # ISO timestamp; empty when unparsable
    source: str  # ``jcronq/alice#777`` or similar; often empty
    tags: tuple[str, ...]


def _parse_sm_task_record(obj: dict) -> SmTask | None:
    """Coerce one CLI row (or yaml-safe_load dict) into an :class:`SmTask`.

    Returns ``None`` if ``id`` is missing — every SM row has one, so
    absence means the record was malformed.
    """
    task_id = str(obj.get("id") or "").strip()
    if not task_id:
        return None
    raw_tags = obj.get("tags") or []
    if isinstance(raw_tags, str):
        tags = tuple(t.strip() for t in raw_tags.split(",") if t.strip())
    else:
        tags = tuple(str(t).strip() for t in raw_tags if str(t).strip())
    return SmTask(
        id=task_id,
        title=str(obj.get("title") or "").strip(),
        status=str(obj.get("status") or "").strip(),
        updated=str(obj.get("updated") or "").strip(),
        source=str(obj.get("source") or "").strip(),
        tags=tags,
    )


def read_sm_tasks_building(
    *,
    runner: SubprocessRunner | None = None,
    bin_path: str | None = None,
    tasks_dir: Path | None = None,
) -> list[SmTask]:
    """Return every SM task currently in ``status=building``.

    Tries the ``task --json list --status building`` CLI first (correctness
    — the schema may evolve, the CLI is the write path). On failure, falls
    back to a direct ``inner/tasks/task-*/task.yaml`` glob with a minimal
    yaml reader (no PyYAML dep — the file is tiny and well-formed).
    """
    bin_path = bin_path or os.environ.get("TASK_BIN") or _DEFAULT_TASK_BIN
    r = runner or _default_runner

    cli_out = ""
    try:
        result = r([bin_path, "--json", "list", "--status", "building"])
        if result.returncode == 0:
            cli_out = result.stdout or ""
    except (OSError, subprocess.SubprocessError):
        cli_out = ""

    tasks: list[SmTask] = []
    if cli_out.strip():
        for line in cli_out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed = _parse_sm_task_record(obj)
            if parsed and parsed.status == "building":
                tasks.append(parsed)
        if tasks:
            return tasks

    # Fallback: direct yaml walk. Stays minimal — the schema is 4-6 keys
    # per task.yaml; we don't need a real yaml parser for this.
    return _read_sm_tasks_from_yaml(tasks_dir)


# Minimal yaml-ish reader for the fallback path. Each task.yaml is a flat
# top-level dict (``id: task-NNNN``, ``title: ...``, etc.) with ``tags:`` as
# a single-level sequence. Full PyYAML would work but adds a dep to the
# status-site container that doesn't need it.
_YAML_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")


def _read_sm_tasks_from_yaml(tasks_dir: Path | None) -> list[SmTask]:
    tasks_dir = tasks_dir or _default_tasks_dir()
    if not tasks_dir.is_dir():
        return []
    out: list[SmTask] = []
    for task_dir in sorted(tasks_dir.glob("task-*")):
        yaml_path = task_dir / "task.yaml"
        if not yaml_path.is_file():
            continue
        try:
            text = yaml_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rec: dict = {"tags": []}
        current_list_key: str | None = None
        for line in text.splitlines():
            if not line.strip():
                current_list_key = None
                continue
            # Sequence continuation (``- item``).
            if line.startswith("-"):
                if current_list_key:
                    val = line[1:].strip().strip("'").strip('"')
                    rec.setdefault(current_list_key, []).append(val)
                continue
            m = _YAML_KV_RE.match(line)
            if not m:
                current_list_key = None
                continue
            key, val = m.group(1), m.group(2).strip()
            if val == "":
                current_list_key = key
                rec[key] = []
            else:
                current_list_key = None
                rec[key] = val.strip("'").strip('"')
        parsed = _parse_sm_task_record(rec)
        if parsed and parsed.status == "building":
            out.append(parsed)
    return out


def _default_tasks_dir() -> Path:
    return (
        Path(os.environ.get("ALICE_MIND", os.path.expanduser("~/alice-mind")))
        / "inner"
        / "tasks"
    )


# ─── Open-dispatch computation ──────────────────────────────────────


@dataclass(frozen=True)
class OpenDispatch:
    """One sub-agent currently in flight — a start without a matching stop."""
    tool_call_id: str
    task_id: str | None
    session_id: str
    session_name: str | None  # ``task-NNNN`` on steerable rows, else None
    steerable: bool
    subagent_type: str
    description: str
    started_at: float


def _parse_start_row(row: dict) -> OpenDispatch | None:
    tool_call_id = str(row.get("tool_call_id") or "").strip()
    if not tool_call_id:
        return None
    task_id_raw = row.get("task_id")
    task_id = str(task_id_raw).strip() if task_id_raw else None
    try:
        ts = float(row.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return OpenDispatch(
        tool_call_id=tool_call_id,
        task_id=task_id or None,
        session_id=str(row.get("session_id") or "").strip(),
        session_name=(
            str(row["session_name"]).strip()
            if row.get("session_name")
            else None
        ),
        # Missing / absent is False — design §7.5.
        steerable=bool(row.get("steerable", False)),
        subagent_type=str(row.get("subagent_type") or "").strip(),
        description=str(row.get("description") or "").strip(),
        started_at=ts,
    )


def open_dispatches_by_task(
    starts: Iterable[dict],
    stops: Iterable[dict],
) -> dict[str, list[OpenDispatch]]:
    """Return ``{task_id: [OpenDispatch, ...]}`` for every start lacking a stop.

    Rows with ``task_id is None`` (filtered subagent types — fork / Explore /
    etc.) are dropped from the returned dict per design §1.3 / OQ5.
    """
    closed: set[str] = set()
    for row in stops:
        tcid = row.get("tool_call_id")
        if tcid:
            closed.add(str(tcid))

    by_task: dict[str, list[OpenDispatch]] = defaultdict(list)
    seen_tcids: set[str] = set()
    for row in starts:
        parsed = _parse_start_row(row)
        if parsed is None:
            continue
        # Deduplicate — a rotation that overlaps across files can emit the
        # same start twice. Keep the first occurrence (file order places
        # the older row first, which is fine for timing).
        if parsed.tool_call_id in seen_tcids:
            continue
        seen_tcids.add(parsed.tool_call_id)

        if parsed.tool_call_id in closed:
            continue
        if not parsed.task_id:
            continue  # §1.3 "if there's no task_id, it's not in Running now".
        by_task[parsed.task_id].append(parsed)
    return dict(by_task)


# ─── Row shape — what the renderer and the SSE payload share ────────


@dataclass
class RunningRow:
    """One row of the Running-now section — SM-task-rooted, deterministic.

    Shape deliberately diverges from the pre-#2 ``RunningTask``:
    - no ``branch``/``worktree`` columns (deleted along with ``parse_worktrees``)
    - no ``alive`` bool (presence of ``agents`` is the liveness signal)
    - no scraped PR url (SM frontmatter ``source`` is the canonical pointer)
    - adds ``agent_count``, ``state``, ``steerable_endpoint`` per §3.3 + §7.5
    """
    task_id: str
    title: str
    state: str  # "active" | "idle"
    agent_count: int
    elapsed: str  # human-readable age
    issue: str  # repo-local issue number, or ""
    repo: str  # ``owner/name``, or ""
    pr_url: str  # from SM ``source`` / tags, or "" (unused today; future hook)
    steerable_endpoint: str | None  # ``task-NNNN`` on steerable rows, else None
    agents: list[dict] = field(default_factory=list)

    # Kept for backwards compat with the old ``RunningTask`` consumers —
    # the frontend has an ``alive`` column; we map ``state == "active"`` to
    # True so no React edit is strictly required on this wire.
    @property
    def alive(self) -> bool:
        return self.state == "active"


# ─── Issue / repo derivation — §3.4 ─────────────────────────────────


_SOURCE_RE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_TAG_SOURCE_RE = re.compile(r"^github-issue:([\w.-]+/[\w.-]+)#(\d+)$")


def derive_issue_and_repo(task: SmTask) -> tuple[str, str]:
    """Return ``(issue_number, repo)`` from SM ``source`` or tags.

    - ``source: "jcronq/alice#777"`` → ``("777", "jcronq/alice")``
    - Tag ``"github-issue:jcronq/alice#777"`` → same.
    - Either present → winner. When both, ``source`` wins.
    - Neither → ``("", "")``.
    """
    if task.source:
        m = _SOURCE_RE.match(task.source)
        if m:
            return m.group(2), m.group(1)
    for tag in task.tags:
        m = _TAG_SOURCE_RE.match(tag)
        if m:
            return m.group(2), m.group(1)
    return "", ""


# ─── Elapsed formatter ──────────────────────────────────────────────


def _elapsed_str(seconds: float) -> str:
    if seconds < 1:
        return "0s"
    if seconds < 60:
        return f"{int(seconds)}s"
    m = int(seconds // 60)
    if m < 60:
        return f"{m}m"
    h, rem_m = divmod(m, 60)
    return f"{h}h {rem_m}m" if rem_m else f"{h}h"


def _parse_iso(ts: str) -> float | None:
    """Loose ISO 8601 → epoch. Returns None on anything unparseable."""
    if not ts:
        return None
    # Python 3.11+'s fromisoformat handles ``+00:00`` fine; normalise ``Z``.
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# ─── Core collect ───────────────────────────────────────────────────


def build_rows(
    tasks: Iterable[SmTask],
    open_by_task: dict[str, list[OpenDispatch]],
    *,
    now: float | None = None,
) -> list[RunningRow]:
    """Assemble ``RunningRow`` list from SM tasks + open dispatches.

    ``now`` is injected so tests can pin wall clock.
    """
    if now is None:
        now = time.time()
    rows: list[RunningRow] = []
    for task in tasks:
        opens = sorted(
            open_by_task.get(task.id, []), key=lambda o: o.started_at
        )
        state = "active" if opens else "idle"
        if opens:
            earliest = opens[0].started_at or now
            elapsed = _elapsed_str(max(0.0, now - earliest))
        else:
            updated_ts = _parse_iso(task.updated) or now
            elapsed = _elapsed_str(max(0.0, now - updated_ts))

        issue, repo = derive_issue_and_repo(task)
        # Steerability — any open dispatch marked steerable makes the row
        # steerable. The endpoint label is the ``session_name`` the
        # steerer uses with ``SendMessage(to=...)``.
        steer_name: str | None = None
        for o in opens:
            if o.steerable and o.session_name:
                steer_name = o.session_name
                break

        rows.append(
            RunningRow(
                task_id=task.id,
                title=task.title,
                state=state,
                agent_count=len(opens),
                elapsed=elapsed,
                issue=issue,
                repo=repo,
                pr_url="",  # future: lift from ``merge_ref`` when CLI exports it
                steerable_endpoint=steer_name,
                agents=[
                    {
                        "tool_call_id": o.tool_call_id,
                        "subagent_type": o.subagent_type,
                        "description": o.description,
                        "started_at": o.started_at,
                    }
                    for o in opens
                ],
            )
        )
    return rows


def collect(
    *,
    now: float | None = None,
    start_log: Path | None = None,
    stop_log: Path | None = None,
    tasks_dir: Path | None = None,
    task_bin: str | None = None,
    runner: SubprocessRunner | None = None,
    hide_idle: bool = False,
) -> list[RunningRow]:
    """Run the full collection pipeline — the server's background poller
    calls this once per tick.

    ``hide_idle=False`` keeps idle ``building`` rows visible per design
    OQ2. Callers that want the terser view set True.
    """
    starts = read_start_log(start_log)
    stops = read_stop_log(stop_log)
    open_by_task = open_dispatches_by_task(starts, stops)
    tasks = read_sm_tasks_building(
        runner=runner, bin_path=task_bin, tasks_dir=tasks_dir
    )
    rows = build_rows(tasks, open_by_task, now=now)
    if hide_idle:
        rows = [r for r in rows if r.state == "active"]
    # Sort: active first, then by elapsed ascending (shortest-elapsed on
    # top = "just started" above "running a while"). Stable, deterministic.
    rows.sort(key=lambda r: (r.state != "active", r.elapsed))
    return rows


# ─── Render — Markdown fallback mirror of §3.5 ──────────────────────


def render(
    rows: Sequence[RunningRow],
    *,
    now: datetime | None = None,
) -> str:
    """Render the Running-now section body as markdown.

    The React shell uses the typed ``rows`` payload for its table, but the
    ``/index.md`` fallback and SSE snapshot carry this markdown so the page
    stays serveable even if the React bundle fails.
    """
    if not rows:
        body = "*(nothing building right now)*"
    else:
        lines: list[str] = []
        for r in rows:
            steer = " 📡" if r.steerable_endpoint else ""
            label = r.title or r.task_id
            count_frag = (
                f"active ({r.agent_count} agent{'s' if r.agent_count != 1 else ''}, {r.elapsed})"
                if r.state == "active"
                else f"idle {r.elapsed}, no agent"
            )
            lines.append(f"- **{r.task_id}** _{label}_ — {count_frag}{steer}")
        body = "\n".join(lines)
    return body + "\n\n" + staleness_footer(SOURCE_NAME, now=now)


# ─── Thin backward-compat helper for the server's deploy-sha probe ──
#
# The status-site server also uses ``_running_now.repo_dir_from_env()`` to
# locate the repo for a ``git log -1 --format=...`` check. That's an
# orthogonal concern (deploy-status display), not the running-now join —
# preserve the helper verbatim so nothing else has to move.


def repo_dir_from_env(env: dict[str, str] | None = None) -> Path:
    e = dict(os.environ if env is None else env)
    return Path(e.get("GIT_REPO_DIR", "/home/alice/alice"))
