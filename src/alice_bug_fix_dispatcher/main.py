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
                                                 to ``open``; green
                                                 draft from a trusted
                                                 reporter → ``gh pr
                                                 ready`` +
                                                 ``ready-for-review``.
   * ``ready-for-review`` and a ``pr_url``    → keep polling so a merge
                                                 still routes to
                                                 ``fixed``.
   * anything else                            → no-op.
4. Journal every transition to
   ``inner/state/bug-fix-dispatcher.log``.

Writes are atomic — the dispatcher never silently redispatches a
bug that already has ``dispatched_at`` set. If a worker spawn fails
for a technical reason, the bug reverts to ``open`` so the next
tick can retry; if a worker is dispatched but never produces a PR
within the stall TTL, the bug lands in ``dispatch-failed`` and
needs a human triage.

The dispatcher only calls GitHub for the PR-state poll (``gh pr
view``) and, for trusted reporters, the draft → ready flip
(``gh pr ready``) once CI is green. Everything else goes through the
spawned worker, so the ``--draft`` invariant at PR creation lives in
the worker prompt template.

At the end of each tick the ``<!-- bug-review -->`` block under
``## Waiting on you`` in ``index.md`` is rebuilt: one
``Review PR #N`` line per ``ready-for-review`` bug.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import io
import json
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from alice_pricing import (
    actual_cost_from_tokens,
    estimate_cost,
    estimate_tokens,
    format_usd,
    read_worker_tokens,
)
from alice_pricing.rates import is_free

from status_site import cli as _status_cli
from status_site.history import title_slug as _site_title_slug
from status_site.server import _bug_title_from_heading

from . import frontmatter as _fm

# Issue #786 M3 — rate-limit pre-flight gate shared with the SM
# dispatcher. Reading the backoff file is a fast local-filesystem
# check; the gate runs once per bug before the ``claude -p`` spawn so
# a burst of in-flight bugs can't trip the secondary rate limit.
from alice_forge.rate_limit import (
    DEFAULT_MIN_CORE as _RL_DEFAULT_MIN_CORE,
    DEFAULT_MIN_GRAPHQL as _RL_DEFAULT_MIN_GRAPHQL,
    DeferDecision as _RateLimitDeferDecision,
    should_defer as _rate_limit_should_defer,
)


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
DEFAULT_INDEX_PATH = pathlib.Path("/home/alice/alice-mind/inner/status/index.md")
DEFAULT_TRUSTED_REPORTERS: tuple[str, ...] = ("cronqj",)
WAITING_HEADING_RE = re.compile(r"^##\s+Waiting on you\s*$", re.M | re.I)
BUG_REVIEW_START = "<!-- bug-review -->"
BUG_REVIEW_END = "<!-- /bug-review -->"
BUG_REVIEW_BLOCK_RE = re.compile(
    r"(?:\n\n)?" + re.escape(BUG_REVIEW_START) + r".*?" + re.escape(BUG_REVIEW_END),
    re.S,
)
ACCOUNT_RE = re.compile(r"^[\s>*_-]*Account\**:\**\s*@?(\S+)", re.M | re.I)
# The dispatcher spawns ``claude -p`` without passing ``--model``; the
# user's current Claude Code setup picks the active model at launch
# time, which today is Opus 4.7. Keep the dispatcher-side default
# aligned so the estimate column reflects reality. Overridable via
# ``bug_fix_dispatcher.worker_model`` in alice.config.json.
DEFAULT_WORKER_MODEL = "claude-opus-4-7"
# Output budget sized for a code-fix PR; mirrors the pricing module.
DEFAULT_EXPECTED_OUTPUT_TOKENS = 8_000
# How long the scratch dir must sit idle before we attempt to read
# actuals on a worker whose result.json is empty or malformed. 5
# minutes matches the spec; keeps us from pricing a mid-flight log.
ACTUALS_SETTLE_SECONDS = 300


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
    # Pricing knobs. ``worker_model`` + ``expected_output_tokens`` feed
    # the pre-flight ``estimate_usd`` write. Overridable in
    # ``alice.config.json`` under ``bug_fix_dispatcher.*`` so Jason can
    # swap models without a redeploy.
    worker_model: str = DEFAULT_WORKER_MODEL
    expected_output_tokens: int = DEFAULT_EXPECTED_OUTPUT_TOKENS
    # Auto ready-for-review. ``index_path`` is the status-site index whose
    # ``## Waiting on you`` section gets the dispatcher-owned block;
    # ``trusted_reporters`` gates the draft → ready flip. Both overridable
    # under ``bug_fix_dispatcher.*`` in alice.config.json.
    index_path: pathlib.Path = DEFAULT_INDEX_PATH
    trusted_reporters: tuple[str, ...] = DEFAULT_TRUSTED_REPORTERS


@dataclass
class TickReport:
    scanned: int = 0
    paused: bool = False
    dispatched: list[int] = field(default_factory=list)
    adopted_pr_url: list[int] = field(default_factory=list)
    fixed: list[int] = field(default_factory=list)
    reopened: list[int] = field(default_factory=list)
    ready: list[int] = field(default_factory=list)
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


def _read_config_pricing(cfg: "DispatcherConfig") -> tuple[str, int]:
    """Return ``(worker_model, expected_output_tokens)`` from alice.config.json.

    Falls back to the dataclass defaults when the file is missing /
    malformed / lacks the keys. Reading at tick time (not instance
    construction) means Jason can edit the config file and get a new
    rate without restarting the dispatcher service.
    """
    worker_model = cfg.worker_model
    expected_out = cfg.expected_output_tokens
    if not cfg.config_json.is_file():
        return worker_model, expected_out
    try:
        data = json.loads(cfg.config_json.read_text())
    except (OSError, ValueError):
        return worker_model, expected_out
    section = data.get("bug_fix_dispatcher") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return worker_model, expected_out
    override_model = section.get("worker_model")
    if isinstance(override_model, str) and override_model:
        worker_model = override_model
    override_out = section.get("expected_output_tokens")
    if isinstance(override_out, int) and override_out > 0:
        expected_out = override_out
    return worker_model, expected_out


def _read_config_trusted_reporters(cfg: "DispatcherConfig") -> tuple[str, ...]:
    """``bug_fix_dispatcher.trusted_reporters`` (list of strings) or the cfg default."""
    if not cfg.config_json.is_file():
        return cfg.trusted_reporters
    try:
        data = json.loads(cfg.config_json.read_text())
    except (OSError, ValueError):
        return cfg.trusted_reporters
    section = data.get("bug_fix_dispatcher") if isinstance(data, dict) else None
    override = section.get("trusted_reporters") if isinstance(section, dict) else None
    if isinstance(override, list) and all(isinstance(r, str) for r in override):
        return tuple(override)
    return cfg.trusted_reporters


def _reporter_trusted(
    fields: Mapping[str, str], body: str, cfg: "DispatcherConfig"
) -> bool:
    """True when the bug's reporter is on the trusted list.

    Reads the ``reporter`` frontmatter field, falling back to the
    ``Account: <x>`` line in the body. Comparison is case-insensitive.
    """
    reporter = (fields.get("reporter") or "").strip()
    if not reporter:
        m = ACCOUNT_RE.search(body)
        reporter = m.group(1) if m else ""
    reporter = reporter.lstrip("@").lower()
    if not reporter:
        return False
    trusted = {r.lstrip("@").lower() for r in _read_config_trusted_reporters(cfg)}
    return reporter in trusted


def _pr_is_green(data: Mapping[str, object]) -> bool:
    """True when ``statusCheckRollup`` is non-empty and every context is SUCCESS.

    Handles both CheckRun (``conclusion``) and StatusContext (``state``)
    shapes. An empty rollup (no CI) is not green.
    """
    rollup = data.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        return False
    for ctx in rollup:
        if not isinstance(ctx, dict):
            return False
        verdict = ctx.get("conclusion") or ctx.get("state") or ""
        if str(verdict).upper() != "SUCCESS":
            return False
    return True


def _mark_pr_ready(
    pr_url: str,
    *,
    gh_pr_ready: Callable[[str], None] | None = None,
) -> bool:
    """Run ``gh pr ready <url>``. Returns True on success."""
    if gh_pr_ready is not None:
        try:
            gh_pr_ready(pr_url)
        except Exception:  # noqa: BLE001 — test fakes shouldn't blow up the tick
            return False
        return True
    try:
        subprocess.run(
            ["gh", "pr", "ready", pr_url],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def _rebuild_waiting_block(status_dir: pathlib.Path, index_path: pathlib.Path) -> bool:
    """Rewrite the ``<!-- bug-review -->`` block in ``index.md``'s Waiting section.

    One ``[ ] Review PR #N (bug-N: <title>)`` line per ``ready-for-review``
    bug. Content outside the markers is never touched. Returns True when
    the file changed; idempotent across ticks.
    """
    if not index_path.is_file():
        return False
    try:
        text = index_path.read_text()
    except OSError:
        return False

    lines = []
    for report_id, _path, fields in _scan_bugs(status_dir):
        if (fields.get("status") or "").strip().lower() != "ready-for-review":
            continue
        m = re.search(r"/pull/(\d+)", fields.get("pr_url", ""))
        pr_label = f"PR #{m.group(1)}" if m else "PR"
        title = fields.get("title", f"bug-{report_id}")
        lines.append(f"- [ ] Review {pr_label} (bug-{report_id}: {title})")

    block = "\n".join([BUG_REVIEW_START, *lines, BUG_REVIEW_END]) if lines else ""
    if BUG_REVIEW_BLOCK_RE.search(text):
        new = BUG_REVIEW_BLOCK_RE.sub(
            lambda _m: ("\n\n" + block) if block else "", text, count=1
        )
    elif not block:
        return False
    else:
        heading = WAITING_HEADING_RE.search(text)
        if heading:
            new = text[: heading.end()] + "\n\n" + block + text[heading.end() :]
        else:
            new = text.rstrip("\n") + "\n\n## Waiting on you\n\n" + block + "\n"
    if new == text:
        return False
    _fm.atomic_write(index_path, new)
    return True


def _journal(journal_path: pathlib.Path, line: str) -> None:
    """Append a single timestamped line. Best-effort — swallows IOError."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(journal_path, "a") as f:
            f.write(f"{_now_utc_iso()} {line}\n")
    except OSError:
        pass


def _status_reply_default(slug: str, msg: str) -> None:
    """Append ``msg`` to the status-site thread ``slug`` (what ``status-reply`` does)."""
    with contextlib.redirect_stdout(io.StringIO()):
        _status_cli.main([slug, msg])


def _site_title(
    report_id: int, fields: Mapping[str, str], bug_path: pathlib.Path
) -> str:
    """Title the status site renders for this bug: frontmatter, else ``# `` heading."""
    title = (fields.get("title") or "").strip()
    if title:
        return title
    return _bug_title_from_heading(_body_from(bug_path), report_id)


def _post_transition(
    report_id: int,
    title: str,
    msg: str,
    *,
    post_reply: Callable[[str, str], None] | None = None,
) -> None:
    """Post one line to the bug's status-site thread. Best-effort — never raises."""
    slug = _site_title_slug(f"bug-{report_id} — {title}")
    try:
        (post_reply or _status_reply_default)(slug, msg)
    except Exception:  # noqa: BLE001 — a thread post must never abort a transition
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


def _parse_depends_on(value: str) -> list[str]:
    """``depends_on: [a, b]`` / ``a, b`` frontmatter value -> deduped slugs."""
    out: list[str] = []
    for raw in (value or "").strip().strip("[]").split(","):
        slug = re.sub(r"[^a-z0-9-]", "", raw.strip().strip("'\"").lower())[:60].strip("-")
        if slug and slug not in out:
            out.append(slug)
    return out


def _unresolved_deps(
    depends_on: list[str],
    status_dir: pathlib.Path,
    bugs: list[tuple[int, pathlib.Path, dict[str, str]]],
) -> list[str]:
    """Deps not yet satisfied. ``bug-<N>`` resolves via that bug's status
    (fixed / wontfix); any other slug via ``.item-state.json`` (checked).
    Unknown slugs are unresolved so a typo blocks rather than passes."""
    bug_status = {
        f"bug-{rid}": (f.get("status") or "").strip().lower() for rid, _p, f in bugs
    }
    try:
        state = json.loads((status_dir / ".item-state.json").read_text()).get("items", {})
    except (OSError, ValueError, AttributeError):
        state = {}
    out = []
    for dep in depends_on:
        if dep in bug_status:
            if bug_status[dep] not in ("fixed", "wontfix"):
                out.append(dep)
        elif (state.get(dep) or {}).get("checked") is not True:
            out.append(dep)
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
    post_reply: Callable[[str, str], None] | None = None,
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

    # Pre-flight pricing. Compute even on free models — the frontmatter
    # column reads cleaner when the field is always present (``$0.00``
    # for local inference), and ``is_free`` keeps us from writing a
    # meaningless ``estimate_usd: 0`` next to a huge Hermes prompt
    # without context.
    worker_model, expected_out = _read_config_pricing(cfg)
    prompt_tokens = estimate_tokens(prompt)
    try:
        estimate_usd = estimate_cost(worker_model, prompt_tokens, expected_out)
    except KeyError as exc:
        # Unknown model — don't invent a price. Log, write null.
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: pricing skipped — "
            f"unknown worker model {worker_model!r}: {exc}"
        )
        estimate_usd = None

    # Issue #786 M3 — rate-limit pre-flight gate. If the shared backoff
    # file says another process just hit a 429, defer this spawn rather
    # than firing another ~20 gh calls into an already-throttled window.
    # The next tick will retry. The gate is file-local; no network call.
    rate_decision = _rate_limit_should_defer()
    if rate_decision.defer:
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: rate-limit gate "
            f"deferred spawn — {rate_decision.reason} "
            f"(retry in ~{rate_decision.retry_after_s or 0:.0f}s)"
        )
        return False, {}

    spawner = spawn or _spawn_claude_detached
    pid = spawner(report_id, scratch_dir, prompt)
    if pid is None:
        log(f"[bug-fix-dispatcher] bug-{report_id}: worker spawn failed")
        _post_transition(
            report_id,
            _site_title(report_id, fields, bug_path),
            "worker crash: spawn failed",
            post_reply=post_reply,
        )
        return False, {}

    patch: dict[str, object | None] = {
        "status": "in-progress",
        "dispatched_at": now_iso(),
        "worker_task_id": f"pid:{pid}",
        "worker_model": worker_model,
        "estimate_prompt_tokens": prompt_tokens,
        "estimate_output_tokens": expected_out,
    }
    if estimate_usd is not None:
        # Four decimal places gives sub-cent resolution for Haiku-grade
        # fixes without exploding digit count for Opus.
        patch["estimate_usd"] = f"{estimate_usd:.4f}"
    try:
        _fm.update(bug_path, patch)
    except OSError as exc:
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: frontmatter write failed after "
            f"spawn (pid {pid}): {exc}"
        )
        return False, {}
    estimate_label = (
        format_usd(estimate_usd) if estimate_usd is not None else "unknown"
    )
    log(
        f"[bug-fix-dispatcher] bug-{report_id}: dispatched worker pid={pid} "
        f"scratch={scratch_dir} model={worker_model} "
        f"estimate_usd={estimate_label}"
    )
    _post_transition(
        report_id,
        _site_title(report_id, fields, bug_path),
        f"worker-spawned pid:{pid}",
        post_reply=post_reply,
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


def _compute_actuals_patch(
    *,
    report_id: int,
    scratch_dir: pathlib.Path,
    fields: Mapping[str, str],
    cfg: DispatcherConfig,
    log: Callable[[str], None],
) -> dict[str, object | None]:
    """Build the frontmatter patch carrying ``actual_usd`` + sidecars.

    Returns a dict with (always):
        * ``actual_usd``: formatted dollars, or ``None`` on unparseable
        * ``actual_tokens``: ``"input=X output=Y"`` audit string, or ``None``
        * ``cost_attribution_note``: human-readable explanation when
          ``actual_usd`` is ``None``, or ``None`` to clear a stale note

    The returned patch is deliberately idempotent: calling twice with
    the same scratch dir writes the same values.
    """
    patch: dict[str, object | None] = {}
    tokens = read_worker_tokens(scratch_dir)
    if tokens is None:
        patch["actual_usd"] = None
        patch["actual_tokens"] = None
        patch["cost_attribution_note"] = (
            f"worker scratch ({scratch_dir}) had no parseable token "
            "counts in result.json or stderr.log"
        )
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: actuals unreadable in "
            f"{scratch_dir} — writing actual_usd=null"
        )
        return patch

    # If the worker's own envelope reports a model, trust it; else
    # fall back to the model we recorded at dispatch time (``worker_model``
    # in the frontmatter) and finally the config default.
    model = tokens.get("model") or fields.get("worker_model") or cfg.worker_model
    if is_free(model):
        patch["actual_usd"] = f"{0:.4f}"
        patch["actual_tokens"] = (
            f"input={tokens['input_tokens']} output={tokens['output_tokens']}"
        )
        patch["cost_attribution_note"] = None
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: local model ({model}) — "
            f"actual_usd=$0.00 input={tokens['input_tokens']} "
            f"output={tokens['output_tokens']}"
        )
        return patch

    try:
        actual_usd = actual_cost_from_tokens(
            model,
            tokens["input_tokens"],
            tokens["output_tokens"],
            tokens.get("cache_read_tokens", 0),
            tokens.get("cache_write_tokens", 0),
        )
    except KeyError:
        patch["actual_usd"] = None
        patch["actual_tokens"] = (
            f"input={tokens['input_tokens']} output={tokens['output_tokens']}"
        )
        patch["cost_attribution_note"] = (
            f"worker reported unknown model {model!r}; add it to "
            "alice_pricing.rates.RATES to price retroactively"
        )
        log(
            f"[bug-fix-dispatcher] bug-{report_id}: unknown model {model!r} — "
            f"writing actual_usd=null"
        )
        return patch

    patch["actual_usd"] = f"{actual_usd:.4f}"
    patch["actual_tokens"] = (
        f"input={tokens['input_tokens']} output={tokens['output_tokens']}"
    )
    cache_read = tokens.get("cache_read_tokens", 0)
    cache_write = tokens.get("cache_write_tokens", 0)
    if cache_read or cache_write:
        patch["actual_tokens"] = (
            f"input={tokens['input_tokens']} output={tokens['output_tokens']} "
            f"cache_read={cache_read} cache_write={cache_write}"
        )
    # Clear any stale attribution note from a prior tick.
    patch["cost_attribution_note"] = None
    log(
        f"[bug-fix-dispatcher] bug-{report_id}: actual_usd={format_usd(actual_usd)} "
        f"({model}, input={tokens['input_tokens']} output={tokens['output_tokens']})"
    )
    return patch


def _adopt_worker_result(
    *,
    report_id: int,
    bug_path: pathlib.Path,
    fields: Mapping[str, str],
    cfg: DispatcherConfig,
    log: Callable[[str], None],
    now_iso: Callable[[], str] = _now_utc_iso,
    post_reply: Callable[[str, str], None] | None = None,
) -> tuple[bool, bool]:
    """If a worker produced a result.json, adopt its ``pr_url`` into the frontmatter.

    Returns ``(adopted, stalled)``.

    * ``adopted`` is True when ``pr_url`` was added. Dispatcher's next
      tick will poll it.
    * ``stalled`` is True when the dispatched_at is older than the
      stall TTL and no result/pr_url has materialised. The caller
      transitions the bug to ``dispatch-failed``.

    Side effect: when the worker is finished (PR URL extracted OR
    scratch dir hasn't been touched for ``ACTUALS_SETTLE_SECONDS``),
    we also compute ``actual_usd`` and merge it into the frontmatter
    patch — one write per state transition, no silent drops.
    """
    scratch_dir = cfg.worker_scratch_root / f"bug-{report_id}"
    result_path = scratch_dir / "result.json"
    pr_url = _extract_pr_url_from_result(result_path)
    if pr_url:
        patch: dict[str, object | None] = {
            "pr_url": pr_url,
            "last_check_at": now_iso(),
        }
        # Fold actuals into the same write — if the worker emitted a
        # PR URL, its result envelope should carry a ``usage`` block.
        actuals_patch = _compute_actuals_patch(
            report_id=report_id,
            scratch_dir=scratch_dir,
            fields=fields,
            cfg=cfg,
            log=log,
        )
        patch.update(actuals_patch)
        try:
            _fm.update(bug_path, patch)
        except OSError as exc:
            log(
                f"[bug-fix-dispatcher] bug-{report_id}: failed to adopt pr_url "
                f"{pr_url}: {exc}"
            )
            return False, False
        log(f"[bug-fix-dispatcher] bug-{report_id}: adopted pr_url={pr_url}")
        _post_transition(
            report_id,
            _site_title(report_id, fields, bug_path),
            f"PR {pr_url} opened, CI pending",
            post_reply=post_reply,
        )
        return True, False

    # No pr_url yet — decide whether the worker has settled. If the
    # scratch dir hasn't been touched in ACTUALS_SETTLE_SECONDS,
    # assume the run finished without emitting a tidy PR URL (crashed,
    # produced a stderr-only report, hit the token limit) and price
    # the actuals anyway so the bug line gets a real dollar figure.
    settled = _scratch_dir_settled(scratch_dir, ACTUALS_SETTLE_SECONDS)
    actuals_already_recorded = "actual_usd" in fields
    # An unpriceable run writes ``cost_attribution_note`` instead of
    # ``actual_usd``; either field means this crash was already handled.
    first_settle = not actuals_already_recorded and "cost_attribution_note" not in fields
    if settled and not actuals_already_recorded:
        actuals_patch = _compute_actuals_patch(
            report_id=report_id,
            scratch_dir=scratch_dir,
            fields=fields,
            cfg=cfg,
            log=log,
        )
        try:
            _fm.update(bug_path, actuals_patch)
        except OSError:
            pass  # best-effort; stall sweep still fires below
        else:
            if first_settle:
                _post_transition(
                    report_id,
                    _site_title(report_id, fields, bug_path),
                    "worker crash: exited without a PR",
                    post_reply=post_reply,
                )

    dispatched = _parse_iso(fields.get("dispatched_at", ""))
    if dispatched is None:
        return False, False
    now = _parse_iso(now_iso()) or _dt.datetime.now(_dt.timezone.utc)
    age = now - dispatched
    if age >= _dt.timedelta(hours=cfg.stall_hours):
        return False, True
    return False, False


def _scratch_dir_settled(scratch_dir: pathlib.Path, settle_seconds: int) -> bool:
    """True when the newest file in ``scratch_dir`` is older than ``settle_seconds``.

    Used as the "worker is done, safe to read tokens" signal when the
    worker didn't produce a PR URL. We check the ``stderr.log`` and
    ``result.json`` mtimes; if either is still being written, we hold
    off so a mid-flight price doesn't get recorded as final.
    """
    newest = 0.0
    for name in ("result.json", "stderr.log"):
        p = scratch_dir / name
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime > newest:
            newest = st.st_mtime
    if newest == 0.0:
        return False
    age = _dt.datetime.now(_dt.timezone.utc).timestamp() - newest
    return age >= settle_seconds


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
    """Call ``gh pr view --json state,mergedAt,mergeCommit,...`` for a PR URL.

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
                "state,mergedAt,mergeCommit,url,isDraft,statusCheckRollup,reviewDecision",
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
    gh_pr_ready: Callable[[str], None] | None = None,
    body: str = "",
    post_reply: Callable[[str, str], None] | None = None,
) -> str:
    """Poll the PR and apply any resulting transition.

    Returns one of ``"fixed"``, ``"reopened"``, ``"ready"``, ``""`` (no change).
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
        _post_transition(
            report_id,
            _site_title(report_id, fields, bug_path),
            f"merged {fix_commit}, bug flipped to fixed",
            post_reply=post_reply,
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
    status = (fields.get("status") or "").strip().lower()
    if (
        status == "in-progress"
        and state == "OPEN"
        and data.get("isDraft")
        and _pr_is_green(data)
        and (data.get("reviewDecision") or "").upper() != "CHANGES_REQUESTED"
        and _reporter_trusted(fields, body, cfg)
    ):
        if _mark_pr_ready(pr_url, gh_pr_ready=gh_pr_ready):
            _fm.update(
                bug_path,
                {"status": "ready-for-review", "last_check_at": now_iso()},
            )
            log(
                f"[bug-fix-dispatcher] bug-{report_id}: draft PR green ({pr_url}) → "
                f"gh pr ready, status=ready-for-review"
            )
            return "ready"
        log(f"[bug-fix-dispatcher] bug-{report_id}: gh pr ready failed for {pr_url}")
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
    gh_pr_ready: Callable[[str], None] | None = None,
    post_reply: Callable[[str, str], None] | None = None,
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
                blockers = _unresolved_deps(
                    _parse_depends_on(fields.get("depends_on", "")),
                    cfg.status_dir,
                    bugs,
                )
                if blockers:
                    _log(
                        f"[bug-fix-dispatcher] bug-{report_id}: blocked by "
                        f"{', '.join(blockers)} — not dispatching"
                    )
                    report.skipped.append((report_id, "blocked"))
                    continue
                ok, _patch = _dispatch_worker(
                    report_id=report_id,
                    bug_path=bug_path,
                    fields=fields,
                    body=_body_from(bug_path),
                    cfg=cfg,
                    log=_log,
                    now_iso=now_iso,
                    spawn=spawn,
                    post_reply=post_reply,
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
            elif status in ("in-progress", "ready-for-review"):
                pr_url = fields.get("pr_url", "").strip()
                if status == "ready-for-review" and not pr_url:
                    report.skipped.append((report_id, "status=ready-for-review"))
                elif pr_url:
                    outcome = _check_pr_state(
                        report_id=report_id,
                        bug_path=bug_path,
                        fields=fields,
                        cfg=cfg,
                        log=_log,
                        now_iso=now_iso,
                        gh_pr_view=gh_pr_view,
                        gh_pr_ready=gh_pr_ready,
                        body=_body_from(bug_path),
                        post_reply=post_reply,
                    )
                    if outcome == "fixed":
                        report.fixed.append(report_id)
                    elif outcome == "reopened":
                        report.reopened.append(report_id)
                    elif outcome == "ready":
                        report.ready.append(report_id)
                else:
                    adopted, stalled = _adopt_worker_result(
                        report_id=report_id,
                        bug_path=bug_path,
                        fields=fields,
                        cfg=cfg,
                        log=_log,
                        now_iso=now_iso,
                        post_reply=post_reply,
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
                        _post_transition(
                            report_id,
                            _site_title(report_id, fields, bug_path),
                            f"worker no PR after {cfg.stall_hours}h, flagging",
                            post_reply=post_reply,
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

    try:
        if _rebuild_waiting_block(cfg.status_dir, cfg.index_path):
            _log(f"[bug-fix-dispatcher] rebuilt bug-review block in {cfg.index_path}")
    except OSError as exc:
        _log(f"[bug-fix-dispatcher] failed to rebuild bug-review block: {exc}")

    # P&L roll-up — sum estimate_usd / actual_usd across *all* bugs
    # (not just this tick's) so the log line is a running total Jason
    # can grep for across restarts.
    totals = _sum_pricing_totals(cfg.status_dir)
    _log(
        f"[bug-fix-dispatcher] done — scanned={report.scanned} "
        f"dispatched={len(report.dispatched)} "
        f"adopted_pr_url={len(report.adopted_pr_url)} "
        f"fixed={len(report.fixed)} reopened={len(report.reopened)} "
        f"ready={len(report.ready)} "
        f"stalled={len(report.stalled)} skipped={len(report.skipped)} "
        f"total_estimate_usd={format_usd(totals['estimate'])} "
        f"total_actual_usd={format_usd(totals['actual'])}"
    )
    return report


def _sum_pricing_totals(status_dir: pathlib.Path) -> dict[str, float]:
    """Scan ``bug-*.md`` frontmatter and sum ``estimate_usd`` / ``actual_usd``.

    Missing / unparseable fields are skipped silently (``$0.00`` for
    the pile that hasn't been priced yet). Returns
    ``{"estimate": float, "actual": float}``.
    """
    totals = {"estimate": 0.0, "actual": 0.0}
    if not status_dir.is_dir():
        return totals
    for p in sorted(status_dir.glob("bug-*.md")):
        if not BUG_FILE_RE.fullmatch(p.name):
            continue
        try:
            text = p.read_text()
        except OSError:
            continue
        fields, _body, _raw = _fm.parse(text)
        for key, bucket in (("estimate_usd", "estimate"), ("actual_usd", "actual")):
            raw = fields.get(key, "").strip()
            if not raw:
                continue
            try:
                totals[bucket] += float(raw)
            except ValueError:
                continue
    return totals


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
    parser.add_argument(
        "--worker-model",
        default=DEFAULT_WORKER_MODEL,
        help="model id priced at dispatch (alice.config.json overrides this)",
    )
    parser.add_argument(
        "--expected-output-tokens",
        type=int,
        default=DEFAULT_EXPECTED_OUTPUT_TOKENS,
        help="output budget used for pre-flight estimate_usd (default 8K)",
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
        worker_model=args.worker_model,
        expected_output_tokens=args.expected_output_tokens,
    )

    def _stderr(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    try:
        tick(cfg, log=_stderr)
    except Exception as exc:  # noqa: BLE001 — supervisor retries on next cadence
        _stderr(f"[bug-fix-dispatcher] tick crashed: {type(exc).__name__}: {exc}")
        return 1
    return 0
