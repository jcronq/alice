"""Smoke tests for :mod:`alice_thinking.memory_worker.wake`.

Phase 1 wake is a journal-replay + heartbeat emit; we cover the
config-disabled short-circuit, the heartbeat event content, and
the override knob for the journal path.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_thinking.memory_worker import journal, wake


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
