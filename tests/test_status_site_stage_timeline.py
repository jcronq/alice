"""Slice-timeline tests: per-item stage-run attribution + aggregator + HTTP wire-up.

Covers the pieces status-site grew in Slice-timeline:

- ``append_stage_run`` + ``read_stage_runs`` round-trip, including
  malformed-line robustness and the ``item_slug`` filter.
- ``aggregate_timeline`` always returns five entries in the canonical
  stage order, with the correct rollup status per stage (none /
  running / success / failed).
- ``handle_stage_run`` is key-gated like the other POST handlers and
  validates stage / status at the HTTP boundary.
- The GET ``/api/item-timeline/<slug>`` endpoint returns the aggregate
  shape and an all-empty envelope for an item with no history.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from status_site import server as S
from status_site import stage_runs as SR
from status_site.server import Config, make_server


# ---------------------------------------------------------------- helpers


def _mk_cfg(tmp_path: Path) -> S.Config:
    """Minimal Config duck-type, same shape as the slice-effort fixture."""
    d = tmp_path / "status"
    d.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    lock = threading.Lock()

    class _C:
        status_dir = d
        threads_dir = d / "threads"
        surface_dir = surface
        reply_log = d / ".replies.jsonl"
        key = "k"
        public_url = "http://localhost:8095"
        max_body = 10_000
        max_upload = 1_000_000
    cfg = _C()
    cfg.lock = lock
    return cfg


# ---------------------------------------------------------------- module shape


def test_stages_are_the_canonical_lifecycle():
    """The frontend walks this tuple in order to render dots — if we ever
    renumber, the dot order flips. Lock the order here so future reorderings
    are an explicit decision with a failing test."""
    assert SR.STAGES == ("design", "build", "review", "check", "merge")


def test_statuses_are_running_success_failed():
    assert set(SR.STATUSES) == {"running", "success", "failed"}


# ---------------------------------------------------------------- append / read


def test_append_and_read_round_trip(tmp_path):
    rec = SR.append_stage_run(
        tmp_path, item_slug="foo-bar", stage="build",
        status="running", task_id="bg-task-1",
        started_at="2026-11-17T10:00:00-05:00",
    )
    assert rec.item_slug == "foo-bar"
    assert rec.stage == "build"
    assert rec.status == "running"
    assert rec.ended_at is None
    runs = SR.read_stage_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0] == rec


def test_append_defaults_started_at_to_now_iso(tmp_path, monkeypatch):
    monkeypatch.setattr(SR, "now_iso", lambda: "2026-11-17T12:00:00-05:00")
    rec = SR.append_stage_run(
        tmp_path, item_slug="x", stage="design", status="success",
    )
    assert rec.started_at == "2026-11-17T12:00:00-05:00"


def test_append_rejects_bad_stage(tmp_path):
    with pytest.raises(ValueError):
        SR.append_stage_run(tmp_path, item_slug="x", stage="mega", status="success")


def test_append_rejects_bad_status(tmp_path):
    with pytest.raises(ValueError):
        SR.append_stage_run(tmp_path, item_slug="x", stage="build", status="partial")


def test_append_rejects_empty_slug(tmp_path):
    with pytest.raises(ValueError):
        SR.append_stage_run(tmp_path, item_slug="", stage="build", status="success")


def test_append_narrows_task_id_charset(tmp_path):
    """Task ids with weird chars are narrowed, not rejected — the dispatcher
    sends whatever prefix it picked and we want the record on disk even if the
    id is ugly."""
    rec = SR.append_stage_run(
        tmp_path, item_slug="x", stage="build", status="success",
        task_id="bg$task/with weird!chars",
    )
    # Whitespace / slashes / shell metas scrubbed; the surviving chars
    # include [A-Za-z0-9._-].
    assert rec.task_id == "bgtaskwithweirdchars"


def test_append_truncates_long_summary(tmp_path):
    """The UI pins the summary into a tooltip; runaway callers must not
    be able to pack the on-disk log with 100 KB summaries."""
    rec = SR.append_stage_run(
        tmp_path, item_slug="x", stage="build", status="success",
        summary="x" * 1200,
    )
    assert rec.summary.endswith("…")
    # 400 + ellipsis
    assert len(rec.summary) <= 401


def test_read_filters_by_slug(tmp_path):
    SR.append_stage_run(tmp_path, item_slug="alpha", stage="build", status="success")
    SR.append_stage_run(tmp_path, item_slug="beta", stage="build", status="failed")
    alpha = SR.read_stage_runs(tmp_path, item_slug="alpha")
    assert len(alpha) == 1
    assert alpha[0].item_slug == "alpha"
    beta = SR.read_stage_runs(tmp_path, item_slug="beta")
    assert len(beta) == 1
    assert beta[0].status == "failed"


def test_read_filters_by_slug_normalises_input(tmp_path):
    """safe_slug normalisation runs on both read-side filter and write-
    side slug so a hand-written slug with mixed case still matches."""
    SR.append_stage_run(tmp_path, item_slug="My-Item", stage="build", status="success")
    # Mixed case + stray chars still find it.
    assert len(SR.read_stage_runs(tmp_path, item_slug="MY-ITEM!!!")) == 1


def test_read_skips_malformed_lines(tmp_path):
    p = tmp_path / SR.STAGE_RUNS_FILE
    p.write_text("\n".join([
        "{not json",
        json.dumps(["not", "an", "object"]),
        json.dumps({"item_slug": "x", "stage": "build", "status": "success", "started_at": "2026-11-17T10:00:00-05:00"}),
        json.dumps({"item_slug": "x", "stage": "mega", "status": "success", "started_at": "2026-11-17T10:00:00-05:00"}),  # bad stage
        json.dumps({"item_slug": "x", "stage": "build", "status": "whatever", "started_at": "2026-11-17T10:00:00-05:00"}),  # bad status
        json.dumps({"item_slug": "", "stage": "build", "status": "success", "started_at": "2026-11-17T10:00:00-05:00"}),  # empty slug
        json.dumps({"item_slug": "x", "stage": "build", "status": "success"}),  # missing started_at
    ]) + "\n")
    runs = SR.read_stage_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].stage == "build"


def test_read_empty_file_returns_empty(tmp_path):
    assert SR.read_stage_runs(tmp_path) == []


# ---------------------------------------------------------------- aggregate


def test_aggregate_empty_returns_five_none_stages(tmp_path):
    agg = SR.aggregate_timeline(tmp_path, "nothing-here")
    assert agg["slug"] == "nothing-here"
    assert [s["stage"] for s in agg["stages"]] == list(SR.STAGES)
    assert all(s["status"] == "none" for s in agg["stages"])
    assert all(s["latest"] is None for s in agg["stages"])
    assert all(s["count"] == 0 for s in agg["stages"])


def test_aggregate_rollup_single_success(tmp_path):
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="build", status="success",
        task_id="t1", started_at="2026-11-17T10:00:00-05:00",
        ended_at="2026-11-17T10:15:00-05:00",
    )
    agg = SR.aggregate_timeline(tmp_path, "item")
    by_stage = {s["stage"]: s for s in agg["stages"]}
    assert by_stage["build"]["status"] == "success"
    assert by_stage["build"]["count"] == 1
    assert by_stage["build"]["latest"]["task_id"] == "t1"
    # Untouched stages stay none.
    for other in ("design", "review", "check", "merge"):
        assert by_stage[other]["status"] == "none"


def test_aggregate_rollup_running_beats_success(tmp_path):
    """A success followed by a running retry should show 'running' (pulsing)
    on the UI, not 'success' — Jason needs to know work is actively in-flight."""
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="build", status="success",
        task_id="t1", started_at="2026-11-17T10:00:00-05:00",
        ended_at="2026-11-17T10:10:00-05:00",
    )
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="build", status="running",
        task_id="t2", started_at="2026-11-17T10:20:00-05:00",
    )
    agg = SR.aggregate_timeline(tmp_path, "item")
    by_stage = {s["stage"]: s for s in agg["stages"]}
    assert by_stage["build"]["status"] == "running"
    # Count reflects distinct tasked runs, not raw line count.
    assert by_stage["build"]["count"] == 2


def test_aggregate_rollup_success_beats_failed(tmp_path):
    """If any run on this stage succeeded, the dot shows success (even if a
    later retry failed). Matches the task's rendering spec: bright = at
    least one successful run."""
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="check", status="failed",
        task_id="t1", started_at="2026-11-17T10:00:00-05:00",
    )
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="check", status="success",
        task_id="t2", started_at="2026-11-17T10:05:00-05:00",
    )
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="check", status="failed",
        task_id="t3", started_at="2026-11-17T10:10:00-05:00",
    )
    agg = SR.aggregate_timeline(tmp_path, "item")
    by_stage = {s["stage"]: s for s in agg["stages"]}
    assert by_stage["check"]["status"] == "success"
    # Latest by started_at is t3 (failed), kept in ``latest`` so the
    # tooltip shows "this stage succeeded once but the last run failed".
    assert by_stage["check"]["latest"]["task_id"] == "t3"


def test_aggregate_rollup_failed_only_when_no_success(tmp_path):
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="check", status="failed",
        task_id="t1", started_at="2026-11-17T10:00:00-05:00",
    )
    agg = SR.aggregate_timeline(tmp_path, "item")
    by_stage = {s["stage"]: s for s in agg["stages"]}
    assert by_stage["check"]["status"] == "failed"


def test_aggregate_collapses_followups_by_task_id(tmp_path):
    """Opening + closing the same bg-task counts as one run; the aggregate's
    ``count`` reflects distinct tasks, not raw lines."""
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="build", status="running",
        task_id="t1", started_at="2026-11-17T10:00:00-05:00",
    )
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="build", status="success",
        task_id="t1", started_at="2026-11-17T10:00:00-05:00",
        ended_at="2026-11-17T10:05:00-05:00",
    )
    agg = SR.aggregate_timeline(tmp_path, "item")
    by_stage = {s["stage"]: s for s in agg["stages"]}
    assert by_stage["build"]["count"] == 1
    assert by_stage["build"]["status"] == "success"
    assert by_stage["build"]["latest"]["ended_at"] == "2026-11-17T10:05:00-05:00"


def test_aggregate_treats_untasked_events_as_independent(tmp_path):
    """Merge events / CI results often don't have a bg-task id — each
    append should count as one run so the aggregate's ``count`` goes up."""
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="merge", status="success",
        started_at="2026-11-17T10:00:00-05:00",
    )
    SR.append_stage_run(
        tmp_path, item_slug="item", stage="merge", status="success",
        started_at="2026-11-17T10:05:00-05:00",
    )
    agg = SR.aggregate_timeline(tmp_path, "item")
    by_stage = {s["stage"]: s for s in agg["stages"]}
    assert by_stage["merge"]["count"] == 2


# ---------------------------------------------------------------- HTTP handler


def test_handle_stage_run_bad_key_raises_403(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_stage_run(cfg, {"key": "nope", "item_slug": "x", "stage": "build", "status": "success"})
    assert e.value.code == 403


def test_handle_stage_run_bad_slug_raises_400(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_stage_run(cfg, {"key": "k", "item_slug": "", "stage": "build", "status": "success"})
    assert e.value.code == 400


def test_handle_stage_run_bad_stage_raises_400(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_stage_run(cfg, {"key": "k", "item_slug": "x", "stage": "mega", "status": "success"})
    assert e.value.code == 400


def test_handle_stage_run_bad_status_raises_400(tmp_path):
    cfg = _mk_cfg(tmp_path)
    with pytest.raises(S.ReplyError) as e:
        S.handle_stage_run(cfg, {"key": "k", "item_slug": "x", "stage": "build", "status": "whatever"})
    assert e.value.code == 400


def test_handle_stage_run_persists_record(tmp_path):
    cfg = _mk_cfg(tmp_path)
    resp = S.handle_stage_run(cfg, {
        "key": "k", "item_slug": "feat-x", "stage": "build",
        "status": "running", "task_id": "bg-123",
        "started_at": "2026-11-17T10:00:00-05:00",
    })
    assert resp["ok"] is True
    assert resp["record"]["stage"] == "build"
    assert resp["record"]["task_id"] == "bg-123"
    runs = SR.read_stage_runs(cfg.status_dir)
    assert len(runs) == 1
    assert runs[0].task_id == "bg-123"


def test_handle_stage_run_closes_an_in_flight_run(tmp_path):
    """Two-phase lifecycle: open with ``running``, close with ``success``
    on the same task_id. Aggregate rollup must show ``success``."""
    cfg = _mk_cfg(tmp_path)
    S.handle_stage_run(cfg, {
        "key": "k", "item_slug": "feat-x", "stage": "build",
        "status": "running", "task_id": "bg-123",
        "started_at": "2026-11-17T10:00:00-05:00",
    })
    S.handle_stage_run(cfg, {
        "key": "k", "item_slug": "feat-x", "stage": "build",
        "status": "success", "task_id": "bg-123",
        "started_at": "2026-11-17T10:00:00-05:00",
        "ended_at": "2026-11-17T10:05:00-05:00",
        "summary": "worker returned PR URL",
    })
    agg = SR.aggregate_timeline(cfg.status_dir, "feat-x")
    by_stage = {s["stage"]: s for s in agg["stages"]}
    assert by_stage["build"]["status"] == "success"
    assert by_stage["build"]["latest"]["summary"] == "worker returned PR URL"


# ---------------------------------------------------------------- HTTP wire
#
# Full-stack integration — spin up a real ThreadingHTTPServer and hit
# ``/api/item-timeline/<slug>`` + ``/api/stage-run`` over loopback.
# Mirrors the pattern in ``test_status_site.py`` so a route-level
# regression (middleware, dispatcher, wiring) surfaces here.


HTTP_KEY = "stage-timeline-test-key"


@pytest.fixture
def site(tmp_path):
    status = tmp_path / "status"
    (status / "threads").mkdir(parents=True)
    (status / "index.md").write_text("# Overview\n")
    cfg = Config(
        status_dir=status,
        surface_dir=tmp_path / "surface",
        upload_dir=tmp_path / "uploads",
        key=HTTP_KEY,
        port=0,
        host="127.0.0.1",
        upload_dir_alias=str(tmp_path / "uploads"),
        max_upload=1024,
    )
    srv = make_server(cfg)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield cfg, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _get_json(port: int, path: str) -> tuple[int, dict]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    code = r.status
    raw = r.read()
    c.close()
    return code, json.loads(raw or b"{}")


def _post_json(port: int, path: str, body: dict) -> tuple[int, dict]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body=json.dumps(body).encode(),
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    code = r.status
    raw = r.read()
    c.close()
    return code, json.loads(raw or b"{}")


def test_get_item_timeline_returns_five_stages(site):
    cfg, port = site
    SR.append_stage_run(cfg.status_dir, item_slug="probe", stage="build",
                        status="success", task_id="t1",
                        started_at="2026-11-17T10:00:00-05:00")
    code, body = _get_json(port, "/api/item-timeline/probe")
    assert code == 200
    assert body["slug"] == "probe"
    assert len(body["stages"]) == 5
    by_stage = {s["stage"]: s for s in body["stages"]}
    assert by_stage["build"]["status"] == "success"
    assert by_stage["design"]["status"] == "none"
    assert by_stage["merge"]["count"] == 0


def test_get_item_timeline_unknown_slug_returns_empty_envelope(site):
    """Unknown slugs return 200 with five none-stages so the UI never
    has to branch on 404 vs. empty."""
    _, port = site
    code, body = _get_json(port, "/api/item-timeline/never-touched")
    assert code == 200
    assert body["slug"] == "never-touched"
    assert all(s["status"] == "none" for s in body["stages"])


def test_post_stage_run_route_is_wired(site):
    """POST /api/stage-run is reachable through the dispatcher — same wire
    shape as /api/effort and /api/woy-action."""
    cfg, port = site
    code, body = _post_json(port, "/api/stage-run", {
        "key": HTTP_KEY,
        "item_slug": "wire-test",
        "stage": "design",
        "status": "success",
        "task_id": "t1",
    })
    assert code == 200
    assert body["ok"] is True
    assert body["record"]["stage"] == "design"
    runs = SR.read_stage_runs(cfg.status_dir, item_slug="wire-test")
    assert len(runs) == 1


def test_post_stage_run_bad_key_returns_403(site):
    _, port = site
    code, body = _post_json(port, "/api/stage-run", {
        "key": "nope", "item_slug": "x", "stage": "build", "status": "success",
    })
    assert code == 403


def test_post_then_get_full_lifecycle(site):
    """Open with running, close with success, aggregate via GET."""
    cfg, port = site
    code1, _ = _post_json(port, "/api/stage-run", {
        "key": HTTP_KEY, "item_slug": "lifecycle",
        "stage": "build", "status": "running", "task_id": "bg-9",
        "started_at": "2026-11-17T10:00:00-05:00",
    })
    code2, _ = _post_json(port, "/api/stage-run", {
        "key": HTTP_KEY, "item_slug": "lifecycle",
        "stage": "build", "status": "success", "task_id": "bg-9",
        "started_at": "2026-11-17T10:00:00-05:00",
        "ended_at": "2026-11-17T10:05:00-05:00",
        "summary": "worker merged PR",
    })
    assert code1 == code2 == 200
    code, body = _get_json(port, "/api/item-timeline/lifecycle")
    assert code == 200
    by_stage = {s["stage"]: s for s in body["stages"]}
    assert by_stage["build"]["status"] == "success"
    assert by_stage["build"]["count"] == 1
    assert by_stage["build"]["latest"]["summary"] == "worker merged PR"
