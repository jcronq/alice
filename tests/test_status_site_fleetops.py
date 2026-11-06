"""Worker-usage digest (#641)."""

from __future__ import annotations

import json

from status_site import fleetops


RECS = [
    {"lane": "thinking", "duration_s": 100, "cost_usd": 1.0, "ci_passed": True},
    {"lane": "thinking", "duration_ms": 300_000, "total_cost_usd": 3.0, "status": "failed", "ci_passed": False},
    {"lane": "sonnet", "started": "2026-11-06T10:00:00Z", "ended": "2026-11-06T10:10:00Z", "cost_usd": 0.5},
]


def test_family_split_and_totals():
    d = fleetops.digest(RECS, primary_lanes=["thinking"])
    p, o, all_ = d["families"]["primary"], d["families"]["other"], d["overall"]
    assert p["runs"] == 2 and o["runs"] == 1 and all_["runs"] == 3
    assert p["cost_usd_total"] == 4.0
    assert p["cost_usd_per_run"] == 2.0
    assert p["lead_time_median_s"] == 200.0
    assert p["change_failure_rate"] == 0.5
    assert p["ci_pass_rate"] == 0.5
    assert o["lead_time_median_s"] == 600.0
    assert all_["cost_usd_total"] == 4.5


def test_no_data_yields_none_not_zero():
    d = fleetops.digest([], primary_lanes=["x"])
    for s in (d["overall"], *d["families"].values()):
        assert s["runs"] == 0
        assert s["change_failure_rate"] is None
        assert s["ci_pass_rate"] is None
        assert s["cost_usd_total"] is None


def test_since_epoch_filters_old_but_keeps_untimestamped():
    recs = [
        {"ended": "2026-01-01T00:00:00Z", "cost_usd": 9},
        {"ended": "2026-11-06T00:00:00Z", "cost_usd": 1},
        {"cost_usd": 2},
    ]
    since = fleetops._epoch("2026-06-01T00:00:00Z")
    d = fleetops.digest(recs, since_epoch=since)
    assert d["overall"]["runs"] == 2
    assert d["overall"]["cost_usd_total"] == 3.0


def test_malformed_values_ignored():
    recs = [{"duration_s": "fast", "cost_usd": True, "ci_passed": "yes"}, "junk", None]
    d = fleetops.digest(recs)
    assert d["overall"]["runs"] == 1
    assert d["overall"]["lead_time_median_s"] is None
    assert d["overall"]["cost_usd_total"] is None
    assert d["overall"]["ci_pass_rate"] is None


def test_negative_duration_dropped():
    assert fleetops.duration_seconds({"started": "2026-11-06T11:00:00Z", "ended": "2026-11-06T10:00:00Z"}) is None


def test_read_jsonl_tolerant(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text(json.dumps({"a": 1}) + "\nnot json\n\n[1]\n" + json.dumps({"b": 2}) + "\n")
    assert fleetops.read_jsonl(p) == [{"a": 1}, {"b": 2}]
    assert fleetops.read_jsonl(tmp_path / "missing.jsonl") == []
