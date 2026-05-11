"""Deterministic post-wake hooks.

These hooks run after the LLM-driven wake body returns, success or
error, so invariants the prompt advised the model to check no longer
depend on the model remembering. The Stage D invariant is the first
mover here: PR #40 added :func:`alice_thinking.stage_d_pipeline.commit_stage_d_synthesis`
as the structural gate for new syntheses, and
:func:`alice_thinking.stage_d_invariant.find_unaudited_stage_d_notes`
as the belt-and-suspenders scan for vault notes that bypassed the gate.
The wake prompt's step-5 instructions advised the model to invoke the
scan and file a surface on non-empty results; in practice the model
sometimes skipped that step entirely (the same prompt-forgetting failure
mode that motivated the structural commit gate). This module closes
the loop by running the scan from Python after every Stage D wake.

Mode: audit-report. The hook only **reads** the vault — it emits one
``stage_d_invariant_violation`` event per unaudited note and writes a
single surface file under ``inner/surface/`` listing all violations.
It never rewrites or deletes notes (the wake prompt at
``wake.active.md.j2:382`` is explicit on that). PR #40's commit
message frames the close-clean step as "files a surface if non-empty",
and an after-the-fact re-judge would require the original source
texts and would mutate an already-committed note, which is exactly the
behavior the prompt forbids.

Errors from the scan or the surface write log a warning and emit a
``stage_d_invariant_hook_error`` event, but never propagate — the hook
must not break wake completion.
"""

from __future__ import annotations

import datetime as _dt
import logging
import pathlib
from typing import Any

from alice_core.events import EventEmitter

from .phase import Phase

logger = logging.getLogger(__name__)


__all__ = [
    "post_stage_d_invariant_check",
]


def _format_surface_body(violations: list[dict]) -> str:
    """Render the surface body for an unaudited-notes list.

    One bullet per note, with slug, note_a, note_b, and on-disk path so
    a human review can resolve each one.
    """
    lines: list[str] = [
        "Stage D invariant check found vault notes with `source: stage-d` "
        "that have no matching attempts.jsonl ship line and no matching "
        "judge-failures.jsonl fallback line. Each note bypassed the "
        "`commit_stage_d_synthesis` gate.",
        "",
        "Do not delete or rewrite the offending notes — they may still "
        "be useful content. Review each, decide whether to keep, "
        "re-judge (re-run through the gate with the captured pair), or "
        "archive.",
        "",
        "Unaudited notes:",
        "",
    ]
    for v in violations:
        slug = v.get("slug") or "(unknown)"
        note_a = v.get("note_a") or "(missing)"
        note_b = v.get("note_b") or "(missing)"
        created = v.get("created") or "(missing)"
        path = v.get("path") or "(missing)"
        lines.append(
            f"- **{slug}** (created: {created})\n"
            f"  - note_a: `{note_a}`\n"
            f"  - note_b: `{note_b}`\n"
            f"  - path: `{path}`"
        )
    lines.append("")
    return "\n".join(lines)


def _write_surface(
    *,
    mind: pathlib.Path,
    violations: list[dict],
    now: _dt.datetime,
) -> pathlib.Path:
    """Write one surface listing all violations.

    Filename matches the speaking-side surface convention used elsewhere
    in the wake prompt: ``<YYYY-MM-DD-HHMMSS>-stage-d-invariant.md``
    under ``inner/surface/``. Frontmatter mirrors the standard surface
    shape (``priority`` / ``context`` / ``reply_expected``) plus
    ``surface_type`` for downstream filtering.
    """
    surface_dir = mind / "inner" / "surface"
    surface_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    path = surface_dir / f"{stamp}-stage-d-invariant.md"
    body = _format_surface_body(violations)
    count = len(violations)
    content = (
        "---\n"
        "priority: insight\n"
        "surface_type: stage-d-invariant\n"
        f"violation_count: {count}\n"
        "context: post-wake Stage D invariant scan found unaudited notes\n"
        "reply_expected: false\n"
        "---\n"
        "\n"
        f"{body}"
    )
    path.write_text(content, encoding="utf-8")
    return path


def post_stage_d_invariant_check(
    *,
    mind: pathlib.Path,
    emitter: EventEmitter,
    phase: Phase,
    now: _dt.datetime | None = None,
    find_unaudited_fn: Any = None,
) -> None:
    """Run the Stage D invariant scan after a Stage D wake completes.

    Always called from ``wake.py``'s finally block when
    ``phase == Phase.SLEEP_D`` — including on wake error. That's the
    "deterministic" part: the LLM can't skip it because the LLM isn't
    the one calling it.

    Parameters
    ----------
    mind
        Path to ``~/alice-mind`` (or test stand-in). Vault lives at
        ``<mind>/cortex-memory/``; surfaces land at
        ``<mind>/inner/surface/``.
    emitter
        Event sink (``EventLogger`` in production, ``CapturingEmitter``
        in tests).
    phase
        The wake's phase. The hook only does work for ``Phase.SLEEP_D``;
        it's safe to call for any phase (no-op for non-D).
    now
        Override for the surface-filename timestamp. ``None`` → wall
        clock.
    find_unaudited_fn
        Test injection seam. ``None`` → the production
        ``find_unaudited_stage_d_notes`` callable.

    Side effects
    ------------
    - One ``stage_d_invariant_violation`` event per unaudited note.
    - On non-empty violations: one ``stage-d-invariant`` surface file
      written under ``<mind>/inner/surface/``.
    - On empty violations: a single ``stage_d_invariant_clean`` event
      so the audit trail records that the scan ran.
    - On any internal failure: a ``stage_d_invariant_hook_error`` event
      with the exception type + message. Never raises.
    """
    if phase != Phase.SLEEP_D:
        return

    now = now or _dt.datetime.now().astimezone()

    try:
        if find_unaudited_fn is None:
            from .stage_d_invariant import find_unaudited_stage_d_notes as _scan
        else:
            _scan = find_unaudited_fn

        # ``date.min`` lets the scan look at every dated stage-d note,
        # not just today's. The wake fires once per Stage D wake and
        # may be the first scan since a hand-edit days ago — a today-only
        # filter would silently let prior violations linger.
        violations = _scan(
            date=_dt.date.min,
            vault_root=mind / "cortex-memory",
        )
    except Exception as exc:  # noqa: BLE001 — hook must not propagate
        logger.warning(
            "stage_d_invariant scan failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        try:
            emitter.emit(
                "stage_d_invariant_hook_error",
                phase=phase.value,
                stage="scan",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        except Exception:  # noqa: BLE001 — emitter must not break wake
            pass
        return

    if not violations:
        try:
            emitter.emit("stage_d_invariant_clean", phase=phase.value)
        except Exception:  # noqa: BLE001
            pass
        return

    # Per-violation events first — these are easy to grep in the log.
    for v in violations:
        try:
            emitter.emit(
                "stage_d_invariant_violation",
                phase=phase.value,
                slug=v.get("slug"),
                note_a=v.get("note_a"),
                note_b=v.get("note_b"),
                created=v.get("created"),
                path=v.get("path"),
            )
        except Exception:  # noqa: BLE001
            # Keep going — losing one event line is better than
            # aborting the whole hook.
            continue

    # Single rolled-up surface for human review.
    try:
        surface_path = _write_surface(
            mind=mind, violations=violations, now=now
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "stage_d_invariant surface write failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        try:
            emitter.emit(
                "stage_d_invariant_hook_error",
                phase=phase.value,
                stage="surface_write",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        emitter.emit(
            "stage_d_invariant_surface_written",
            phase=phase.value,
            surface_path=str(surface_path),
            violation_count=len(violations),
        )
    except Exception:  # noqa: BLE001
        pass
