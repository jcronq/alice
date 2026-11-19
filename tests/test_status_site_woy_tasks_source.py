"""Tests for ``status_site.sources.woy_tasks``.

Exercises the WoY task scanner that turns ``kind=woy`` SM tasks into
rows with ``verb`` / ``target`` / ``context`` / ``slug`` /
``accept_descriptors`` for the Overview Waiting-on-you tier.

Covers:

- ``scan_woy_tasks`` filters by ``kind=woy`` and visible statuses.
- Terminal statuses (done / rejected) and non-woy kinds are skipped.
- Snoozed tasks hide by default, surface when ``include_snoozed=True``.
- Row shape carries the full accept_descriptor trio + stable slug
  matching the frontend ``titleSlug``.
- ``render_markdown`` emits the chip-ready bullet shape.
- ``slug_to_task_id`` round-trips the slug back to the task id.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alice_forge.task_store import TaskStore
from status_site.sources import woy_tasks as wt


@pytest.fixture
def store(tmp_path) -> TaskStore:
    return TaskStore(tmp_path / "tasks")


@pytest.fixture
def _now_epoch() -> float:
    """Deterministic 'now' for scanner tests. 2026-11-18T12:00:00Z."""
    return datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc).timestamp()


def _seed_woy(store: TaskStore, **extra):
    """Create a kind=woy task with sensible defaults; override via kwargs."""
    kwargs = {
        "title": "Call Randy Sosa at NAM",
        "kind": "woy",
        "verb": "Call",
        "target": "Randy Sosa at NAM",
        "context": "Withdraw case 674699.",
    }
    kwargs.update(extra)
    return store.create(**kwargs)


def test_scan_emits_structured_rows(store, _now_epoch):
    _seed_woy(store)
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    assert len(rows) == 1
    r = rows[0]
    assert r["task_id"] == "task-0001"
    assert r["verb"] == "Call"
    assert r["target"] == "Randy Sosa at NAM"
    assert r["context"] == "Withdraw case 674699."
    assert r["body"] == r["context"]
    assert r["title"] == "Call Randy Sosa at NAM"
    assert r["slug"] == "call-randy-sosa-at-nam"
    assert r["external"] is True
    assert r["status"] == "draft"
    # Default chip_set is "todo" (Done / Snooze) — the vast majority
    # of WoY items are action TODOs; Accept/Reject is opt-in for the
    # design-review subcase.
    assert r["chip_set"] == "todo"
    actions = [d["action"] for d in r["accept_descriptors"]]
    assert actions == ["task-close", "task-snooze"]
    labels = [d["label"] for d in r["accept_descriptors"]]
    assert labels == ["Done", "Snooze"]


def test_scan_excludes_non_woy_tasks(store, _now_epoch):
    _seed_woy(store)
    store.create(title="code task")  # kind unset → NOT woy
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    ids = [r["task_id"] for r in rows]
    assert ids == ["task-0001"]


def test_scan_excludes_terminal_statuses(store, _now_epoch):
    t = _seed_woy(store)
    store.woy_close(t.id, outcome="done")
    _seed_woy(store, title="Still open")
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    assert [r["task_id"] for r in rows] == ["task-0002"]


def test_scan_hides_snoozed_task_by_default(store, _now_epoch):
    t = _seed_woy(store)
    # Snoozed to 1h in the future — hidden.
    future = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)
    store.snooze(t.id, until=future.isoformat(timespec="seconds"))
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    assert rows == []
    # With include_snoozed, the row surfaces.
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch, include_snoozed=True)
    assert len(rows) == 1
    assert rows[0]["snooze_until"] == future.isoformat(timespec="seconds")


def test_scan_shows_snoozed_task_after_window(store, _now_epoch):
    t = _seed_woy(store)
    # Snoozed to 1h in the past — expired, should show.
    past = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)
    store.snooze(t.id, until=past.isoformat(timespec="seconds"))
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    assert len(rows) == 1


def test_render_markdown_emits_chip_ready_bullets(store, _now_epoch):
    _seed_woy(store)
    _seed_woy(
        store,
        title="Email Dan re Agentic Org parity",
        verb="Email",
        target="Dan re Agentic Org parity",
        context="Ask for prompts, manifest, and repo URLs.",
    )
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    md = wt.render_markdown(rows)
    assert "- [ ] **Call Randy Sosa at NAM**: Withdraw case 674699." in md
    assert (
        "- [ ] **Email Dan re Agentic Org parity**: "
        "Ask for prompts, manifest, and repo URLs."
    ) in md


def test_render_markdown_empty_input_returns_empty_string():
    assert wt.render_markdown([]) == ""


def test_slug_to_task_id_roundtrip(store, _now_epoch):
    t = _seed_woy(store)
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    assert rows[0]["slug"] == "call-randy-sosa-at-nam"
    assert wt.slug_to_task_id(store.root, "call-randy-sosa-at-nam") == t.id
    assert wt.slug_to_task_id(store.root, "no-such-slug") is None


def test_slug_to_task_id_finds_snoozed_task(store, _now_epoch):
    """A chip click on a visually-hidden snoozed task must still resolve
    (the frontend might have the old slug cached)."""
    t = _seed_woy(store)
    future = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)
    store.snooze(t.id, until=future.isoformat(timespec="seconds"))
    assert wt.slug_to_task_id(store.root, "call-randy-sosa-at-nam") == t.id


def test_typed_rows_strips_volatile_fields(store, _now_epoch):
    _seed_woy(store)
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    typed = wt.typed_rows(rows)
    assert typed[0]["slug"] == "call-randy-sosa-at-nam"
    # ``age_seconds`` is intentionally OUT of the typed shape so a
    # section.update doesn't fire every tick as time drifts.
    assert "age_seconds" not in typed[0]
    assert "accept_descriptors" in typed[0]
    assert typed[0]["chip_set"] == "todo"
    # ``created_epoch`` is stable across ticks → dict-equality holds.
    assert isinstance(typed[0]["created_epoch"], float)


# ------------------------------------------------------------------ chip_set


def test_approval_chip_set_emits_accept_reject(store, _now_epoch):
    """Opt-in ``chip_set=approval`` swaps the renderer into the
    design-review Accept / Reject descriptor pair."""
    _seed_woy(store, chip_set="approval")
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    r = rows[0]
    assert r["chip_set"] == "approval"
    actions = [d["action"] for d in r["accept_descriptors"]]
    assert actions == ["task-close", "task-reject"]
    labels = [d["label"] for d in r["accept_descriptors"]]
    assert labels == ["Accept", "Reject"]


def test_chip_set_default_when_missing_from_yaml(store, _now_epoch, tmp_path):
    """A task.yaml without ``chip_set`` defaults to ``todo`` in the
    renderer — no migration needed for pre-chip-set WoY tasks."""
    _seed_woy(store)
    # Hand-edit the file to drop chip_set (simulate a pre-field task).
    tyaml = store.root / "task-0001" / "task.yaml"
    text = tyaml.read_text()
    text = "\n".join(line for line in text.splitlines() if not line.startswith("chip_set:"))
    tyaml.write_text(text)
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    assert rows[0]["chip_set"] == "todo"
    actions = [d["action"] for d in rows[0]["accept_descriptors"]]
    assert actions == ["task-close", "task-snooze"]


def test_chip_set_unknown_value_in_yaml_falls_back_to_default(
    store, _now_epoch
):
    """A hand-edited stale value shouldn't crash the renderer — the
    store-level write path owns validation."""
    _seed_woy(store)
    tyaml = store.root / "task-0001" / "task.yaml"
    text = tyaml.read_text().replace("chip_set: todo", "chip_set: haunted")
    tyaml.write_text(text)
    rows = wt.scan_woy_tasks(store.root, now=_now_epoch)
    assert rows[0]["chip_set"] == "todo"


def test_descriptors_for_chip_set_direct():
    todo = wt._descriptors_for_chip_set("task-0001", "todo")
    assert [d["action"] for d in todo] == ["task-close", "task-snooze"]
    approval = wt._descriptors_for_chip_set("task-0001", "approval")
    assert [d["action"] for d in approval] == ["task-close", "task-reject"]
    # Unknown → defaults to todo (renderer is forgiving).
    unknown = wt._descriptors_for_chip_set("task-0001", "???")
    assert [d["action"] for d in unknown] == ["task-close", "task-snooze"]
