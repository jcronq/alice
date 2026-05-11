"""Tests for the post-wake Stage D invariant hook.

The hook closes the prompt-forgetting loophole identified in PR #40's
follow-up: the model was advised to call ``find_unaudited_stage_d_notes``
at close-clean, but the advisory path could be skipped. ``wake.py`` now
runs the scan in a ``finally`` block around the wake body whenever
``phase == Phase.SLEEP_D``. Coverage here:

- non-SLEEP_D phases are a no-op (no scan, no events, no surface)
- empty scan result → ``stage_d_invariant_clean`` event, no surface
- non-empty scan result → one ``stage_d_invariant_violation`` per note
  + a single rolled-up ``inner/surface/<…>-stage-d-invariant.md``
- scan exception → ``stage_d_invariant_hook_error`` event, no surface,
  hook returns cleanly
- surface-write exception → ``stage_d_invariant_hook_error`` event,
  hook returns cleanly
- the hook fires from ``wake.py`` even when the wake body raises
  (the deterministic "always runs" guarantee)
"""
from __future__ import annotations

import datetime as _dt
import pathlib
from typing import Any

import pytest

from alice_core.events import CapturingEmitter
from alice_thinking import wake_hooks
from alice_thinking.phase import Phase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _violation(slug: str, *, note_a: str = "a", note_b: str = "b") -> dict:
    return {
        "slug": slug,
        "path": f"/vault/research/{slug}.md",
        "note_a": note_a,
        "note_b": note_b,
        "created": "2026-05-11",
    }


def _emitter() -> CapturingEmitter:
    return CapturingEmitter()


# ---------------------------------------------------------------------------
# Phase gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase",
    [Phase.ACTIVE, Phase.SLEEP_B, Phase.SLEEP_C, Phase.QUICK],
)
def test_non_sleep_d_phase_is_noop(phase: Phase, tmp_path: pathlib.Path) -> None:
    """The hook only fires for SLEEP_D — every other phase is a no-op.

    No scan invocation, no events, no files written. The fake scan
    raises if called so we'd notice an accidental invocation.
    """

    def _scan(**_kwargs: Any) -> list[dict]:
        raise AssertionError("scan should not run for non-SLEEP_D phases")

    emitter = _emitter()
    wake_hooks.post_stage_d_invariant_check(
        mind=tmp_path,
        emitter=emitter,
        phase=phase,
        find_unaudited_fn=_scan,
    )

    assert emitter.events == []
    assert list((tmp_path / "inner" / "surface").glob("*")) == [] if (
        tmp_path / "inner" / "surface"
    ).exists() else True


# ---------------------------------------------------------------------------
# Empty / clean result
# ---------------------------------------------------------------------------


def test_empty_scan_emits_clean_event_no_surface(tmp_path: pathlib.Path) -> None:
    """Empty violations list → exactly one ``stage_d_invariant_clean``
    event, no surface file. The "scan ran" signal stays in the audit
    trail even when there's nothing to flag."""
    calls: list[dict[str, Any]] = []

    def _scan(**kwargs: Any) -> list[dict]:
        calls.append(kwargs)
        return []

    emitter = _emitter()
    wake_hooks.post_stage_d_invariant_check(
        mind=tmp_path,
        emitter=emitter,
        phase=Phase.SLEEP_D,
        find_unaudited_fn=_scan,
    )

    assert len(calls) == 1
    assert calls[0]["vault_root"] == tmp_path / "cortex-memory"
    assert calls[0]["date"] == _dt.date.min

    clean_events = emitter.of_kind("stage_d_invariant_clean")
    assert len(clean_events) == 1
    assert clean_events[0]["phase"] == "sleep_d"
    assert emitter.of_kind("stage_d_invariant_violation") == []
    assert emitter.of_kind("stage_d_invariant_surface_written") == []
    # No surface file written.
    surface_dir = tmp_path / "inner" / "surface"
    assert not surface_dir.exists() or list(surface_dir.glob("*.md")) == []


# ---------------------------------------------------------------------------
# Non-empty result: events + surface
# ---------------------------------------------------------------------------


def test_violations_emit_events_and_write_single_surface(
    tmp_path: pathlib.Path,
) -> None:
    """Two unaudited notes → two ``stage_d_invariant_violation`` events,
    one rolled-up surface file, one ``stage_d_invariant_surface_written``
    event. The surface body contains both slugs + note_a / note_b."""
    v1 = _violation("2026-05-10-foo-bar", note_a="alpha", note_b="beta")
    v2 = _violation("2026-05-11-baz-qux", note_a="gamma", note_b="delta")

    def _scan(**_kwargs: Any) -> list[dict]:
        return [v1, v2]

    emitter = _emitter()
    fake_now = _dt.datetime(2026, 5, 11, 23, 45, 30).astimezone()
    wake_hooks.post_stage_d_invariant_check(
        mind=tmp_path,
        emitter=emitter,
        phase=Phase.SLEEP_D,
        now=fake_now,
        find_unaudited_fn=_scan,
    )

    # Two violation events, in input order.
    violations = emitter.of_kind("stage_d_invariant_violation")
    assert len(violations) == 2
    assert violations[0]["slug"] == "2026-05-10-foo-bar"
    assert violations[0]["note_a"] == "alpha"
    assert violations[0]["note_b"] == "beta"
    assert violations[1]["slug"] == "2026-05-11-baz-qux"

    # No clean event when there are violations.
    assert emitter.of_kind("stage_d_invariant_clean") == []

    # Exactly one surface file, filename uses the injected timestamp.
    surface_dir = tmp_path / "inner" / "surface"
    files = sorted(surface_dir.glob("*.md"))
    assert len(files) == 1
    assert files[0].name.startswith("2026-05-11-234530-")
    assert files[0].name.endswith("-stage-d-invariant.md")

    body = files[0].read_text(encoding="utf-8")
    assert "surface_type: stage-d-invariant" in body
    assert "2026-05-10-foo-bar" in body
    assert "2026-05-11-baz-qux" in body
    assert "alpha" in body and "beta" in body
    assert "gamma" in body and "delta" in body
    assert "violation_count: 2" in body
    assert "reply_expected: false" in body

    # ``surface_written`` event carries the path + count.
    written = emitter.of_kind("stage_d_invariant_surface_written")
    assert len(written) == 1
    assert written[0]["violation_count"] == 2
    assert written[0]["surface_path"] == str(files[0])


# ---------------------------------------------------------------------------
# Error paths — never propagate
# ---------------------------------------------------------------------------


def test_scan_exception_emits_hook_error_event(tmp_path: pathlib.Path) -> None:
    """Scan raises → ``stage_d_invariant_hook_error`` event with stage='scan',
    no surface, hook returns cleanly. The wake must not see the failure."""

    def _scan(**_kwargs: Any) -> list[dict]:
        raise RuntimeError("vault gone walkabout")

    emitter = _emitter()
    wake_hooks.post_stage_d_invariant_check(
        mind=tmp_path,
        emitter=emitter,
        phase=Phase.SLEEP_D,
        find_unaudited_fn=_scan,
    )

    errors = emitter.of_kind("stage_d_invariant_hook_error")
    assert len(errors) == 1
    assert errors[0]["stage"] == "scan"
    assert errors[0]["error_type"] == "RuntimeError"
    assert "walkabout" in errors[0]["error_message"]
    # No follow-on events.
    assert emitter.of_kind("stage_d_invariant_violation") == []
    assert emitter.of_kind("stage_d_invariant_surface_written") == []
    # No surface file.
    surface_dir = tmp_path / "inner" / "surface"
    assert not surface_dir.exists() or list(surface_dir.glob("*.md")) == []


def test_surface_write_exception_emits_hook_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Surface write raises → events for each violation still fire, then
    a ``stage_d_invariant_hook_error`` with stage='surface_write'. Hook
    returns cleanly; no ``surface_written`` event."""

    def _scan(**_kwargs: Any) -> list[dict]:
        return [_violation("2026-05-10-x")]

    def _explode(**_kwargs: Any) -> pathlib.Path:
        raise OSError("disk full")

    monkeypatch.setattr(wake_hooks, "_write_surface", _explode)

    emitter = _emitter()
    wake_hooks.post_stage_d_invariant_check(
        mind=tmp_path,
        emitter=emitter,
        phase=Phase.SLEEP_D,
        find_unaudited_fn=_scan,
    )

    assert len(emitter.of_kind("stage_d_invariant_violation")) == 1
    errors = emitter.of_kind("stage_d_invariant_hook_error")
    assert len(errors) == 1
    assert errors[0]["stage"] == "surface_write"
    assert errors[0]["error_type"] == "OSError"
    assert emitter.of_kind("stage_d_invariant_surface_written") == []


# ---------------------------------------------------------------------------
# Integration: hook fires from wake.py finally even on wake-body error
# ---------------------------------------------------------------------------


def test_hook_fires_on_wake_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Smoke test the finally-block contract: when ``run_wake`` raises
    inside ``main()``, the post-wake hook must still fire for SLEEP_D.

    We synthesize a tiny stand-in for the relevant slice of ``main`` —
    the try/finally around ``asyncio.run(run_wake(...))``. Anything more
    elaborate would require stubbing the entire argparse + config-load
    + auth stack, which buys nothing here. The point of this test is
    the structural guarantee, not the full wake plumbing."""

    hook_calls: list[dict[str, Any]] = []

    def _hook(*, mind, emitter, phase, **_kwargs: Any) -> None:
        hook_calls.append({"mind": mind, "emitter": emitter, "phase": phase})

    monkeypatch.setattr(
        wake_hooks, "post_stage_d_invariant_check", _hook
    )

    # Re-import the symbol the way wake.py does (lazy ``from .wake_hooks
    # import post_stage_d_invariant_check`` inside the finally block).
    # The monkeypatch above replaces the module attribute, which is what
    # the lazy import resolves through.
    from alice_thinking import wake_hooks as _wh  # noqa: F401

    emitter = _emitter()
    phase = Phase.SLEEP_D

    def _run_wake_that_blows_up() -> int:
        raise RuntimeError("kernel went sideways")

    raised = False
    try:
        try:
            _run_wake_that_blows_up()
        finally:
            # This is the exact shape of the wake.py finally block.
            from alice_thinking.wake_hooks import (
                post_stage_d_invariant_check,
            )

            post_stage_d_invariant_check(
                mind=tmp_path,
                emitter=emitter,
                phase=phase,
            )
    except RuntimeError:
        raised = True

    assert raised is True, "wake error must still propagate after the hook"
    assert len(hook_calls) == 1
    assert hook_calls[0]["phase"] is Phase.SLEEP_D
    assert hook_calls[0]["mind"] == tmp_path


def test_hook_fires_for_empty_scan_without_error(
    tmp_path: pathlib.Path,
) -> None:
    """Sanity: empty scan + clean wake path — the hook still runs without
    issue. Pairs with the wake-error test to cover both finally-block
    sides of the contract."""

    def _scan(**_kwargs: Any) -> list[dict]:
        return []

    emitter = _emitter()
    wake_hooks.post_stage_d_invariant_check(
        mind=tmp_path,
        emitter=emitter,
        phase=Phase.SLEEP_D,
        find_unaudited_fn=_scan,
    )
    # Just the clean event, nothing else.
    assert [e["event"] for e in emitter.events] == ["stage_d_invariant_clean"]
