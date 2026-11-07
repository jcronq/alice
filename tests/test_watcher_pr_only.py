"""PR-7 (#830): gh-watcher's PR-only terminal transition path.

The watcher used to be the SM lifecycle's eyes on GitHub — labeling issues,
watching for ``sm:design_review`` edges, snapshotting labels. All of that
moved to the alice-state DB (#823), so the watcher now carries exactly one
SM-aware responsibility: when a watched PR merges (or closes unmerged) and
its body carries ``Closes item-<N>``, write one terminal
``state_machine`` transition against the referenced item.

These tests pin that contract. The tests inject a ``Mock()`` for
``transition=`` so they do not depend on PR-1 (#824 / #835 — the
``alice_forge.sm.store`` module) having merged.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import Mock

import pytest

from alice_forge.sm.states import SMState
from watchers import github as gh_watcher

# Reuse the existing FakeAPI / _make_pr / _make_issue helpers — duplicating
# them would just drift from the real GitHub API shapes the rest of the
# suite exercises.
from tests.test_gh_watcher import FakeAPI, _make_issue, _make_pr


REPO = "acme/widgets"


@pytest.fixture
def mind_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "mind"
    (d / "config").mkdir(parents=True)
    (d / "inner" / "notes").mkdir(parents=True)
    (d / "config" / "alice.config.json").write_text(
        json.dumps({"github_watcher": {"enabled": True, "repos": [REPO]}})
    )
    return d


@pytest.fixture
def state_path(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "state"
    d.mkdir()
    return d / "gh-watcher-state.json"


def _run(
    mind_dir: pathlib.Path,
    state_path: pathlib.Path,
    api: FakeAPI,
    *,
    transition: Mock | None = None,
) -> Mock:
    """Drive one watcher pass and return the (possibly fresh) transition mock."""
    if transition is None:
        transition = Mock(return_value=1)
    gh_watcher.run(
        mind_dir=mind_dir,
        state_path=state_path,
        api=api,
        log=lambda _: None,
        transition=transition,
        db_path=mind_dir / "inner" / "state" / "alice-state.db",
    )
    return transition


def _events_lines(mind: pathlib.Path) -> list[dict]:
    events_path = mind / "memory" / "events.jsonl"
    if not events_path.is_file():
        return []
    return [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Test 1 — a merge fires exactly one terminal transition and preserves the
# existing pr_state telemetry write.
# ---------------------------------------------------------------------------


def test_merge_fires_one_transition(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """Prime with the PR open; second poll flips it to merged with a
    body containing ``Closes item-0830``. One transition call, args
    deterministic. The merge still writes its ``pr_state`` telemetry
    line to ``events.jsonl`` (the transition path is additive, not a
    substitute)."""
    api = FakeAPI()
    body = "Closes #830\n\nCloses item-0830\n"
    api.pulls = [_make_pr(number=830, title="PR-7 impl", body=body, user="jcronq")]
    _run(mind_dir, state_path, api)  # prime

    # Flip to merged.
    api.pulls = [
        _make_pr(
            number=830,
            title="PR-7 impl",
            body=body,
            user="jcronq",
            state="closed",
            merged_at="2026-11-07T10:00:00Z",
        )
    ]
    transition = _run(mind_dir, state_path, api)

    assert transition.call_count == 1
    kwargs = transition.call_args.kwargs
    assert kwargs["item_id"] == f"gh:{REPO}#830"
    assert kwargs["to"] is SMState.DONE
    assert kwargs["source_ref"] == f"gh:pr:{REPO}#830:merged"
    assert kwargs["db_path"] == mind_dir / "inner" / "state" / "alice-state.db"

    # The existing telemetry path is untouched by the new transition call.
    events = _events_lines(mind_dir)
    pr_state_entries = [e for e in events if e.get("kind") == "pr_state"]
    assert len(pr_state_entries) == 1
    assert pr_state_entries[0]["type"] == "github_pr_state"
    assert pr_state_entries[0]["number"] == 830


# ---------------------------------------------------------------------------
# Test 2 — a close without a merge fires REJECTED instead of DONE.
# ---------------------------------------------------------------------------


def test_closed_unmerged_fires_rejected(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    api = FakeAPI()
    body = "Closes item-0830\n"
    api.pulls = [_make_pr(number=830, title="Rejected path", body=body, user="jcronq")]
    _run(mind_dir, state_path, api)  # prime

    # Close without merged_at → state == "closed".
    api.pulls = [
        _make_pr(
            number=830,
            title="Rejected path",
            body=body,
            user="jcronq",
            state="closed",
        )
    ]
    transition = _run(mind_dir, state_path, api)

    assert transition.call_count == 1
    kwargs = transition.call_args.kwargs
    assert kwargs["item_id"] == f"gh:{REPO}#830"
    assert kwargs["to"] is SMState.REJECTED
    assert kwargs["source_ref"] == f"gh:pr:{REPO}#830:closed"


# ---------------------------------------------------------------------------
# Test 3 — no ``Closes item-<N>`` line is a no-op for transition but NOT
# for the pr_state telemetry write.
# ---------------------------------------------------------------------------


def test_no_closes_item_line_skips_transition_but_writes_telemetry(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    api = FakeAPI()
    body = "Closes #830\n"  # no `Closes item-NNNN`
    api.pulls = [_make_pr(number=830, title="PR w/o item ref", body=body)]
    _run(mind_dir, state_path, api)  # prime

    api.pulls = [
        _make_pr(
            number=830,
            title="PR w/o item ref",
            body=body,
            state="closed",
            merged_at="2026-11-07T10:00:00Z",
        )
    ]
    transition = _run(mind_dir, state_path, api)

    transition.assert_not_called()

    events = _events_lines(mind_dir)
    pr_state_entries = [e for e in events if e.get("kind") == "pr_state"]
    assert len(pr_state_entries) == 1
    assert pr_state_entries[0]["number"] == 830


# ---------------------------------------------------------------------------
# Test 4 — an issue gaining an ``sm:*`` label is a complete no-op. The
# gh-watcher no longer snapshots labels, no longer fires design_review
# events, no longer writes inner/notes/ for label edges, no longer calls
# transition for label changes.
# ---------------------------------------------------------------------------


def test_issue_sm_label_change_is_noop(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    api = FakeAPI()
    api.issues = [
        _make_issue(
            number=616,
            title="Review page",
            labels=[{"id": 1, "name": "sm:designing", "color": "ffffff"}],
        )
    ]
    api.issue_thread_comments[616] = []
    _run(mind_dir, state_path, api)  # prime

    notes_before = list((mind_dir / "inner" / "notes").glob("*.md"))
    events_before = _events_lines(mind_dir)

    # Add ``sm:design_review`` — under the old watcher this would have
    # fired a decide-item write. Under PR-7 it must be invisible.
    api.issues[0]["labels"] = [
        {"id": 1, "name": "sm:designing"},
        {"id": 2, "name": "sm:design_review"},
    ]
    transition = _run(mind_dir, state_path, api)

    transition.assert_not_called()
    assert list((mind_dir / "inner" / "notes").glob("*.md")) == notes_before
    # No new events.jsonl entries either — the issue didn't change
    # state (still open), only its label set.
    assert _events_lines(mind_dir) == events_before
    # And no stray decide-item note from the removed sm:design_review path.
    assert not (mind_dir / "inner" / "status" / "notes").exists() or not list(
        (mind_dir / "inner" / "status" / "notes").glob("*.md")
    )


# ---------------------------------------------------------------------------
# Helper-level sanity: extract_closes_item_ref matches the spec.
# ---------------------------------------------------------------------------


def test_extract_closes_item_ref_shape() -> None:
    url = "https://github.com/acme/widgets/pull/830"
    assert gh_watcher.extract_closes_item_ref(
        "Closes #830\n\nCloses item-0830\n", url
    ) == ("item-0830", url)
    assert gh_watcher.extract_closes_item_ref("closes item-42", url) == (
        "item-42",
        url,
    )
    assert gh_watcher.extract_closes_item_ref("Closes #830 only", url) is None
    assert gh_watcher.extract_closes_item_ref(None, url) is None
    assert gh_watcher.extract_closes_item_ref("", url) is None
