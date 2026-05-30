"""Tests for the raw SSE event JSONL logger (issue #401).

Covers:

* Unit — writes 3 events, reads back the file, asserts schema +
  filename + append behavior.
* Unit — date roll across UTC midnight produces two files.
* Unit — disabled logger is a no-op (no filesystem touch).
* Unit — write failure (unwritable parent) logs a warning and does
  NOT raise.
* Integration — stub SSE stream with motion + light events, run the
  wake loop, assert only INPUT_KINDS events land in the JSONL AND
  duplicate sensor-still-on events are preserved BEFORE the throttle.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import datetime, timezone

import pytest

from alice_cozylobe.event_log import (
    DEFAULT_EVENT_LOG_ROOT,
    SseEventLogger,
)
from alice_cozylobe.events import CozyHemEvent
from alice_cozylobe.throttle import Throttle
from alice_cozylobe.wake_loop import WakeLoop
from core.events import CapturingEmitter


# ---------------------------------------------------------------------------
# helpers


def _evt(
    entity_id: str = "binary_sensor.kitchen_motion",
    kind: str = "entity:update",
    *,
    payload: dict | None = None,
    received_at: float = 0.0,
) -> CozyHemEvent:
    return CozyHemEvent(
        kind=kind,
        entity_id=entity_id,
        payload=payload or {},
        received_at=received_at,
    )


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    text = path.read_text().strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


# ---------------------------------------------------------------------------
# 1. unit — write three events, schema + filename + append


def test_logger_writes_three_events_with_correct_schema(tmp_path):
    logger = SseEventLogger(root=tmp_path)
    logger.log(_evt("binary_sensor.kitchen_motion", payload={"state": "on"}))
    logger.log(_evt("binary_sensor.front_door", payload={"state": "off"}))
    logger.log(
        _evt(
            "binary_sensor.kitchen_motion",
            payload={"state": "on", "battery": 87},
        )
    )
    logger.close()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = tmp_path / f"{today}.jsonl"
    assert path.exists(), f"expected {path} to exist"

    records = _read_jsonl(path)
    assert len(records) == 3
    for r in records:
        assert set(r.keys()) == {"ts", "entity_id", "kind", "state"}
        # ts shape: ISO-8601 UTC with ms + Z.
        assert r["ts"].endswith("Z")
        assert "T" in r["ts"]
        # ms precision: exactly 3 digits after the seconds dot.
        seconds_part = r["ts"].split("T", 1)[1].rstrip("Z")
        assert "." in seconds_part
        ms = seconds_part.split(".")[1]
        assert len(ms) == 3, f"ts ms precision wrong: {r['ts']}"

    assert records[0]["entity_id"] == "binary_sensor.kitchen_motion"
    assert records[0]["kind"] == "entity:update"
    assert records[0]["state"] == {"state": "on"}
    assert records[1]["entity_id"] == "binary_sensor.front_door"
    assert records[2]["state"] == {"state": "on", "battery": 87}


def test_logger_appends_to_existing_file(tmp_path):
    """Two logger instances pointing at the same dir + date append,
    never truncate."""
    logger1 = SseEventLogger(root=tmp_path)
    logger1.log(_evt("a"))
    logger1.close()

    logger2 = SseEventLogger(root=tmp_path)
    logger2.log(_evt("b"))
    logger2.close()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    records = _read_jsonl(tmp_path / f"{today}.jsonl")
    assert [r["entity_id"] for r in records] == ["a", "b"]


def test_default_root_is_alice_mind_cozylobe_cortex_events():
    """Sanity check the documented default path so we don't drift it
    silently. Future PRs that need a different location should update
    this assertion deliberately."""
    expected = (
        pathlib.Path.home()
        / "alice-mind"
        / "cozylobe-cortex"
        / "events"
    )
    assert DEFAULT_EVENT_LOG_ROOT == expected


# ---------------------------------------------------------------------------
# 2. unit — date rollover at UTC midnight produces two files


def test_date_rollover_creates_new_file(tmp_path):
    fake_now = [datetime(2026, 5, 26, 23, 59, 30, 100_000, tzinfo=timezone.utc)]

    def clock() -> datetime:
        return fake_now[0]

    logger = SseEventLogger(root=tmp_path, clock=clock)
    logger.log(_evt("before_midnight", payload={"state": "on"}))

    # Cross UTC midnight.
    fake_now[0] = datetime(
        2026, 5, 27, 0, 0, 15, 500_000, tzinfo=timezone.utc
    )
    logger.log(_evt("after_midnight", payload={"state": "off"}))
    logger.close()

    before = tmp_path / "2026-05-26.jsonl"
    after = tmp_path / "2026-05-27.jsonl"
    assert before.exists()
    assert after.exists()

    rec_before = _read_jsonl(before)
    rec_after = _read_jsonl(after)
    assert len(rec_before) == 1
    assert len(rec_after) == 1
    assert rec_before[0]["entity_id"] == "before_midnight"
    assert rec_after[0]["entity_id"] == "after_midnight"
    # The two timestamps land in their respective UTC days.
    assert rec_before[0]["ts"].startswith("2026-05-26T")
    assert rec_after[0]["ts"].startswith("2026-05-27T")


def test_same_day_writes_share_one_file(tmp_path):
    """No spurious rotation when the clock stays inside one UTC day."""
    fake_now = [datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)]

    def clock() -> datetime:
        return fake_now[0]

    logger = SseEventLogger(root=tmp_path, clock=clock)
    for i in range(5):
        logger.log(_evt(f"e{i}"))
        fake_now[0] = fake_now[0].replace(second=i + 1)
    logger.close()

    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["2026-05-26.jsonl"]
    assert len(_read_jsonl(tmp_path / "2026-05-26.jsonl")) == 5


# ---------------------------------------------------------------------------
# 3. unit — disabled is a no-op


def test_disabled_logger_writes_nothing(tmp_path):
    logger = SseEventLogger(root=tmp_path, enabled=False)
    for _ in range(10):
        logger.log(_evt())
    logger.close()
    # No directory or file should have been created.
    assert not tmp_path.exists() or not any(tmp_path.iterdir())


# ---------------------------------------------------------------------------
# 4. unit — write failure does not raise


def test_write_failure_does_not_raise(tmp_path, caplog):
    """If the parent path collides with an existing file, mkdir fails;
    the logger must log a warning and return silently."""
    blocker = tmp_path / "blocker"
    blocker.write_text("this is a file, not a directory")

    logger = SseEventLogger(root=blocker / "events")
    with caplog.at_level("WARNING"):
        # Should not raise.
        logger.log(_evt())
        # Repeat call — should not produce a second warning (warn-once).
        logger.log(_evt())
    logger.close()

    warnings = [
        rec for rec in caplog.records
        if rec.levelname == "WARNING" and "event_log" in rec.message
    ]
    assert len(warnings) == 1, "expected exactly one warn-once message"


# ---------------------------------------------------------------------------
# 5. integration — wake loop only logs INPUT_KINDS, preserves pre-throttle


class _NullRunAgent:
    """Stub for ``run_agent_fn`` — accepts everything, returns None."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, agent_spec, *, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return None


@pytest.mark.asyncio
async def test_event_logger_hook_only_logs_input_kinds_and_preserves_duplicates(
    tmp_path, monkeypatch
):
    """End-to-end: feed a mix of motion / door / light events through
    the wake loop with the EventLogger installed and assert:

    * Light brightness ``entity:update`` events (NOT on INPUT_KINDS and
      NOT matching any tracked-input-entity glob) never reach the JSONL.
    * Motion events DO reach the JSONL, including the duplicate
      ``state=on`` fire that downstream throttle would either pass-as-
      non-micro-change or coalesce — the logger sits pre-throttle so
      timing information is preserved.
    * Doorbell-pressed kind (canonical INPUT_KINDS entry) reaches the
      JSONL.
    """
    # Surfaces write into a per-test mind dir to keep the run hermetic.
    monkeypatch.setattr(
        "alice_cozylobe.surfaces.DEFAULT_MIND", tmp_path / "mind"
    )

    log_root = tmp_path / "events"
    event_logger = SseEventLogger(root=log_root)

    # Real throttle with default config, pointed at a non-existent yaml
    # so the shipped defaults apply (INPUT_KINDS + entity-glob patterns).
    cfg_path = tmp_path / "throttle.yaml"
    throttle = Throttle(config_path=cfg_path)

    emitter = CapturingEmitter()
    stub_run = _NullRunAgent()

    loop = WakeLoop(
        emitter=emitter,
        llm_client=None,
        run_agent_fn=stub_run,
        throttle=throttle,
        event_logger=event_logger,
    )

    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()

    # Mix of INPUT and OUTPUT-class events.
    fixtures = [
        # INPUT: motion sensor fires on (matches binary_sensor.*_motion).
        _evt(
            "binary_sensor.kitchen_motion",
            kind="entity:update",
            payload={"state": "on"},
            received_at=1.0,
        ),
        # INPUT: same sensor still on. Duplicate state — the throttle
        # would PASS this (no field change → no micro-delta), but the
        # logger captures it regardless because it sits pre-throttle.
        _evt(
            "binary_sensor.kitchen_motion",
            kind="entity:update",
            payload={"state": "on"},
            received_at=2.0,
        ),
        # OUTPUT: circadian brightness tick on a light. NOT on
        # INPUT_KINDS and entity_id doesn't match any tracked glob.
        # Logger must NOT see this.
        _evt(
            "light.living_room",
            kind="entity:update",
            payload={"brightness": 0.52},
            received_at=3.0,
        ),
        # INPUT: doorbell pressed (canonical INPUT_KINDS member).
        _evt(
            "binary_sensor.front_doorbell",
            kind="doorbell_pressed",
            payload={"state": "on"},
            received_at=4.0,
        ),
    ]
    for evt in fixtures:
        await queue.put(evt)

    task = asyncio.create_task(loop.run(queue, stop))
    # Spin until the queue drains.
    for _ in range(400):
        if queue.empty():
            break
        await asyncio.sleep(0.01)
    # Allow the wake loop one extra tick to finish the last event's
    # handler before we shut down.
    await asyncio.sleep(0.05)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    event_logger.close()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = log_root / f"{today}.jsonl"
    assert log_path.exists(), "expected event log file to be created"
    records = _read_jsonl(log_path)

    entity_ids = [r["entity_id"] for r in records]
    # Light brightness tick was filtered out by INPUT_KINDS before the
    # logger hook fired.
    assert "light.living_room" not in entity_ids, (
        f"output-class light event leaked into corpus: {entity_ids}"
    )
    # Both kitchen_motion fires were captured — duplicates preserved.
    motion = [r for r in records if r["entity_id"] == "binary_sensor.kitchen_motion"]
    assert len(motion) == 2, (
        f"expected 2 motion entries (pre-throttle), got {len(motion)}: {entity_ids}"
    )
    # Doorbell captured.
    assert "binary_sensor.front_doorbell" in entity_ids


@pytest.mark.asyncio
async def test_event_logger_disabled_hook_writes_nothing(
    tmp_path, monkeypatch
):
    """Wake loop with a disabled logger must never touch the corpus
    directory, even though wiring is in place."""
    monkeypatch.setattr(
        "alice_cozylobe.surfaces.DEFAULT_MIND", tmp_path / "mind"
    )

    log_root = tmp_path / "events"
    event_logger = SseEventLogger(root=log_root, enabled=False)

    cfg_path = tmp_path / "throttle.yaml"
    throttle = Throttle(config_path=cfg_path)
    emitter = CapturingEmitter()
    stub_run = _NullRunAgent()

    loop = WakeLoop(
        emitter=emitter,
        llm_client=None,
        run_agent_fn=stub_run,
        throttle=throttle,
        event_logger=event_logger,
    )

    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    await queue.put(
        _evt(
            "binary_sensor.kitchen_motion",
            payload={"state": "on"},
            received_at=1.0,
        )
    )

    task = asyncio.create_task(loop.run(queue, stop))
    for _ in range(200):
        if queue.empty():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    event_logger.close()

    assert not log_root.exists() or not any(log_root.iterdir())
