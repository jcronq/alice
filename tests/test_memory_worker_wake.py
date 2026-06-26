"""Smoke tests for :mod:`alice_thinking.memory_worker.wake`.

Phase 1 wake is a journal-replay + heartbeat emit; we cover the
config-disabled short-circuit, the heartbeat event content, and
the override knob for the journal path.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_thinking.memory_worker import (
    correction_cascade_auto_propagate as _autoprop,
    journal,
    wake,
)


@pytest.fixture(autouse=True)
def _reset_autoprop_dry_run_state():
    """Reset the module-level ``_DRY_RUN`` global between tests.

    ``auto_propagate`` mutates ``_autoprop._DRY_RUN`` when called with
    ``dry_run=`` set. Other test files (notably
    ``test_correction_cascade_auto_propagate.py``) leak ``dry_run=True``
    into the global, which then prevents the per-wake hook here from
    actually mutating the vault. We snapshot + restore around every test
    in this module so the wake tests see the production default.
    """
    saved = _autoprop._DRY_RUN
    _autoprop._DRY_RUN = False
    try:
        yield
    finally:
        _autoprop._DRY_RUN = saved


def _write_config(mind: pathlib.Path, blob: dict) -> None:
    cfg_dir = mind / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "alice.config.json").write_text(json.dumps(blob))


def test_disabled_short_circuit_is_noop(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_worker.enabled=false → exit 0, no event log written."""
    mind = tmp_path / "mind"
    log = tmp_path / "memory-worker.log"
    _write_config(mind, {"memory_worker": {"enabled": False}})

    monkeypatch.setattr(
        "sys.argv",
        [
            "memory-worker",
            "--mind",
            str(mind),
            "--log",
            str(log),
        ],
    )
    monkeypatch.setattr(wake, "MEMORY_WORKER_LIVENESS_PATH", tmp_path / "liveness")

    rc = wake.main()
    assert rc == 0
    # Heartbeat is suppressed when disabled — no log writes.
    assert not log.is_file()


def test_heartbeat_event_carries_replay_report(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One wake → one ``memory_worker_heartbeat`` event with replay stats."""
    mind = tmp_path / "mind"
    log = tmp_path / "memory-worker.log"
    journal_path = tmp_path / "journal.jsonl"
    _write_config(
        mind,
        {
            "memory_worker": {
                "enabled": True,
                "cadence_minutes": 30,
                "journal_path": str(journal_path),
                "stage_d_model": "local",
                "stage_d_api_tier_enabled": False,
            }
        },
    )

    # Seed one pending entry so the replay report has non-zero
    # inspected count to assert on.
    journal.append(
        journal_path,
        op="atomize",
        source="cortex-memory/research/foo.md",
        journal_id="seed-1",
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "memory-worker",
            "--mind",
            str(mind),
            "--log",
            str(log),
        ],
    )
    monkeypatch.setattr(wake, "MEMORY_WORKER_LIVENESS_PATH", tmp_path / "liveness")

    rc = wake.main()
    assert rc == 0
    assert (tmp_path / "liveness").is_file()
    assert log.is_file()

    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "memory_worker_heartbeat"
    # Phase 2 onward: the heartbeat advertises the latest stage
    # that landed. Phase 1's "scaffold" was a placeholder.
    assert record["phase"] == "stage_b"
    assert record["cadence_minutes"] == 30
    assert record["stage_d_model"] == "local"
    assert record["stage_d_api_tier_enabled"] is False
    # Replay report fields.
    assert record["inspected"] == 1
    # Phase 1 stub returns False → skipped, not committed.
    assert record["skipped"] == 1
    assert record["committed"] == 0


def test_per_wake_correction_cascade_propagation_fires(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake with an unpropagated correction in the vault should run
    auto-propagation as a pre-stage hook (NOT gated to Stage C nights).

    Asserts:
        a) the referencing note picked up the correction wikilink, and
        b) a ``correction_cascade_auto_propagate`` event was emitted to
           events.jsonl with ``trigger="periodic_wake"``.

    Motivated by tonight's 191-correction backlog: Stage C only fires
    nightly; corrections accumulated for 18h before manual drain. The
    per-wake hook lets the 30-min cadence drain them.
    """
    mind = tmp_path / "mind"
    log = tmp_path / "memory-worker.log"
    journal_path = tmp_path / "journal.jsonl"

    # Minimal vault: a correction, the corrected note, and a referencing
    # note that cites the corrected note but not the correction.
    vault = mind / "cortex-memory"
    (vault / "research").mkdir(parents=True)
    (mind / "memory").mkdir(parents=True)

    (vault / "research" / "foo-correction.md").write_text(
        "---\nnote_type: correction\nslug: foo-correction\n"
        "supersedes: foo\n---\n\n"
        "Corrected to 98.1% (159/162).\n",
        encoding="utf-8",
    )
    (vault / "research" / "foo.md").write_text(
        "---\nslug: foo\n---\n\n"
        "Original claim said 12% (4/32).\n",
        encoding="utf-8",
    )
    ref_md = vault / "research" / "ref.md"
    ref_md.write_text(
        "---\nslug: ref\n---\n\n"
        "Citing [[foo]] for the original figure.\n",
        encoding="utf-8",
    )

    _write_config(
        mind,
        {
            "memory_worker": {
                "enabled": True,
                "cadence_minutes": 30,
                "journal_path": str(journal_path),
            }
        },
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "memory-worker",
            "--mind",
            str(mind),
            "--log",
            str(log),
        ],
    )
    monkeypatch.setattr(wake, "MEMORY_WORKER_LIVENESS_PATH", tmp_path / "liveness")

    rc = wake.main()
    assert rc == 0

    # (a) The referencing note now wikilinks the correction.
    new_ref = ref_md.read_text(encoding="utf-8")
    assert "[[foo-correction" in new_ref, (
        "ref.md should pick up the correction wikilink during the per-wake "
        f"hook, got: {new_ref!r}"
    )

    # (b) The events.jsonl received a periodic_wake-tagged propagation event.
    events_path = mind / "memory" / "events.jsonl"
    assert events_path.is_file(), "events.jsonl should exist after propagation"
    events = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]
    autoprop_events = [
        e for e in events if e.get("type") == "correction_cascade_auto_propagate"
    ]
    assert len(autoprop_events) >= 1, (
        f"expected at least one correction_cascade_auto_propagate event, "
        f"got events of types {[e.get('type') for e in events]}"
    )
    # The per-wake hook should carry the periodic_wake trigger tag.
    assert any(
        e.get("trigger") == "periodic_wake" for e in autoprop_events
    ), f"expected trigger=periodic_wake, got triggers {[e.get('trigger') for e in autoprop_events]}"


def test_per_wake_hook_skips_when_nothing_to_propagate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty vault → no propagation event (skip when total_unpropagated == 0)."""
    mind = tmp_path / "mind"
    log = tmp_path / "memory-worker.log"
    journal_path = tmp_path / "journal.jsonl"
    (mind / "cortex-memory").mkdir(parents=True)
    (mind / "memory").mkdir(parents=True)

    _write_config(
        mind,
        {
            "memory_worker": {
                "enabled": True,
                "journal_path": str(journal_path),
            }
        },
    )

    monkeypatch.setattr(
        "sys.argv",
        ["memory-worker", "--mind", str(mind), "--log", str(log)],
    )
    monkeypatch.setattr(wake, "MEMORY_WORKER_LIVENESS_PATH", tmp_path / "liveness")

    rc = wake.main()
    assert rc == 0

    events_path = mind / "memory" / "events.jsonl"
    if events_path.is_file():
        events = [
            json.loads(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
        ]
        autoprop_events = [
            e for e in events if e.get("type") == "correction_cascade_auto_propagate"
        ]
        assert autoprop_events == [], (
            "no propagation event should be emitted when nothing to propagate"
        )


def test_per_wake_hook_respects_enabled_false(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_worker.enabled=false short-circuits BEFORE the per-wake hook
    runs, so a vault with pending corrections gets no propagation."""
    mind = tmp_path / "mind"
    log = tmp_path / "memory-worker.log"
    vault = mind / "cortex-memory"
    (vault / "research").mkdir(parents=True)
    (mind / "memory").mkdir(parents=True)

    # Seed a propagatable correction — the hook would fire if enabled.
    (vault / "research" / "foo-correction.md").write_text(
        "---\nnote_type: correction\nslug: foo-correction\n"
        "supersedes: foo\n---\n\nCorrected to 98.1% (159/162).\n",
        encoding="utf-8",
    )
    (vault / "research" / "foo.md").write_text(
        "---\nslug: foo\n---\n\nOriginal claim.\n", encoding="utf-8"
    )
    (vault / "research" / "ref.md").write_text(
        "---\nslug: ref\n---\n\nCiting [[foo]].\n", encoding="utf-8"
    )

    _write_config(mind, {"memory_worker": {"enabled": False}})

    monkeypatch.setattr(
        "sys.argv",
        ["memory-worker", "--mind", str(mind), "--log", str(log)],
    )
    monkeypatch.setattr(wake, "MEMORY_WORKER_LIVENESS_PATH", tmp_path / "liveness")

    rc = wake.main()
    assert rc == 0
    # No event log written when disabled.
    assert not log.is_file()
    # Vault untouched.
    assert "[[foo-correction" not in (vault / "research" / "ref.md").read_text(
        encoding="utf-8"
    )


def test_journal_cli_override_wins_over_config(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--journal supersedes memory_worker.journal_path."""
    mind = tmp_path / "mind"
    log = tmp_path / "memory-worker.log"
    cfg_journal = tmp_path / "from-config.jsonl"
    cli_journal = tmp_path / "from-cli.jsonl"
    _write_config(
        mind,
        {"memory_worker": {"enabled": True, "journal_path": str(cfg_journal)}},
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "memory-worker",
            "--mind",
            str(mind),
            "--log",
            str(log),
            "--journal",
            str(cli_journal),
        ],
    )
    monkeypatch.setattr(wake, "MEMORY_WORKER_LIVENESS_PATH", tmp_path / "liveness")

    rc = wake.main()
    assert rc == 0

    record = json.loads(log.read_text().splitlines()[0])
    assert record["journal_path"] == str(cli_journal)
