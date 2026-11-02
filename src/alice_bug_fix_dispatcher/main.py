"""Bug-fix dispatcher: scan ``inner/status/bug-*.md`` and act.

One tick:

1. Honour the pause gate. If the sentinel file exists OR the config
   JSON carries ``bug_fix_dispatcher.wave_a_signoff_pending=true``,
   log a skip line and return — no scan.
2. Scan the status dir for ``bug-<N>.md`` files; parse the
   frontmatter.
3. Route each bug by ``status``:

   * ``open`` and no ``dispatched_at``        → spawn a worker,
                                                 flip to ``in-progress``.
   * ``in-progress`` and no ``pr_url``        → check for a worker
                                                 result; adopt
                                                 ``pr_url`` if present.
                                                 Otherwise sweep for
                                                 stall (dispatched_at
                                                 older than the
                                                 configured TTL →
                                                 ``dispatch-failed``).
   * ``in-progress`` and a ``pr_url``         → poll the PR. Merged
                                                 → ``fixed``; closed
                                                 unmerged → revert
                                                 to ``open``.
   * anything else                            → no-op.
4. Journal every transition to
   ``inner/state/bug-fix-dispatcher.log``.

Writes are atomic — the dispatcher never silently redispatches a
bug that already has ``dispatched_at`` set. If a worker spawn fails
for a technical reason, the bug reverts to ``open`` so the next
tick can retry; if a worker is dispatched but never produces a PR
within the stall TTL, the bug lands in ``dispatch-failed`` and
needs a human triage.

The dispatcher never calls GitHub itself for anything except the
PR-state poll (``gh pr view``) — all mutation goes through the
spawned worker, so the ``--draft`` invariant lives in exactly one
place: the worker prompt template.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from . import frontmatter as _fm


DEFAULT_STATUS_DIR = pathlib.Path("/home/alice/alice-mind/inner/status")
DEFAULT_JOURNAL = pathlib.Path("/home/alice/alice-mind/inner/state/bug-fix-dispatcher.log")
DEFAULT_PAUSE_SENTINEL = pathlib.Path(
    "/home/alice/alice-mind/inner/status/.bug-fix-dispatcher-paused"
)
DEFAULT_CONFIG_JSON = pathlib.Path("/home/alice/alice-mind/config/alice.config.json")
DEFAULT_WORKER_SCRATCH_ROOT = pathlib.Path("/state/worker/bug-fix-worker")
DEFAULT_WORKER_PROMPT_TEMPLATE = pathlib.Path(
    "/home/alice/alice-mind/cortex-memory/reference/mud-bug-fix-worker-prompt.md"
)
DEFAULT_STALL_HOURS = 2
DEFAULT_REPO = "jcronq/modern-mud"


BUG_FILE_RE = re.compile(r"^bug-(\d+)\.md$")


# ---------------------------------------------------------------- data classes


@dataclass
class DispatcherConfig:
    status_dir: pathlib.Path = DEFAULT_STATUS_DIR
    journal_path: pathlib.Path = DEFAULT_JOURNAL
    pause_sentinel: pathlib.Path = DEFAULT_PAUSE_SENTINEL
    config_json: pathlib.Path = DEFAULT_CONFIG_JSON
    worker_scratch_root: pathlib.Path = DEFAULT_WORKER_SCRATCH_ROOT
    worker_prompt_template: pathlib.Path = DEFAULT_WORKER_PROMPT_TEMPLATE
    stall_hours: int = DEFAULT_STALL_HOURS
    repo: str = DEFAULT_REPO


@dataclass
class TickReport:
    scanned: int = 0
    paused: bool = False
    dispatched: list[int] = field(default_factory=list)
    adopted_pr_url: list[int] = field(default_factory=list)
    fixed: list[int] = field(default_factory=list)
    reopened: list[int] = field(default_factory=list)
    stalled: list[int] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)


# ---------------------------------------------------------------- helpers


def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> _dt.datetime | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_config_pause_flag(config_json: pathlib.Path) -> bool:
    """``bug_fix_dispatcher.wave_a_signoff_pending`` truthy → paused."""
    if not config_json.is_file():
        return False
    try:
        data = json.loads(config_json.read_text())
    except (OSError, ValueError):
        return False
    section = data.get("bug_fix_dispatcher") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return False
    return bool(section.get("wave_a_signoff_pending"))


def _journal(journal_path: pathlib.Path, line: str) -> None:
    """Append a single timestamped line. Best-effort — swallows IOError."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(journal_path, "a") as f:
            f.write(f"{_now_utc_iso()} {line}\n")
    except OSError:
        pass


def _scan_bugs(status_dir: pathlib.Path) -> list[tuple[int, pathlib.Path, dict[str, str]]]:
    """Return ``[(report_id, path, frontmatter_dict), ...]`` sorted by id ascending."""
    out: list[tuple[int, pathlib.Path, dict[str, str]]] = []
    if not status_dir.is_dir():
        return out
    for p in sorted(status_dir.glob("bug-*.md")):
        m = BUG_FILE_RE.fullmatch(p.name)
        if not m:
            continue
        try:
            text = p.read_text()
        except OSError:
            continue
        fields, _body, _raw = _fm.parse(text)
        try:
            report_id = int(fields.get("report_id") or m.group(1))
        except (TypeError, ValueError):
            report_id = int(m.group(1))
        out.append((report_id, p, fields))
    out.sort(key=lambda row: row[0])
    return out


def _render_prompt(
    template_text: str,
    *,
    bug_id: int,
    title: str,
    report_text: str,
    captured_state: str,
    build_sha: str,
) -> str:
    """Substitute ``{{...}}`` placeholders. Literal substitution only."""
    out = template_text
    out = out.replace("{{bug_id}}", str(bug_id))
    out = out.replace("{{title}}", title)
    out = out.replace("{{report_text}}", report_text)
    out = out.replace("{{captured_state}}", captured_state)
    out = out.replace("{{build_sha}}", build_sha or "unknown")
    return out


def _extract_captured_state(body: str) -> str:
    """Grab the ``## Who / where`` through ``## Likely fix surface`` chunk.

    The ``bug-N.md`` body is the ingress forwarder's write — the
    worker needs the location/flags/counters block to zero in on the
    content wire-up. We hand the worker the whole body minus the
    ``# bug-N — …`` heading and the ``## Report`` section (that part
    goes to ``report_text`` directly), so the prompt stays targeted.

    Falls back to the full body if the markers aren't present.
    """
    marker = "## Who / where"
    idx = body.find(marker)
    return body[idx:] if idx >= 0 else body


def _extract_report_text(body: str) -> str:
    """Grab the ``> ...`` blockquote under ``## Report`` as plain text.

    Falls back to the whole body if the markers aren't present.
    """
    m = re.search(r"^## Report\s*\n+(?P<block>(> .*(?:\n|$))+)", body, re.M)
    if not m:
        return body.strip()
    lines = []
    for raw in m.group("block").splitlines():
        if raw.startswith("> "):
            lines.append(raw[2:])
        elif raw.startswith(">"):
            lines.append(raw[1:])
        else:
            lines.append(raw)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- transitions


def _dispatch_worker(
    *,
    report_id: int,
    bug_path: pathlib.Path,
    fields: Mapping[str, str],
    body: str,
    cfg: DispatcherConfig,
    log: Callable[[str], None],
    now_iso: Callable[[], str] = _now_utc_iso,
    spawn: Callable[[int, pathlib.Path, str], int | None] | None = None,
) -> tuple[bool, dict]:
    """Spawn a worker for an ``open`` bug and flip the frontmatter.

    Returns ``(dispatched, patch)``. ``patch`` is the dict of fields
    merged into the frontmatter on success; empty on failure.
    """
    template_path = cfg.worker_prompt_template
    if not template_path.is_file():
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: worker prompt template missing "
            f"at {template_path} — cannot dispatch"
        )
        return False, {}
    try:
        template_text = template_path.read_text()
    except OSError as exc:
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: failed to read template "
            f"{template_path}: {exc}"
        )
        return False, {}

    title = fields.get("title", f"bug-{report_id}")
    build_sha = ""
    m = re.search(r"Build:\s*([0-9a-f]{7,40})", body)
    if m:
        build_sha = m.group(1)
    captured_state = _extract_captured_state(body)
    report_text = _extract_report_text(body)

    prompt = _render_prompt(
        template_text,
        bug_id=report_id,
        title=title,
        report_text=report_text,
        captured_state=captured_state,
        build_sha=build_sha,
    )

    scratch_dir = cfg.worker_scratch_root / f"bug-{report_id}"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    (scratch_dir / "prompt.md").write_text(prompt)

    spawner = spawn or _spawn_claude_detached
    pid = spawner(report_id, scratch_dir, prompt)
    if pid is None:
        log(f"[bug-fix-dispatcher] bug-{report_id}: worker spawn failed")
        return False, {}

    patch = {
        "status": "in-progress",
        "dispatched_at": now_iso(),
        "worker_task_id": f"pid:{pid}",
    }
    try:
        _fm.update(bug_path, patch)
    except OSError as exc:
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: frontmatter write failed after "
            f"spawn (pid {pid}): {exc}"
        )
        return False, {}
    log(
        f"[bug-fix-dispatcher] bug-{report_id}: dispatched worker pid={pid} "
        f"scratch={scratch_dir}"
    )
    return True, patch


def _spawn_claude_detached(
    report_id: int, scratch_dir: pathlib.Path, prompt: str
) -> int | None:
    """Launch a detached ``claude -p`` worker. Returns its PID or None.

    ``setsid`` + ``start_new_session=True`` decouples the worker from
    the dispatcher's s6 process group so a dispatcher exit doesn't
    SIGHUP the worker. stdout/stderr land in ``scratch_dir`` so the
    worker's structured JSON return is readable on the next tick.
    """
    stdout_path = scratch_dir / "result.json"
    stderr_path = scratch_dir / "stderr.log"
    try:
        stdout_f = open(stdout_path, "wb")
        stderr_f = open(stderr_path, "wb")
    except OSError:
        return None
    try:
        proc = subprocess.Popen(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--permission-mode",
                "acceptEdits",
            ],
            stdin=subprocess.DEVNULL,
            stdout=stdout_f,
            stderr=stderr_f,
            start_new_session=True,
            close_fds=True,
            cwd=str(scratch_dir),
        )
    except OSError:
        stdout_f.close()
        stderr_f.close()
        return None
    finally:
        # The child inherits the fds; the parent can close its copies.
        try:
            stdout_f.close()
            stderr_f.close()
        except OSError:
            pass
    return proc.pid


def _adopt_worker_result(
    *,
    report_id: int,
    bug_path: pathlib.Path,
    fields: Mapping[str, str],
    cfg: DispatcherConfig,
    log: Callable[[str], None],
    now_iso: Callable[[], str] = _now_utc_iso,
) -> tuple[bool, bool]:
    """If a worker produced a result.json, adopt its ``pr_url`` into the frontmatter.

    Returns ``(adopted, stalled)``.

    * ``adopted`` is True when ``pr_url`` was added. Dispatcher's next
      tick will poll it.
    * ``stalled`` is True when the dispatched_at is older than the
      stall TTL and no result/pr_url has materialised. The caller
      transitions the bug to ``dispatch-failed``.
    """
    scratch_dir = cfg.worker_scratch_root / f"bug-{report_id}"
    result_path = scratch_dir / "result.json"
    pr_url = _extract_pr_url_from_result(result_path)
    if pr_url:
        patch = {
            "pr_url": pr_url,
            "last_check_at": now_iso(),
        }
        try:
            _fm.update(bug_path, patch)
        except OSError as exc:
            log(
                f"[bug-fix-dispatcher] bug-{report_id}: failed to adopt pr_url "
                f"{pr_url}: {exc}"
            )
            return False, False
        log(f"[bug-fix-dispatcher] bug-{report_id}: adopted pr_url={pr_url}")
        return True, False

    # No pr_url yet — check stall.
    dispatched = _parse_iso(fields.get("dispatched_at", ""))
    if dispatched is None:
        return False, False
    now = _parse_iso(now_iso()) or _dt.datetime.now(_dt.timezone.utc)
    age = now - dispatched
    if age >= _dt.timedelta(hours=cfg.stall_hours):
        return False, True
    return False, False


def _extract_pr_url_from_result(result_path: pathlib.Path) -> str:
    """Pull a PR URL out of the worker's ``result.json`` if present.

    ``claude -p --output-format json`` emits a wrapper envelope with
    the agent transcript. We first try to parse the whole file as
    JSON (the structured worker contract — ``{"pr_url": "..."}`` or
    the full agent-SDK envelope with ``result`` as a stringified
    JSON). Falls back to a regex over the raw text for the first
    ``https://github.com/<org>/<repo>/pull/<n>`` URL — belt-and-
    braces for workers that don't emit tidy JSON.
    """
    if not result_path.is_file():
        return ""
    try:
        text = result_path.read_text()
    except OSError:
        return ""
    if not text.strip():
        return ""

    def _descend(obj: object) -> str:
        if isinstance(obj, dict):
            v = obj.get("pr_url")
            if isinstance(v, str) and v.startswith("http"):
                return v
            # Agent SDK envelope wraps the real payload under "result"
            # as a stringified JSON. Try to parse it.
            inner = obj.get("result")
            if isinstance(inner, str):
                try:
                    parsed = json.loads(inner)
                except ValueError:
                    parsed = None
                if parsed is not None:
                    out = _descend(parsed)
                    if out:
                        return out
            for value in obj.values():
                out = _descend(value)
                if out:
                    return out
        elif isinstance(obj, list):
            for item in obj:
                out = _descend(item)
                if out:
                    return out
        return ""

    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if data is not None:
        found = _descend(data)
        if found:
            return found

    m = re.search(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", text)
    return m.group(0) if m else ""


def _poll_pr(
    pr_url: str,
    *,
    gh_pr_view: Callable[[str], dict] | None = None,
) -> dict:
    """Call ``gh pr view --json state,mergedAt,mergeCommit`` for a PR URL.

    Returns the parsed dict, or ``{}`` on failure.
    """
    if gh_pr_view is not None:
        try:
            return gh_pr_view(pr_url)
        except Exception:  # noqa: BLE001 — test fakes shouldn't blow up the tick
            return {}
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                pr_url,
                "--json",
                "state,mergedAt,mergeCommit,url",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {}


def _check_pr_state(
    *,
    report_id: int,
    bug_path: pathlib.Path,
    fields: Mapping[str, str],
    cfg: DispatcherConfig,
    log: Callable[[str], None],
    now_iso: Callable[[], str] = _now_utc_iso,
    gh_pr_view: Callable[[str], dict] | None = None,
) -> str:
    """Poll the PR and apply any resulting transition.

    Returns one of ``"fixed"``, ``"reopened"``, ``""`` (no change).
    """
    pr_url = fields.get("pr_url", "").strip()
    if not pr_url:
        return ""
    data = _poll_pr(pr_url, gh_pr_view=gh_pr_view)
    state = (data.get("state") or "").upper()
    if state == "MERGED":
        merge_commit = data.get("mergeCommit") or {}
        if isinstance(merge_commit, dict):
            fix_commit = merge_commit.get("oid", "")
        else:
            fix_commit = str(merge_commit)
        patch = {
            "status": "fixed",
            "fix_commit": fix_commit,
            "fix_pr": pr_url,
            "last_check_at": now_iso(),
        }
        _fm.update(bug_path, patch)
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: PR merged ({pr_url}) → "
            f"status=fixed fix_commit={fix_commit}"
        )
        return "fixed"
    if state == "CLOSED":
        # Closed-unmerged: revert to open for retry. We drop
        # ``dispatched_at`` / ``worker_task_id`` / ``pr_url`` so the
        # idempotency key is cleared; stamp
        # ``last_dispatch_failed_at`` + ``last_failed_pr`` so Jason
        # can see the previous attempt in the file.
        patch: dict[str, object | None] = {
            "status": "open",
            "last_dispatch_failed_at": now_iso(),
            "last_failed_pr": pr_url,
            "dispatched_at": None,
            "worker_task_id": None,
            "pr_url": None,
        }
        _fm.update(bug_path, patch)
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: PR closed-unmerged ({pr_url}) → "
            f"status=open (retry)"
        )
        return "reopened"
    # OPEN / DRAFT / unknown: just update last_check_at.
    _fm.update(bug_path, {"last_check_at": now_iso()})
    return ""


# ---------------------------------------------------------------- tick


def tick(
    cfg: DispatcherConfig | None = None,
    *,
    log: Callable[[str], None] | None = None,
    now_iso: Callable[[], str] = _now_utc_iso,
    spawn: Callable[[int, pathlib.Path, str], int | None] | None = None,
    gh_pr_view: Callable[[str], dict] | None = None,
) -> TickReport:
    """Run one scan + act pass.

    All I/O boundaries are injectable so the test suite can drive the
    tick without touching the real ``inner/status`` tree or spawning
    real worker processes.
    """
    cfg = cfg or DispatcherConfig()
    report = TickReport()

    def _log(line: str) -> None:
        if log is not None:
            log(line)
        _journal(cfg.journal_path, line)

    # --- pause gate
    sentinel_present = cfg.pause_sentinel.exists()
    config_pending = _read_config_pause_flag(cfg.config_json)
    if sentinel_present or config_pending:
        report.paused = True
        reasons = []
        if sentinel_present:
            reasons.append(f"sentinel={cfg.pause_sentinel}")
        if config_pending:
            reasons.append("wave_a_signoff_pending=true")
        _log(
            f"[bug-fix-dispatcher] paused ({', '.join(reasons)}) — "
            f"skipping scan"
        )
        return report

    # --- scan
    bugs = _scan_bugs(cfg.status_dir)
    report.scanned = len(bugs)

    for report_id, bug_path, fields in bugs:
        status = (fields.get("status") or "").strip().lower()
        try:
            if status == "open" and not fields.get("dispatched_at"):
                ok, _patch = _dispatch_worker(
                    report_id=report_id,
                    bug_path=bug_path,
                    fields=fields,
                    body=_body_from(bug_path),
                    cfg=cfg,
                    log=_log,
                    now_iso=now_iso,
                    spawn=spawn,
                )
                if ok:
                    report.dispatched.append(report_id)
                else:
                    report.skipped.append((report_id, "spawn-failed"))
            elif status == "open" and fields.get("dispatched_at"):
                # Already-dispatched record whose status label is
                # still ``open``: this is the "closed unmerged, try
                # again next cycle" case. We cleared dispatched_at on
                # the revert, so this branch only trips on a hand-
                # edited file. Log + skip so we don't double-dispatch.
                _log(
                    f"[bug-fix-dispatcher] bug-{report_id}: status=open but "
                    f"dispatched_at present — skipping (hand-edit? clear "
                    f"dispatched_at to re-dispatch)"
                )
                report.skipped.append((report_id, "stale-dispatched_at"))
            elif status == "in-progress":
                pr_url = fields.get("pr_url", "").strip()
                if pr_url:
                    outcome = _check_pr_state(
                        report_id=report_id,
                        bug_path=bug_path,
                        fields=fields,
                        cfg=cfg,
                        log=_log,
                        now_iso=now_iso,
                        gh_pr_view=gh_pr_view,
                    )
                    if outcome == "fixed":
                        report.fixed.append(report_id)
                    elif outcome == "reopened":
                        report.reopened.append(report_id)
                else:
                    adopted, stalled = _adopt_worker_result(
                        report_id=report_id,
                        bug_path=bug_path,
                        fields=fields,
                        cfg=cfg,
                        log=_log,
                        now_iso=now_iso,
                    )
                    if adopted:
                        report.adopted_pr_url.append(report_id)
                    elif stalled:
                        patch = {
                            "status": "dispatch-failed",
                            "last_dispatch_failed_at": now_iso(),
                        }
                        _fm.update(bug_path, patch)
                        _log(
                            f"[bug-fix-dispatcher] bug-{report_id}: no PR within "
                            f"{cfg.stall_hours}h → status=dispatch-failed"
                        )
                        report.stalled.append(report_id)
            else:
                # fixed / wontfix / dispatch-failed / unknown — no action.
                report.skipped.append((report_id, f"status={status or 'missing'}"))
        except OSError as exc:
            _log(
                f"[bug-fix-dispatcher] bug-{report_id}: I/O error during "
                f"transition: {exc}"
            )
            report.skipped.append((report_id, f"ioerror:{exc.__class__.__name__}"))

    _log(
        f"[bug-fix-dispatcher] done — scanned={report.scanned} "
        f"dispatched={len(report.dispatched)} "
        f"adopted_pr_url={len(report.adopted_pr_url)} "
        f"fixed={len(report.fixed)} reopened={len(report.reopened)} "
        f"stalled={len(report.stalled)} skipped={len(report.skipped)}"
    )
    return report


def _body_from(bug_path: pathlib.Path) -> str:
    """Read a bug file's body (post-frontmatter) for prompt rendering."""
    try:
        text = bug_path.read_text()
    except OSError:
        return ""
    _fields, body, _raw = _fm.parse(text)
    return body


# ---------------------------------------------------------------- CLI


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One tick of the Modern-MUD bug-fix dispatcher."
    )
    parser.add_argument(
        "--status-dir",
        default=str(DEFAULT_STATUS_DIR),
        help="path to inner/status (where bug-*.md live)",
    )
    parser.add_argument(
        "--journal",
        default=str(DEFAULT_JOURNAL),
        help="path to the dispatcher log file",
    )
    parser.add_argument(
        "--pause-sentinel",
        default=str(DEFAULT_PAUSE_SENTINEL),
        help="when this file exists the dispatcher logs and does nothing",
    )
    parser.add_argument(
        "--worker-scratch-root",
        default=str(DEFAULT_WORKER_SCRATCH_ROOT),
        help="per-bug worker scratch dirs land under here",
    )
    parser.add_argument(
        "--worker-prompt-template",
        default=str(DEFAULT_WORKER_PROMPT_TEMPLATE),
        help="path to the mud-bug-fix worker prompt template",
    )
    parser.add_argument(
        "--stall-hours",
        type=int,
        default=DEFAULT_STALL_HOURS,
        help="treat a bug as dispatch-failed when dispatched_at exceeds this age",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help="the modern-mud GitHub slug (reserved for future PR comment paths)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    cfg = DispatcherConfig(
        status_dir=pathlib.Path(args.status_dir),
        journal_path=pathlib.Path(args.journal),
        pause_sentinel=pathlib.Path(args.pause_sentinel),
        worker_scratch_root=pathlib.Path(args.worker_scratch_root),
        worker_prompt_template=pathlib.Path(args.worker_prompt_template),
        stall_hours=args.stall_hours,
        repo=args.repo,
    )

    def _stderr(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    try:
        tick(cfg, log=_stderr)
    except Exception as exc:  # noqa: BLE001 — supervisor retries on next cadence
        _stderr(f"[bug-fix-dispatcher] tick crashed: {type(exc).__name__}: {exc}")
        return 1
    return 0
