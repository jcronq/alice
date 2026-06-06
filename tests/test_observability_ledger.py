"""Tests for :mod:`core.observability_ledger`.

The module reads/writes a single JSON file at the path bound to
``core.observability_ledger.LEDGER_PATH``. Every test monkeypatches that
constant to point at a ``tmp_path``-scoped file so we never touch the
real vault.

Coverage:

* Each signal type can be appended cleanly.
* Auto-useful derivation matches the per-signal heuristic.
* Explicit ``useful=False`` overrides the heuristic.
* Unknown signal types raise ``ValueError``.
* ``query_signals`` filters by signal type and start time.
* ``prune_entries`` drops old entries and returns per-bucket counts.
* Concurrent writers don't corrupt the file (forked processes, both
  entries land).
* Initialising against a missing ledger file is idempotent — first
  ``append_signal`` creates the file with the empty schema, second one
  appends to it.
* Malformed JSON on disk does not crash callers.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from core import observability_ledger as ol


_EDT = timezone(timedelta(hours=-4))


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    """Bind LEDGER_PATH to a tmp-scoped file. Each test starts clean."""
    path = tmp_path / "observability-ledger.json"
    monkeypatch.setattr(ol, "LEDGER_PATH", str(path))
    return path


def _read_ledger(path) -> dict:
    with open(path) as f:
        return json.load(f)


def test_append_each_signal_type_lands(ledger_path):
    ol.append_signal(
        "cozylobe_classification",
        input={"event_type": "motion", "room": "playroom"},
        outcome="consumed",
    )
    ol.append_signal(
        "stage_b_routing",
        input={"note_type": "observation", "urgency": "low"},
        outcome="consumed",
    )
    ol.append_signal(
        "speaking_transport",
        input={"signal_type": "surface", "priority": "insight"},
        outcome="acted_on",
    )

    data = _read_ledger(ledger_path)
    for sig in ol.SIGNAL_TYPES:
        entries = data["signals"][sig]["entries"]
        assert len(entries) == 1, f"{sig} did not receive its entry"
        assert entries[0]["useful"] is True
        assert "ts" in entries[0]


@pytest.mark.parametrize(
    "signal_type,outcome,expected_useful",
    [
        ("cozylobe_classification", "consumed", True),
        ("cozylobe_classification", "acted_on", True),
        ("cozylobe_classification", "deferred", False),
        ("cozylobe_classification", "dropped", False),
        ("cozylobe_classification", "ignored", False),
        ("stage_b_routing", "consumed", True),
        ("stage_b_routing", "deferred", False),
        ("stage_b_routing", "dropped", False),
        ("speaking_transport", "acted_on", True),
        ("speaking_transport", "ignored", False),
        ("speaking_transport", "overridden", False),
    ],
)
def test_auto_useful_heuristic(ledger_path, signal_type, outcome, expected_useful):
    ol.append_signal(signal_type, input={"x": 1}, outcome=outcome)
    entry = _read_ledger(ledger_path)["signals"][signal_type]["entries"][-1]
    assert entry["useful"] is expected_useful


def test_explicit_useful_overrides_heuristic(ledger_path):
    # outcome "consumed" would auto-flag useful=True for cozylobe — override it.
    ol.append_signal(
        "cozylobe_classification",
        input={"event_type": "motion"},
        outcome="consumed",
        useful=False,
    )
    entry = _read_ledger(ledger_path)["signals"]["cozylobe_classification"][
        "entries"
    ][-1]
    assert entry["useful"] is False


def test_invalid_signal_type_raises(ledger_path):
    with pytest.raises(ValueError):
        ol.append_signal("not_a_signal", input={}, outcome="consumed")


def test_query_signals_filters_by_type(ledger_path):
    ol.append_signal(
        "cozylobe_classification", input={"r": "playroom"}, outcome="consumed"
    )
    ol.append_signal(
        "stage_b_routing", input={"n": "observation"}, outcome="consumed"
    )

    cozy = ol.query_signals(signal_type="cozylobe_classification")
    assert len(cozy) == 1
    assert cozy[0]["signal_type"] == "cozylobe_classification"

    all_entries = ol.query_signals()
    assert len(all_entries) == 2


def test_query_signals_filters_by_since(ledger_path):
    old_ts = (datetime.now(_EDT) - timedelta(days=5)).isoformat()
    new_ts = datetime.now(_EDT).isoformat()
    ol.append_signal(
        "cozylobe_classification",
        input={"k": "old"},
        outcome="consumed",
        ts=old_ts,
    )
    ol.append_signal(
        "cozylobe_classification",
        input={"k": "new"},
        outcome="consumed",
        ts=new_ts,
    )

    since = (datetime.now(_EDT) - timedelta(days=1)).isoformat()
    recent = ol.query_signals(signal_type="cozylobe_classification", since_iso=since)
    assert len(recent) == 1
    assert recent[0]["input"]["k"] == "new"


def test_query_signals_rejects_unknown_type(ledger_path):
    with pytest.raises(ValueError):
        ol.query_signals(signal_type="nope")


def test_query_signals_on_missing_file_returns_empty(ledger_path):
    # ledger_path fixture does NOT create the file
    assert not os.path.exists(ledger_path)
    assert ol.query_signals() == []


def test_prune_entries_removes_old_and_returns_counts(ledger_path):
    old_ts = (datetime.now(_EDT) - timedelta(days=45)).isoformat()
    new_ts = datetime.now(_EDT).isoformat()

    # Two old, one new in cozylobe; one old in speaking; nothing in stage_b.
    ol.append_signal(
        "cozylobe_classification",
        input={"i": 1},
        outcome="consumed",
        ts=old_ts,
    )
    ol.append_signal(
        "cozylobe_classification",
        input={"i": 2},
        outcome="consumed",
        ts=old_ts,
    )
    ol.append_signal(
        "cozylobe_classification",
        input={"i": 3},
        outcome="consumed",
        ts=new_ts,
    )
    ol.append_signal(
        "speaking_transport",
        input={"i": 4},
        outcome="acted_on",
        ts=old_ts,
    )

    counts = ol.prune_entries(max_age_days=30)
    assert counts == {
        "cozylobe_classification": 2,
        "stage_b_routing": 0,
        "speaking_transport": 1,
    }

    data = _read_ledger(ledger_path)
    cozy_entries = data["signals"]["cozylobe_classification"]["entries"]
    assert len(cozy_entries) == 1
    assert cozy_entries[0]["input"]["i"] == 3
    assert data["signals"]["speaking_transport"]["entries"] == []


def test_prune_entries_on_missing_file_returns_zeros(ledger_path):
    assert not os.path.exists(ledger_path)
    counts = ol.prune_entries(max_age_days=30)
    assert counts == {sig: 0 for sig in ol.SIGNAL_TYPES}


def test_concurrent_appends_do_not_corrupt(ledger_path):
    """Fork two writers; both entries must land and the file stays valid JSON."""
    pid = os.fork()
    if pid == 0:
        # Child writer.
        try:
            ol.append_signal(
                "cozylobe_classification",
                input={"who": "child"},
                outcome="consumed",
            )
        finally:
            os._exit(0)
    else:
        ol.append_signal(
            "cozylobe_classification",
            input={"who": "parent"},
            outcome="consumed",
        )
        os.waitpid(pid, 0)

    data = _read_ledger(ledger_path)
    entries = data["signals"]["cozylobe_classification"]["entries"]
    authors = sorted(e["input"]["who"] for e in entries)
    assert authors == ["child", "parent"]


def test_idempotent_init_on_empty_ledger(ledger_path):
    # No file yet — first append creates it.
    assert not os.path.exists(ledger_path)
    ol.append_signal(
        "stage_b_routing", input={"n": "observation"}, outcome="consumed"
    )
    assert os.path.exists(ledger_path)
    data = _read_ledger(ledger_path)
    assert data["version"] == 1
    assert set(data["signals"].keys()) >= set(ol.SIGNAL_TYPES)
    assert len(data["signals"]["stage_b_routing"]["entries"]) == 1

    # Second append must not reset the file.
    ol.append_signal(
        "stage_b_routing", input={"n": "decision"}, outcome="deferred"
    )
    data = _read_ledger(ledger_path)
    assert len(data["signals"]["stage_b_routing"]["entries"]) == 2


def test_malformed_json_does_not_crash(ledger_path):
    # Write garbage to the ledger then append — caller must not raise.
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    with open(ledger_path, "w") as f:
        f.write("{ this is not json")

    ol.append_signal(
        "cozylobe_classification",
        input={"recovered": True},
        outcome="consumed",
    )
    data = _read_ledger(ledger_path)
    assert (
        data["signals"]["cozylobe_classification"]["entries"][0]["input"][
            "recovered"
        ]
        is True
    )
