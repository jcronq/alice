"""Tests for the ``/api/woy/action`` endpoint + accept-mirror bridge.

The structured ``kind=woy`` task rows carry three ``accept_descriptors``
(task-close / task-snooze / task-reject). ``handle_woy_task_action``
runs them against the SM task store and appends the result to the
item thread + the dismissal overlay.

Also covers the Accept-mirror bridge wired into the existing
``handle_woy_action`` path: when a chip-click's slug maps to a
``kind=woy`` task, the task transitions in lockstep with the
dismissal.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from alice_forge.task_store import TaskStore
from status_site import server as S


# ---------------------------------------------------------------- helpers


def _mk_cfg(tmp_path: Path, *, tasks_root: Path) -> S.Config:
    d = tmp_path / "status"
    d.mkdir(exist_ok=True)

    class _C:
        status_dir = d
        threads_dir = d / "threads"
        reply_log = d / ".replies.jsonl"
        key = "k"

    cfg = _C()
    cfg.lock = threading.Lock()
    return cfg


def _seed_woy(store: TaskStore, **extra):
    kwargs = {
        "title": "Call Randy Sosa at NAM",
        "kind": "woy",
        "verb": "Call",
        "target": "Randy Sosa at NAM",
        "context": "Withdraw case 674699.",
    }
    kwargs.update(extra)
    return store.create(**kwargs)


@pytest.fixture
def tasks_env(monkeypatch, tmp_path):
    """Point the server's ``_tasks_dir()`` resolver at a tmp task store."""
    mind = tmp_path / "mind"
    (mind / "inner" / "tasks").mkdir(parents=True)
    monkeypatch.setenv("ALICE_MIND", str(mind))
    store = TaskStore(mind / "inner" / "tasks")
    return store


# ---------------------------------------------------------------- handle_woy_task_action


def test_task_close_marks_task_done_and_dismisses(tmp_path, tasks_env):
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    t = _seed_woy(tasks_env)
    out = S.handle_woy_task_action(
        cfg, {"key": "k", "task_id": t.id, "action": "task-close"}
    )
    assert out["ok"] is True
    assert out["task_id"] == t.id
    assert out["outcome"] == "done"
    assert out["dismissed"] is True
    # Task record moved to done.
    reloaded = tasks_env.load(t.id)
    assert reloaded.status == "done"
    # Dismissal recorded.
    disp = (cfg.status_dir / S.WOY_DISMISSED_FILE).read_text().splitlines()
    assert any(json.loads(ln)["slug"] == "call-randy-sosa-at-nam" for ln in disp)
    # Thread entry recorded.
    tr = (cfg.threads_dir / "call-randy-sosa-at-nam.jsonl").read_text().splitlines()
    assert len(tr) == 1
    rec = json.loads(tr[0])
    assert rec["action"] == "task-close"
    assert rec["task_id"] == t.id


def test_task_reject_marks_task_rejected(tmp_path, tasks_env):
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    t = _seed_woy(tasks_env)
    out = S.handle_woy_task_action(
        cfg, {"key": "k", "task_id": t.id, "action": "task-reject"}
    )
    assert out["outcome"] == "rejected"
    assert tasks_env.load(t.id).status == "rejected"


def test_task_snooze_sets_until_default_tomorrow(tmp_path, tasks_env):
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    t = _seed_woy(tasks_env)
    out = S.handle_woy_task_action(
        cfg, {"key": "k", "task_id": t.id, "action": "task-snooze"}
    )
    assert out["outcome"] == "snoozed"
    snooze_until = out["snooze_until"]
    assert "T09:00:00" in snooze_until
    # Status unchanged.
    reloaded = tasks_env.load(t.id)
    assert reloaded.status == "draft"
    assert reloaded.snooze_until == snooze_until


def test_task_snooze_honours_explicit_until(tmp_path, tasks_env):
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    t = _seed_woy(tasks_env)
    until = "2026-12-01T12:00:00-05:00"
    out = S.handle_woy_task_action(
        cfg,
        {"key": "k", "task_id": t.id, "action": "task-snooze", "until": until},
    )
    assert out["snooze_until"] == until
    assert tasks_env.load(t.id).snooze_until == until


def test_bad_key_raises_403(tmp_path, tasks_env):
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    t = _seed_woy(tasks_env)
    with pytest.raises(S.ReplyError) as e:
        S.handle_woy_task_action(
            cfg, {"key": "nope", "task_id": t.id, "action": "task-close"}
        )
    assert e.value.code == 403


def test_bad_action_raises_400(tmp_path, tasks_env):
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    t = _seed_woy(tasks_env)
    with pytest.raises(S.ReplyError) as e:
        S.handle_woy_task_action(
            cfg, {"key": "k", "task_id": t.id, "action": "obliterate"}
        )
    assert e.value.code == 400


def test_bad_task_id_shape_rejected(tmp_path, tasks_env):
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    with pytest.raises(S.ReplyError) as e:
        S.handle_woy_task_action(
            cfg,
            {"key": "k", "task_id": "../../etc/passwd", "action": "task-close"},
        )
    assert e.value.code == 400


def test_missing_task_returns_404(tmp_path, tasks_env):
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    with pytest.raises(S.ReplyError) as e:
        S.handle_woy_task_action(
            cfg, {"key": "k", "task_id": "task-9999", "action": "task-close"}
        )
    assert e.value.code == 404


def test_rejects_non_woy_task(tmp_path, tasks_env):
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    plain = tasks_env.create(title="plain code task")  # no kind
    with pytest.raises(S.ReplyError) as e:
        S.handle_woy_task_action(
            cfg,
            {"key": "k", "task_id": plain.id, "action": "task-close"},
        )
    assert e.value.code == 409


# ---------------------------------------------------------------- accept-mirror bridge


def test_handle_woy_action_accept_mirrors_woy_task_to_done(tmp_path, tasks_env):
    """When the WoY markdown-zone slug resolves to a kind=woy task, the
    chip-click accept bubbles through to woy_close."""
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    t = _seed_woy(tasks_env)
    # The hand-zone render would emit this slug; simulate the chip click.
    out = S.handle_woy_action(
        cfg,
        {"key": "k", "slug": "call-randy-sosa-at-nam", "action": "accept"},
    )
    assert out["dismissed"] is True
    assert out.get("woy_task") == {"task_id": t.id, "outcome": "done"}
    assert tasks_env.load(t.id).status == "done"


def test_handle_woy_action_reject_mirrors_woy_task_to_rejected(tmp_path, tasks_env):
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    t = _seed_woy(tasks_env)
    out = S.handle_woy_action(
        cfg,
        {"key": "k", "slug": "call-randy-sosa-at-nam", "action": "reject"},
    )
    assert out.get("woy_task") == {"task_id": t.id, "outcome": "rejected"}
    assert tasks_env.load(t.id).status == "rejected"


def test_handle_woy_action_non_woy_slug_leaves_tasks_untouched(tmp_path, tasks_env):
    """A plain hand-zone slug that doesn't map to a task stays silent."""
    cfg = _mk_cfg(tmp_path, tasks_root=tasks_env.root)
    out = S.handle_woy_action(
        cfg, {"key": "k", "slug": "not-a-task-at-all", "action": "accept"}
    )
    # No ``woy_task`` key when no match.
    assert "woy_task" not in out
