"""Stage B step bodies — deterministic helpers + the seven step funcs.

Each step is a plain async (or sync) function that takes a
:class:`WakeState` and mutates it in place. The ADK wiring in
:mod:`agents.py` wraps each step in a BaseAgent subclass; tests can
call the step bodies directly without any ADK runtime.

Per-step contracts mirror the design sketch
(``docs/designs/stage-b-adk-workflow-sketch.md``):

1. read_wake_state — deterministic; filesystem reads only.
2. drain_inbox — LLM per note, deterministic apply.
3. pick_grooming_target — deterministic scoring.
4. groom_target — LLM produces typed Diff, deterministic apply.
5. side_checks — three parallel branches via ADK ParallelAgent.
6. emit_surfaces — deterministic.
7. close — deterministic; writes wake summary, runs prune.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import pathlib
import re
import shutil
import time
from typing import Any, Optional

from .scoring import score_candidates
from .subroutines import (
    ModelCall,
    classify_and_route_note,
    coerce_surface_payload,
    conflict_scan as _conflict_scan_subroutine,
    produce_grooming_diff,
    shadow_neighbor_tldr,
    stale_finding_lint as _stale_finding_lint_subroutine,
)
from .types import (
    Action,
    AppendToDaily,
    CreateConflictNote,
    Diff,
    Discard,
    FrontmatterChange,
    InboxResult,
    PromoteToVault,
    RouteToSurface,
    SideCheckResult,
    SideCheckResults,
    StepResult,
    SurfacePayload,
    WakeState,
    WakeSummary,
)


__all__ = [
    "read_wake_state",
    "drain_inbox",
    "apply_action",
    "consume_note",
    "pick_grooming_target",
    "groom_target",
    "apply_diff",
    "side_checks",
    "emit_surfaces",
    "close",
    "stale_finding_lint",
    "shadow_neighbor",
    "conflict_scan",
]


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)((?:#[^\]|]*)?)((?:\|[^\]]*)?)\]\]")


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        out[key.strip()] = raw.strip().strip('"').strip("'")
    return out


def _ensure_dir(p: pathlib.Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Step 1 — read_wake_state
# ---------------------------------------------------------------------------


def _list_inbox(mind_dir: pathlib.Path) -> list[pathlib.Path]:
    notes = mind_dir / "inner" / "notes"
    if not notes.is_dir():
        return []
    out: list[pathlib.Path] = []
    try:
        for entry in os.scandir(notes):
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.name.endswith((".md", ".markdown")):
                out.append(pathlib.Path(entry.path))
    except OSError:
        return []
    out.sort(key=lambda p: p.name)
    return out


def _load_active_thread(mind_dir: pathlib.Path) -> Optional[str]:
    p = mind_dir / "inner" / "state" / "active-thread.md"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _load_latest_vault_health(mind_dir: pathlib.Path) -> Optional[dict[str, Any]]:
    p = mind_dir / "memory" / "events.jsonl"
    if not p.is_file():
        return None
    latest: Optional[dict[str, Any]] = None
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    blob = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (blob.get("event") or blob.get("type")) == "vault_health":
                    latest = blob
    except OSError:
        return None
    return latest


def read_wake_state(
    *,
    mind_dir: pathlib.Path,
    state_dir: pathlib.Path,
    wake_file_path: Optional[pathlib.Path],
    now: Optional[_dt.datetime] = None,
    apply_writes: bool = True,
) -> WakeState:
    """Step 1 — read filesystem state into a typed :class:`WakeState`."""
    now = now or _dt.datetime.now()
    mode = "sleep_b"
    if wake_file_path is not None and wake_file_path.is_file():
        try:
            fm = _parse_frontmatter(wake_file_path.read_text(encoding="utf-8"))
            if "mode" in fm:
                mode = fm["mode"]
        except OSError:
            pass
    return WakeState(
        mind_dir=mind_dir,
        state_dir=state_dir,
        wake_file_path=wake_file_path,
        mode=mode,
        now=now,
        apply_writes=apply_writes,
        inbox_files=_list_inbox(mind_dir),
        vault_health=_load_latest_vault_health(mind_dir),
        active_thread=_load_active_thread(mind_dir),
    )


# ---------------------------------------------------------------------------
# Step 2 — drain_inbox + apply_action + consume_note
# ---------------------------------------------------------------------------


def apply_action(
    action: Action,
    *,
    mind_dir: pathlib.Path,
    today: _dt.date,
) -> Optional[SurfacePayload]:
    """Apply one classified action. Returns a SurfacePayload when the
    action is RouteToSurface (collected by Step 6); other actions write
    inline.
    """
    if isinstance(action, PromoteToVault):
        target = mind_dir / action.target_path
        _ensure_dir(target.parent)
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            if not existing.endswith("\n"):
                existing += "\n"
            target.write_text(existing + "\n" + action.new_content, encoding="utf-8")
        else:
            target.write_text(action.new_content, encoding="utf-8")
        return None
    if isinstance(action, AppendToDaily):
        daily_dir = mind_dir / "cortex-memory" / "dailies"
        _ensure_dir(daily_dir)
        daily = daily_dir / f"{today.isoformat()}.md"
        with daily.open("a", encoding="utf-8") as f:
            f.write(f"- {action.line.strip()}\n")
        return None
    if isinstance(action, CreateConflictNote):
        conflicts_dir = mind_dir / "cortex-memory" / "conflicts"
        _ensure_dir(conflicts_dir)
        slug = re.sub(r"[^a-z0-9-]+", "-", action.slug.lower()).strip("-") or "conflict"
        path = conflicts_dir / f"{today.isoformat()}-{slug}.md"
        if path.exists():
            path = conflicts_dir / f"{today.isoformat()}-{slug}-{int(time.time())}.md"
        path.write_text(action.body, encoding="utf-8")
        return None
    if isinstance(action, RouteToSurface):
        return coerce_surface_payload(action.surface_payload)
    if isinstance(action, Discard):
        return None
    raise TypeError(f"unknown action type: {type(action).__name__}")


def consume_note(note_path: pathlib.Path, *, today: _dt.date) -> pathlib.Path:
    consumed_root = note_path.parent / ".consumed" / today.isoformat()
    _ensure_dir(consumed_root)
    target = consumed_root / note_path.name
    if target.exists():
        target = consumed_root / f"{note_path.stem}-{int(time.time())}{note_path.suffix}"
    shutil.move(str(note_path), str(target))
    return target


async def drain_inbox(
    state: WakeState,
    *,
    model_call: ModelCall,
    vault_index: Optional[dict[str, Any]] = None,
    apply_writes: Optional[bool] = None,
) -> InboxResult:
    """Step 2 — classify each inbox note, apply, consume.

    Per-note errors collect into ``per_note_errors`` and the loop
    continues. ``apply_writes=False`` (or state.apply_writes=False)
    disables filesystem mutation.
    """
    if apply_writes is None:
        apply_writes = state.apply_writes
    actions: list[Action] = []
    consumed: list[pathlib.Path] = []
    surface_payloads: list[SurfacePayload] = []
    errors: list[str] = []
    today = state.now.date()
    for note_path in list(state.inbox_files):
        try:
            note_body = note_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{note_path.name}: read failed — {exc}")
            continue
        try:
            action = await classify_and_route_note(
                note_path=note_path,
                note_body=note_body,
                vault_index=vault_index,
                model_call=model_call,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{note_path.name}: classify failed — {exc}")
            continue
        actions.append(action)
        if not apply_writes:
            continue
        try:
            surface = apply_action(action, mind_dir=state.mind_dir, today=today)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{note_path.name}: apply failed — {exc}")
            continue
        if surface is not None:
            surface_payloads.append(surface)
        try:
            consumed.append(consume_note(note_path, today=today))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{note_path.name}: consume failed — {exc}")

    state.inbox_actions.extend(actions)
    state.surface_payloads.extend(surface_payloads)
    return InboxResult(
        actions=actions,
        consumed_paths=consumed,
        surface_payloads=surface_payloads,
        per_note_errors=errors,
    )


# ---------------------------------------------------------------------------
# Step 3 — pick_grooming_target
# ---------------------------------------------------------------------------


def pick_grooming_target(state: WakeState) -> Optional[pathlib.Path]:
    candidates = score_candidates(
        vault_dir=state.mind_dir / "cortex-memory",
        consumed_root=state.mind_dir / "inner" / "notes" / ".consumed",
        now=state.now,
    )
    if not candidates:
        return None
    state.grooming_target = candidates[0].path
    return candidates[0].path


# ---------------------------------------------------------------------------
# Step 4 — groom_target + apply_diff
# ---------------------------------------------------------------------------


def _serialize_frontmatter(fm: dict[str, str]) -> str:
    if not fm:
        return ""
    lines = ["---"]
    for k, v in fm.items():
        if v is None:
            continue
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        fm[key.strip()] = raw.strip()
    return fm, text[m.end():]


def _replace_section(body: str, heading: str, new_section_body: str) -> str:
    target = heading.strip().lower()
    lines = body.splitlines(keepends=True)
    section_re = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
    out_lines: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        m = section_re.match(line.rstrip("\n"))
        if m and m.group(2).strip().lower() == target and not replaced:
            level = len(m.group(1))
            out_lines.append(line)
            j = i + 1
            while j < len(lines):
                nxt = section_re.match(lines[j].rstrip("\n"))
                if nxt and len(nxt.group(1)) <= level:
                    break
                j += 1
            payload = new_section_body.rstrip("\n") + "\n"
            if not payload.startswith("\n"):
                payload = "\n" + payload
            out_lines.append(payload)
            i = j
            replaced = True
            continue
        out_lines.append(line)
        i += 1
    return "".join(out_lines)


def _replace_wikilinks(body: str, old: str, new: str) -> str:
    old_norm = old.strip()

    def _sub(m: re.Match[str]) -> str:
        if m.group(1).strip() == old_norm:
            return f"[[{new}{m.group(2)}{m.group(3)}]]"
        return m.group(0)

    return _WIKILINK_RE.sub(_sub, body)


def apply_diff(target_path: pathlib.Path, diff: Diff) -> bool:
    if diff.is_empty():
        return False
    original = target_path.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(original)
    for change in diff.frontmatter_changes:
        if change.new_value is None:
            fm.pop(change.key, None)
        else:
            fm[change.key] = change.new_value
    for edit in diff.section_edits:
        body = _replace_section(body, edit.heading, edit.new_body)
    for fix in diff.wikilink_fixes:
        body = _replace_wikilinks(body, fix.old_target, fix.new_target)
    new_text = _serialize_frontmatter(fm) + body
    if new_text == original:
        return False
    target_path.write_text(new_text, encoding="utf-8")
    return True


async def groom_target(
    state: WakeState,
    target: Optional[pathlib.Path],
    *,
    model_call: ModelCall,
    vault_index: Optional[dict[str, Any]] = None,
    apply_writes: Optional[bool] = None,
) -> Optional[Diff]:
    if apply_writes is None:
        apply_writes = state.apply_writes
    if target is None:
        return None
    try:
        current = target.read_text(encoding="utf-8")
    except OSError:
        return None
    diff = await produce_grooming_diff(
        target_path=target,
        current_content=current,
        vault_index=vault_index,
        model_call=model_call,
    )
    if diff.is_empty():
        return None
    state.grooming_diff = diff
    if not apply_writes:
        return diff
    try:
        apply_diff(target, diff)
    except OSError:
        return diff
    return diff


# ---------------------------------------------------------------------------
# Step 5 — side_checks (parallel)
# ---------------------------------------------------------------------------


async def stale_finding_lint(
    state: WakeState,
    target: Optional[pathlib.Path],
    *,
    model_call: ModelCall,
) -> SideCheckResult:
    if target is None or not target.is_file():
        return SideCheckResult(
            name="stale_finding_lint", ok=True, action_summary="no target"
        )
    try:
        body = target.read_text(encoding="utf-8")
    except OSError as exc:
        return SideCheckResult(name="stale_finding_lint", ok=False, error=str(exc))
    neighbors: list[str] = []
    try:
        for sib in sorted(target.parent.glob("*.md")):
            if sib == target:
                continue
            try:
                neighbors.append(sib.read_text(encoding="utf-8")[:500])
            except OSError:
                continue
            if len(neighbors) >= 3:
                break
    except OSError:
        pass
    try:
        result = await _stale_finding_lint_subroutine(
            note_path=target,
            note_body=body,
            neighbor_snippets=neighbors,
            model_call=model_call,
        )
    except Exception as exc:  # noqa: BLE001
        return SideCheckResult(name="stale_finding_lint", ok=False, error=str(exc))
    return SideCheckResult(
        name="stale_finding_lint",
        ok=True,
        action_summary=f"{result.get('verdict')}: {result.get('summary', '')}",
    )


async def shadow_neighbor(
    state: WakeState,
    target: Optional[pathlib.Path],
    *,
    model_call: ModelCall,
    apply_writes: Optional[bool] = None,
) -> SideCheckResult:
    if apply_writes is None:
        apply_writes = state.apply_writes
    if target is None or not target.is_file():
        return SideCheckResult(
            name="shadow_neighbor", ok=True, action_summary="no target"
        )
    try:
        body = target.read_text(encoding="utf-8")
    except OSError as exc:
        return SideCheckResult(name="shadow_neighbor", ok=False, error=str(exc))
    targets = {m.group(1).strip() for m in _WIKILINK_RE.finditer(body)}
    if not targets:
        return SideCheckResult(
            name="shadow_neighbor", ok=True, action_summary="no neighbors"
        )
    vault_dir = state.mind_dir / "cortex-memory"
    dormant: Optional[pathlib.Path] = None
    dormant_body = ""
    for slug in sorted(targets):
        for candidate in vault_dir.rglob(f"{slug}.md"):
            try:
                content = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _parse_frontmatter(content)
            try:
                ac = int(fm.get("access_count", "0") or 0)
            except ValueError:
                ac = 0
            if ac == 0:
                dormant = candidate
                dormant_body = content
                break
        if dormant is not None:
            break
    if dormant is None:
        return SideCheckResult(
            name="shadow_neighbor", ok=True, action_summary="no dormant"
        )
    try:
        result = await shadow_neighbor_tldr(
            neighbor_path=dormant,
            neighbor_body=dormant_body,
            hub_path=target,
            hub_body=body,
            model_call=model_call,
        )
    except Exception as exc:  # noqa: BLE001
        return SideCheckResult(name="shadow_neighbor", ok=False, error=str(exc))
    tldr = (result.get("tldr") or "").strip()
    if not tldr or not apply_writes:
        return SideCheckResult(
            name="shadow_neighbor",
            ok=True,
            action_summary="bumped"
            if not tldr
            else f"would write tldr to {dormant.name}",
        )
    fm, _ = _split_frontmatter(dormant_body)
    try:
        ac = int(fm.get("access_count", "0") or 0)
    except ValueError:
        ac = 0
    diff = Diff(
        frontmatter_changes=[
            FrontmatterChange(key="access_count", new_value=str(ac + 1)),
            FrontmatterChange(key="tldr", new_value=tldr),
        ],
        rationale="shadow_neighbor bump",
    )
    apply_diff(dormant, diff)
    return SideCheckResult(
        name="shadow_neighbor", ok=True, action_summary=f"bumped {dormant.name}"
    )


async def conflict_scan(
    state: WakeState,
    target: Optional[pathlib.Path],
    *,
    model_call: ModelCall,
) -> SideCheckResult:
    if target is None or not target.is_file():
        return SideCheckResult(name="conflict_scan", ok=True, action_summary="no target")
    try:
        body = target.read_text(encoding="utf-8")
    except OSError as exc:
        return SideCheckResult(name="conflict_scan", ok=False, error=str(exc))
    neighbors: list[tuple[pathlib.Path, str]] = []
    seen: set[str] = set()
    for m in _WIKILINK_RE.finditer(body):
        slug = m.group(1).strip()
        if slug in seen:
            continue
        seen.add(slug)
        for candidate in (state.mind_dir / "cortex-memory").rglob(f"{slug}.md"):
            if "/dailies/" in str(candidate) or "/conflicts/" in str(candidate):
                continue
            try:
                cb = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            neighbors.append((candidate, cb))
            break
        if len(neighbors) >= 2:
            break
    if not neighbors:
        return SideCheckResult(
            name="conflict_scan", ok=True, action_summary="no neighbors"
        )
    try:
        result = await _conflict_scan_subroutine(
            target_path=target,
            target_body=body,
            neighbor_pairs=neighbors,
            model_call=model_call,
        )
    except Exception as exc:  # noqa: BLE001
        return SideCheckResult(name="conflict_scan", ok=False, error=str(exc))
    verdict = result.get("verdict")
    summary = str(result.get("summary", ""))
    payloads: list[SurfacePayload] = []
    if verdict == "conflict":
        payloads.append(
            SurfacePayload(
                surface_type="stage-b-conflict",
                body=summary,
                extra_frontmatter={
                    "target": str(target),
                    "slug": str(result.get("slug", "")),
                },
            )
        )
    return SideCheckResult(
        name="conflict_scan",
        ok=True,
        action_summary=f"{verdict}: {summary}",
        surface_payloads=payloads,
    )


async def side_checks(
    state: WakeState,
    target: Optional[pathlib.Path],
    *,
    model_call: ModelCall,
    branch_timeout_s: float = 30.0,
    apply_writes: Optional[bool] = None,
) -> SideCheckResults:
    """Step 5 — three branches in parallel via ``asyncio.gather`` (the
    ADK ``ParallelAgent`` wrapping in :mod:`agents` uses the same
    semantics; this function is the seam tests call directly).
    """
    if apply_writes is None:
        apply_writes = state.apply_writes

    async def _wrap(name: str, coro):
        try:
            return await asyncio.wait_for(coro, timeout=branch_timeout_s)
        except asyncio.TimeoutError:
            return SideCheckResult(name=name, ok=False, error="timeout")
        except Exception as exc:  # noqa: BLE001
            return SideCheckResult(name=name, ok=False, error=str(exc))

    sfl, sn, cs = await asyncio.gather(
        _wrap("stale_finding_lint",
              stale_finding_lint(state, target, model_call=model_call)),
        _wrap("shadow_neighbor",
              shadow_neighbor(state, target, model_call=model_call,
                              apply_writes=apply_writes)),
        _wrap("conflict_scan",
              conflict_scan(state, target, model_call=model_call)),
    )
    results = SideCheckResults(
        stale_finding_lint=sfl, shadow_neighbor=sn, conflict_scan=cs
    )
    state.side_check_results = results
    for r in results.all():
        state.surface_payloads.extend(r.surface_payloads)
    return results


# ---------------------------------------------------------------------------
# Step 6 — emit_surfaces
# ---------------------------------------------------------------------------


def emit_surfaces(state: WakeState, *, apply_writes: Optional[bool] = None) -> int:
    if apply_writes is None:
        apply_writes = state.apply_writes
    if not state.surface_payloads:
        return 0
    if not apply_writes:
        return len(state.surface_payloads)
    from alice_thinking.design_pipeline import write_surface

    written = 0
    for payload in state.surface_payloads:
        try:
            write_surface(
                state.mind_dir,
                surface_type=payload.surface_type,
                body=payload.body,
                now=state.now,
                extra_frontmatter=payload.extra_frontmatter,
            )
            written += 1
        except OSError:
            continue
    return written


# ---------------------------------------------------------------------------
# Step 7 — close
# ---------------------------------------------------------------------------


def _format_action(action: Action) -> str:
    if isinstance(action, PromoteToVault):
        return f"promote → {action.target_path}"
    if isinstance(action, AppendToDaily):
        return f"daily ← {action.line[:80]}"
    if isinstance(action, CreateConflictNote):
        return f"conflict ← {action.slug}"
    if isinstance(action, RouteToSurface):
        return f"surface ← {action.surface_payload.get('surface_type', '?')}"
    if isinstance(action, Discard):
        return f"discard ({action.reason or '?'})"
    return type(action).__name__


def _build_summary_markdown(state: WakeState, results: list[StepResult]) -> str:
    lines: list[str] = ["---"]
    lines.append(f"mode: {state.mode}")
    lines.append(f"timestamp: {state.now.isoformat()}")
    lines.append(f"steps_ok: {sum(1 for r in results if r.ok)}")
    lines.append(f"steps_failed: {sum(1 for r in results if not r.ok)}")
    lines.append("---\n")
    lines.append(f"# Stage B wake — {state.now.strftime('%Y-%m-%d %H:%M')}\n")
    lines.append("## Steps\n")
    for r in results:
        status = "ok" if r.ok else f"FAIL ({r.error})"
        lines.append(f"- **{r.step}** — {status} ({r.duration_ms} ms)")
        if r.details:
            for k, v in r.details.items():
                lines.append(f"    - {k}: {v}")
    lines.append("\n## Inbox actions\n")
    if state.inbox_actions:
        for a in state.inbox_actions:
            lines.append(f"- {_format_action(a)}")
    else:
        lines.append("- (none)")
    if state.grooming_target is not None:
        lines.append(f"\n## Grooming target\n\n- {state.grooming_target}")
    if state.grooming_diff is not None:
        lines.append(f"  - rationale: {state.grooming_diff.rationale}")
    lines.append("\n## Side checks\n")
    if state.side_check_results is not None:
        for sc in state.side_check_results.all():
            status = "ok" if sc.ok else f"FAIL ({sc.error})"
            lines.append(f"- {sc.name}: {status} — {sc.action_summary or ''}")
    else:
        lines.append("- (skipped)")
    if state.errors:
        lines.append("\n## Errors\n")
        for e in state.errors:
            lines.append(f"- {e.step}: {e.error_type} — {e.message}")
    return "\n".join(lines) + "\n"


def _prune_rolling(mind_dir: pathlib.Path, *, today: _dt.date) -> None:
    """CLAUDE.md Step 5 housekeeping — rolling deletes."""
    targets = [
        (mind_dir / "inner" / "thoughts", 7),
        (mind_dir / "inner" / "surface" / ".handled", 30),
        (mind_dir / "inner" / "notes" / ".consumed", 30),
    ]
    for root, days in targets:
        if not root.is_dir():
            continue
        cutoff = today - _dt.timedelta(days=days)
        try:
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                try:
                    when = _dt.date.fromisoformat(entry.name)
                except ValueError:
                    continue
                if when < cutoff:
                    try:
                        shutil.rmtree(entry)
                    except OSError:
                        continue
        except OSError:
            continue


def close(
    state: WakeState,
    results: list[StepResult],
    *,
    duration_ms: int,
    apply_writes: Optional[bool] = None,
    run_prune: bool = True,
) -> WakeSummary:
    if apply_writes is None:
        apply_writes = state.apply_writes
    summary_path: Optional[pathlib.Path] = None
    if apply_writes:
        thoughts_dir = state.mind_dir / "inner" / "thoughts" / state.now.date().isoformat()
        try:
            thoughts_dir.mkdir(parents=True, exist_ok=True)
            summary_path = thoughts_dir / f"{state.now.strftime('%H%M%S')}-wake.md"
            summary_path.write_text(
                _build_summary_markdown(state, results), encoding="utf-8"
            )
        except OSError:
            summary_path = None
        if run_prune:
            _prune_rolling(state.mind_dir, today=state.now.date())

    surfaces_count = 0
    for r in results:
        if r.step == "emit_surfaces":
            surfaces_count = int(r.details.get("count", 0) or 0)
            break
    return WakeSummary(
        steps=list(results),
        actions_total=len(state.inbox_actions),
        surfaces_emitted=surfaces_count,
        duration_ms=duration_ms,
        errors=list(state.errors),
        summary_path=summary_path,
    )
