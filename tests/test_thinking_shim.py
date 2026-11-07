"""Tests for :mod:`alice_forge.thinking_shim` — PR-4 of #823 (#827).

The shim is the per-issue thinking-agent entrypoint. These tests
pin its three contracts:

1. On a clean kernel run that produces a design doc, the shim
   writes exactly one ``transitions`` row at ``sm:design_review``
   with ``actor='thinking'``.
2. On a kernel failure (non-zero exit / exception) or a clean exit
   with no design doc, the shim writes a ``transitions`` row at
   ``sm:blocked`` whose ``reason`` starts with ``'PhaseRunner
   failed'``.
3. ``--mode=build`` never writes a transition — that's
   :mod:`alice_forge.speaking_shim`'s job.

Plus the edge cases: missing ``prompt.txt``, no ``Issue:`` header,
process exit codes matching the stub contract (0 clean / 1 fail).

The DI seams exercised here —
``runner_factory`` / ``kernel_runner`` / ``db_connect`` /
``find_design_doc`` — let the tests swap in doubles without
touching the real :class:`alice_thinking.runtime.PhaseRunner`, the
kernel, the state DB on disk, or the vault. They're the same seams
a future test harness or a CI smoke-run would use.
"""

from __future__ import annotations

import pathlib
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

import pytest

from alice_forge import thinking_shim
from alice_forge.sm.states import SMState
from alice_state_db.schema import apply_schema


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


ITEM_ID = "gh:jcronq/alice#827"
SESSION_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Schema-applied ``alice-state.db`` file — no content seeded."""
    db = tmp_path / "alice-state.db"
    conn = sqlite3.connect(str(db))
    try:
        apply_schema(conn)
    finally:
        conn.close()
    return db


def _seed_item(
    db_path: pathlib.Path,
    *,
    item_id: str = ITEM_ID,
    state: SMState = SMState.DESIGNING,
) -> None:
    """Seed an ``items`` + ``state_machine`` row at ``state``."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            """
            INSERT INTO items (item_id, item_type, title, created_at, updated_at)
            VALUES (?, 'gh:issue', ?, ?, ?)
            """,
            (
                item_id,
                f"test item {item_id}",
                "2026-11-07T00:00:00+00:00",
                "2026-11-07T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO state_machine (item_id, state, entered_at)
            VALUES (?, ?, ?)
            """,
            (item_id, state.label, "2026-11-07T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _make_db_connect(
    db_path: pathlib.Path,
) -> Callable[..., Any]:
    """Return a ``db_connect`` double matching
    :func:`alice_state_db.read_api.connect`'s signature — a context
    manager that yields a ``sqlite3.Connection``."""

    @contextmanager
    def _connect(*, read_only: bool = True) -> Iterator[sqlite3.Connection]:
        # read_only is honoured for shape only — tests always need
        # write access to assert the transitions row landed.
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
        finally:
            conn.close()

    return _connect


def _read_transitions(
    db_path: pathlib.Path, item_id: str
) -> list[sqlite3.Row]:
    """Return every ``transitions`` row for ``item_id``, oldest first."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                "SELECT * FROM transitions WHERE item_id = ? "
                "ORDER BY transition_id ASC",
                (item_id,),
            ).fetchall()
        )
    finally:
        conn.close()


def _read_state(db_path: pathlib.Path, item_id: str) -> Optional[str]:
    """Return the current ``state_machine.state`` for ``item_id``."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT state FROM state_machine WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _write_prompt(
    tmp_path: pathlib.Path,
    *,
    issue_number: int = 827,
    mode: str = "design",
    with_header: bool = True,
    body: str = "do the thing",
) -> pathlib.Path:
    """Write a minimal ``<spawn_dir>/prompt.txt`` and return the spawn dir."""
    spawn_dir = tmp_path / "spawn-dir"
    spawn_dir.mkdir(exist_ok=True)
    if with_header:
        prompt_body = f"Issue: #{issue_number}\n\n{body}\n"
    else:
        prompt_body = f"{body}\n"
    (spawn_dir / "prompt.txt").write_text(
        f"---\nphase: per_issue_{mode}\n---\n{prompt_body}",
        encoding="utf-8",
    )
    return spawn_dir


class _FakePhaseRunner:
    """Stand-in for :class:`alice_thinking.runtime.PhaseRunner`.

    ``runner.run(phase, ctx, injected_content=body)`` must return
    ``(prompt_text, spec)`` — the shim passes those straight to the
    kernel runner, which the test replaces with a stub.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, str]] = []

    def run(self, phase, ctx, *, injected_content: str):
        self.calls.append((phase, ctx, injected_content))
        return "prompt text", object()


def _runner_factory() -> _FakePhaseRunner:
    return _FakePhaseRunner()


def _make_kernel_runner(
    rc: int = 0, *, raise_exc: Optional[Exception] = None
) -> Callable[..., int]:
    """Return a ``kernel_runner`` double matching the ``(log=...)`` DI shape.

    (The production ``_default_kernel_runner`` also takes ``backend``;
    the shim dispatches on identity to call the default with backend
    and anything else with just ``log``.)
    """

    def _runner(prompt_text: str, spec: Any, *, log: Callable[[str], None]) -> int:
        if raise_exc is not None:
            raise raise_exc
        return rc

    return _runner


def _no_mind_side_effects(monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Patch backend / cwd / mind resolution so production helpers can't
    touch the operator's real ``~/alice-mind`` / ``~/alice`` from a test.

    Returns a throwaway mind dir the caller can hand to
    ``find_design_doc`` doubles for parity with production paths.
    """
    # The shim calls _load_backend (loads the thinking model config
    # from mind/) and _build_wake_context (builds a WakeContext with
    # the mind dir). We stub both to decouple from the real vault.
    class _Backend:
        model = "test-model"

    def _fake_load_backend(*, mind, model_override):
        return _Backend()

    def _fake_build_ctx(**kwargs):
        return object()

    monkeypatch.setattr(thinking_shim, "_load_backend", _fake_load_backend)
    monkeypatch.setattr(thinking_shim, "_build_wake_context", _fake_build_ctx)
    return pathlib.Path("/does/not/exist-mind")


# ---------------------------------------------------------------------------
# Happy path — design mode
# ---------------------------------------------------------------------------


def test_design_success_writes_design_review_transition(
    tmp_path: pathlib.Path,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean kernel + design doc found → one row at sm:design_review."""
    _seed_item(db_path, state=SMState.DESIGNING)
    spawn_dir = _write_prompt(tmp_path, issue_number=827, mode="design")
    _no_mind_side_effects(monkeypatch)

    fake_doc = tmp_path / "designs" / "2026-11-07-issue827-shim.md"
    fake_doc.parent.mkdir()
    fake_doc.write_text("# design")

    def _find(mind, issue_number, since_ts):
        assert issue_number == "827"
        return fake_doc

    rc = thinking_shim.main(
        argv=[
            "--spawn-dir",
            str(spawn_dir),
            "--session-id",
            SESSION_ID,
            "--mode",
            "design",
            "--mind",
            str(tmp_path),
        ],
        runner_factory=_runner_factory,
        kernel_runner=_make_kernel_runner(rc=0),
        db_connect=_make_db_connect(db_path),
        find_design_doc=_find,
        log=lambda _m: None,
    )

    assert rc == 0
    rows = _read_transitions(db_path, ITEM_ID)
    assert len(rows) == 1
    assert rows[0]["to_state"] == SMState.DESIGN_REVIEW.label
    assert rows[0]["from_state"] == SMState.DESIGNING.label
    assert rows[0]["actor"] == "thinking"
    assert rows[0]["source"] == "thinking-shim"
    assert rows[0]["source_ref"] == SESSION_ID
    assert rows[0]["reason"] == "design doc ready"
    # state_machine row updated in the same transaction.
    assert _read_state(db_path, ITEM_ID) == SMState.DESIGN_REVIEW.label


# ---------------------------------------------------------------------------
# Failure paths — design mode
# ---------------------------------------------------------------------------


def test_design_kernel_nonzero_writes_blocked_transition(
    tmp_path: pathlib.Path,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kernel exits rc!=0 → sm:blocked row, reason starts PhaseRunner failed."""
    _seed_item(db_path, state=SMState.DESIGNING)
    spawn_dir = _write_prompt(tmp_path, mode="design")
    _no_mind_side_effects(monkeypatch)

    rc = thinking_shim.main(
        argv=[
            "--spawn-dir",
            str(spawn_dir),
            "--session-id",
            SESSION_ID,
            "--mode",
            "design",
            "--mind",
            str(tmp_path),
        ],
        runner_factory=_runner_factory,
        kernel_runner=_make_kernel_runner(rc=1),
        db_connect=_make_db_connect(db_path),
        # Not consulted on failure, but passed for completeness.
        find_design_doc=lambda *_a, **_k: None,
        log=lambda _m: None,
    )

    assert rc == 1
    rows = _read_transitions(db_path, ITEM_ID)
    assert len(rows) == 1
    assert rows[0]["to_state"] == SMState.BLOCKED.label
    assert rows[0]["actor"] == "thinking"
    assert rows[0]["reason"].startswith("PhaseRunner failed")
    assert "rc=1" in rows[0]["reason"]
    assert _read_state(db_path, ITEM_ID) == SMState.BLOCKED.label


def test_design_kernel_raises_writes_blocked_transition(
    tmp_path: pathlib.Path,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kernel raises → sm:blocked row with exception name in reason."""
    _seed_item(db_path, state=SMState.DESIGNING)
    spawn_dir = _write_prompt(tmp_path, mode="design")
    _no_mind_side_effects(monkeypatch)

    rc = thinking_shim.main(
        argv=[
            "--spawn-dir",
            str(spawn_dir),
            "--session-id",
            SESSION_ID,
            "--mode",
            "design",
            "--mind",
            str(tmp_path),
        ],
        runner_factory=_runner_factory,
        kernel_runner=_make_kernel_runner(raise_exc=RuntimeError("boom")),
        db_connect=_make_db_connect(db_path),
        find_design_doc=lambda *_a, **_k: None,
        log=lambda _m: None,
    )

    assert rc == 1
    rows = _read_transitions(db_path, ITEM_ID)
    assert len(rows) == 1
    assert rows[0]["to_state"] == SMState.BLOCKED.label
    assert rows[0]["reason"].startswith("PhaseRunner failed")
    assert "RuntimeError" in rows[0]["reason"]
    assert "boom" in rows[0]["reason"]


def test_design_no_doc_found_writes_blocked_transition(
    tmp_path: pathlib.Path,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kernel clean but no design doc → sm:blocked with 'no design doc'."""
    _seed_item(db_path, state=SMState.DESIGNING)
    spawn_dir = _write_prompt(tmp_path, mode="design")
    _no_mind_side_effects(monkeypatch)

    rc = thinking_shim.main(
        argv=[
            "--spawn-dir",
            str(spawn_dir),
            "--session-id",
            SESSION_ID,
            "--mode",
            "design",
            "--mind",
            str(tmp_path),
        ],
        runner_factory=_runner_factory,
        kernel_runner=_make_kernel_runner(rc=0),
        db_connect=_make_db_connect(db_path),
        find_design_doc=lambda *_a, **_k: None,
        log=lambda _m: None,
    )

    assert rc == 1
    rows = _read_transitions(db_path, ITEM_ID)
    assert len(rows) == 1
    assert rows[0]["to_state"] == SMState.BLOCKED.label
    assert rows[0]["reason"].startswith("PhaseRunner failed")
    assert "no design doc" in rows[0]["reason"]


# ---------------------------------------------------------------------------
# Build mode — never transitions
# ---------------------------------------------------------------------------


def test_build_mode_clean_exit_writes_no_transition(
    tmp_path: pathlib.Path,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--mode=build`` + rc=0 → shim exits 0 and writes zero transitions.

    The build-phase transition belongs to :mod:`alice_forge.speaking_shim`.
    """
    _seed_item(db_path, state=SMState.DESIGNING)  # arbitrary; shouldn't matter
    spawn_dir = _write_prompt(tmp_path, mode="build")
    _no_mind_side_effects(monkeypatch)

    # Should not be called — if the test invokes it, the assertion in
    # the double fires loudly rather than silently succeeding.
    def _find_fail(*_a, **_k):
        raise AssertionError("find_design_doc called in --mode=build")

    rc = thinking_shim.main(
        argv=[
            "--spawn-dir",
            str(spawn_dir),
            "--session-id",
            SESSION_ID,
            "--mode",
            "build",
            "--mind",
            str(tmp_path),
        ],
        runner_factory=_runner_factory,
        kernel_runner=_make_kernel_runner(rc=0),
        db_connect=_make_db_connect(db_path),
        find_design_doc=_find_fail,
        log=lambda _m: None,
    )

    assert rc == 0
    rows = _read_transitions(db_path, ITEM_ID)
    assert rows == []
    assert _read_state(db_path, ITEM_ID) == SMState.DESIGNING.label


# ---------------------------------------------------------------------------
# Pre-flight failures — exit 1 before DB touch
# ---------------------------------------------------------------------------


def test_missing_prompt_file_exits_one_no_db_touch(
    tmp_path: pathlib.Path,
    db_path: pathlib.Path,
) -> None:
    """Spawn dir without prompt.txt → exit 1, no DB write attempted."""
    spawn_dir = tmp_path / "spawn-dir"
    spawn_dir.mkdir()

    # If the shim touches db_connect without an item_id, that's a bug.
    def _db_connect_tripwire(**_kwargs):
        raise AssertionError("db_connect called with no item_id")

    rc = thinking_shim.main(
        argv=[
            "--spawn-dir",
            str(spawn_dir),
            "--session-id",
            SESSION_ID,
        ],
        db_connect=_db_connect_tripwire,
        log=lambda _m: None,
    )

    assert rc == 1
    # state_machine untouched (item was never seeded)
    assert _read_state(db_path, ITEM_ID) is None


def test_missing_issue_header_exits_one_no_db_touch(
    tmp_path: pathlib.Path,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prompt.txt without ``Issue: #N`` → exit 1, no sm.store call made.

    This is the 'unresolvable target' case — there's no item_id to
    transition, so the shim bails before attempting a DB write.
    """
    spawn_dir = _write_prompt(tmp_path, with_header=False, mode="design")
    _no_mind_side_effects(monkeypatch)

    def _db_connect_tripwire(**_kwargs):
        raise AssertionError("db_connect called despite missing Issue header")

    rc = thinking_shim.main(
        argv=[
            "--spawn-dir",
            str(spawn_dir),
            "--session-id",
            SESSION_ID,
            "--mode",
            "design",
            "--mind",
            str(tmp_path),
        ],
        runner_factory=_runner_factory,
        kernel_runner=_make_kernel_runner(rc=0),
        db_connect=_db_connect_tripwire,
        log=lambda _m: None,
    )

    assert rc == 1


# ---------------------------------------------------------------------------
# _find_design_doc — the glob behaviour
# ---------------------------------------------------------------------------


def test_find_design_doc_picks_newest_matching_file(
    tmp_path: pathlib.Path,
) -> None:
    """_find_design_doc returns the newest file matching *issue<N>* that
    was written after ``since_ts``; older files are skipped."""
    designs = tmp_path / "cortex-memory" / "designs"
    designs.mkdir(parents=True)

    old = designs / "2025-01-01-issue827-old.md"
    old.write_text("# old")
    import os

    os.utime(old, (1_000_000.0, 1_000_000.0))

    newer = designs / "2026-11-07-issue827-newer.md"
    newer.write_text("# newer")
    os.utime(newer, (2_000_000.0, 2_000_000.0))

    # Bar across the old one, under the newer one → only newer qualifies.
    hit = thinking_shim._find_design_doc(tmp_path, "827", since_ts=1_500_000.0)
    assert hit == newer


def test_find_design_doc_skips_other_issues(tmp_path: pathlib.Path) -> None:
    """A design doc for a different issue number is not a candidate."""
    designs = tmp_path / "cortex-memory" / "designs"
    designs.mkdir(parents=True)
    (designs / "2026-11-07-issue999-other.md").write_text("# other")

    hit = thinking_shim._find_design_doc(tmp_path, "827", since_ts=0.0)
    assert hit is None
