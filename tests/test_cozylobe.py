"""Tests for the cozylobe walking skeleton (issue #344).

Covers the four required pieces from the task spec:

* SSE event consumer parses a sample event correctly.
* Wake loop tick calls run_agent with the registered cozylobe AgentSpec.
* Surface emission writes a properly-formed file (note + surface).
* qwen-endpoint-unreachable path doesn't crash the loop.

Plus the AgentSpec-registration sanity checks that mirror the
existing pattern in :mod:`tests.test_agent_library`.

Tests use an in-process double for the httpx layer so we don't open
real sockets, and stub :func:`run_agent` so the asserts focus on the
cozylobe's behavior rather than the kernel chain.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
import pytest

from alice_cozylobe import (
    CozyHemEvent,
    QwenClassification,
    QwenClient,
    QwenUnreachable,
    SSEConsumer,
    WakeLoop,
    write_observation_note,
    write_urgent_surface,
)
from alice_cozylobe.surfaces import build_slug
from core.agent_library import default_registry
from core.events import CapturingEmitter
from core.kernel import KernelResult, UsageInfo


# ---------------------------------------------------------------------------
# Fake httpx layer for SSE consumer + qwen client tests.
#
# Mirrors :mod:`tests.test_cozyhem_subscriber` so the SSE producer is
# driven without real sockets and without burning wall clock during the
# reconnect-backoff assertions.


class _FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            await asyncio.sleep(0)
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeStreamCM:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    def __init__(self, scripts: list):
        """``scripts`` is a list where each entry is either:
        * list[str] — SSE lines for one connection, OR
        * Exception — raised from the ``stream()`` call to simulate
          a connection failure (used by reconnect-backoff cases).
        """
        self._scripts = scripts
        self._call_idx = 0

    def stream(self, method, url, **kwargs):
        script = self._scripts[self._call_idx]
        self._call_idx += 1
        if isinstance(script, Exception):
            raise script
        return _FakeStreamCM(_FakeStreamResponse(script))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _client_factory(scripts: list):
    """Return a factory that hands out a single shared fake client so
    successive reconnects step through ``scripts`` in order."""
    client = _FakeAsyncClient(scripts)
    return lambda: client


# ---------------------------------------------------------------------------
# SSE consumer
# ---------------------------------------------------------------------------


def test_sse_consumer_parses_doorbell_event() -> None:
    """One well-formed SSE frame becomes one CozyHemEvent on the queue."""
    payload = {"entity_id": "doorbell.front_door", "captured_at": 12345}
    lines = [
        "event: doorbell_pressed",
        f"data: {json.dumps(payload)}",
        "",  # blank line closes the event
    ]
    factory = _client_factory([lines])

    async def _run():
        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        stop = asyncio.Event()
        consumer = SSEConsumer(
            "http://example/api/v1/events",
            http_client_factory=factory,
            sleep=lambda *_: asyncio.sleep(0),
        )
        task = asyncio.create_task(consumer.run(queue, stop))
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return event

    event = asyncio.run(_run())
    assert isinstance(event, CozyHemEvent)
    assert event.kind == "doorbell_pressed"
    assert event.entity_id == "doorbell.front_door"
    assert event.payload == payload
    assert event.received_at > 0


def test_sse_consumer_handles_comment_and_malformed_data(caplog) -> None:
    """SSE heartbeat comments are ignored and malformed JSON data
    yields an event with an empty payload + a warning log."""
    lines = [
        ": heartbeat",
        "event: motion_detected",
        "data: not-json-{",
        "",
    ]
    factory = _client_factory([lines])

    async def _run():
        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        stop = asyncio.Event()
        consumer = SSEConsumer(
            "http://example/api/v1/events",
            http_client_factory=factory,
            sleep=lambda *_: asyncio.sleep(0),
        )
        task = asyncio.create_task(consumer.run(queue, stop))
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return event

    with caplog.at_level("WARNING"):
        event = asyncio.run(_run())
    assert event.kind == "motion_detected"
    assert event.payload == {}
    assert event.entity_id == ""


# ---------------------------------------------------------------------------
# Wake loop
# ---------------------------------------------------------------------------


class _StubRunAgent:
    """Stub for core.agent_library.run_agent.

    Captures the spec/prompt/correlation_id passed by the wake loop so
    the test can assert the cozylobe AgentSpec was dispatched (not, say,
    the thinking spec).
    """

    def __init__(self, *, raises: Optional[Exception] = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises

    async def __call__(
        self,
        agent,
        *,
        prompt,
        emitter,
        backend=None,
        correlation_id=None,
    ):
        self.calls.append(
            {
                "agent_name": agent.name,
                "prompt": prompt,
                "emitter": emitter,
                "backend": backend,
                "correlation_id": correlation_id,
            }
        )
        if self._raises is not None:
            raise self._raises
        return KernelResult(
            text="ok",
            session_id="sess-1",
            usage=UsageInfo(input_tokens=1, output_tokens=1),
            duration_ms=10,
            cost_usd=None,
            is_error=False,
            num_turns=1,
        )


class _StubQwen:
    """In-process qwen client double.

    Returns the supplied :class:`QwenClassification` on every call, or
    raises :class:`QwenUnreachable` when configured to. Lets tests
    cover both the happy-path and the lobe-quiet-on-link-loss path
    without standing up the real desktop-3090 endpoint.
    """

    def __init__(
        self,
        *,
        classification: Optional[QwenClassification] = None,
        unreachable: bool = False,
    ) -> None:
        self._classification = classification
        self._unreachable = unreachable
        self.call_count = 0

    async def classify(self, event, *, context=None) -> QwenClassification:
        self.call_count += 1
        if self._unreachable:
            raise QwenUnreachable("simulated outage")
        assert self._classification is not None
        return self._classification


def _make_event(kind: str = "entity:update", entity: str = "light.kitchen") -> CozyHemEvent:
    return CozyHemEvent(
        kind=kind,
        entity_id=entity,
        payload={"entity_id": entity, "from": "off", "to": "on"},
        received_at=time.time(),
    )


@pytest.mark.asyncio
async def test_wake_loop_dispatches_cozylobe_agent_spec(
    tmp_path, monkeypatch
) -> None:
    """One event in → run_agent called once with the registered cozylobe
    AgentSpec. The supervisor prompt carries the event + qwen
    classification.
    """
    # Surface writes must land in tmp_path so we don't touch the real vault.
    monkeypatch.setattr(
        "alice_cozylobe.surfaces.DEFAULT_MIND", tmp_path
    )

    emitter = CapturingEmitter()
    qwen = _StubQwen(
        classification=QwenClassification(
            urgency="MEDIUM",
            intent="log",
            summary="kitchen light on",
            reasoning="routine",
            raw={"actions": []},
        ),
    )
    stub_run = _StubRunAgent()
    loop = WakeLoop(
        emitter=emitter,
        qwen_client=qwen,
        run_agent_fn=stub_run,
    )

    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    event = _make_event()
    await queue.put(event)

    task = asyncio.create_task(loop.run(queue, stop))
    # Spin until the wake loop has drained the event.
    for _ in range(100):
        if stub_run.calls:
            break
        await asyncio.sleep(0.01)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    assert len(stub_run.calls) == 1
    call = stub_run.calls[0]
    assert call["agent_name"] == "cozylobe"
    assert "entity:update" in call["prompt"]
    assert "light.kitchen" in call["prompt"]
    assert "urgency=MEDIUM" in call["prompt"]
    assert call["correlation_id"].startswith("cozylobe-entity:update-")
    # Backstop note should have been dropped.
    notes = list((tmp_path / "inner" / "notes").iterdir())
    assert any("cozylobe" in p.name for p in notes)


@pytest.mark.asyncio
async def test_wake_loop_qwen_unreachable_does_not_crash(
    tmp_path, monkeypatch, caplog
) -> None:
    """When qwen raises QwenUnreachable, the wake loop logs once,
    skips the backstop note, dispatches the agent anyway with a
    "qwen unreachable" prompt block, and stays alive for the next
    event."""
    monkeypatch.setattr(
        "alice_cozylobe.surfaces.DEFAULT_MIND", tmp_path
    )

    emitter = CapturingEmitter()
    qwen = _StubQwen(unreachable=True)
    stub_run = _StubRunAgent()
    loop = WakeLoop(
        emitter=emitter,
        qwen_client=qwen,
        run_agent_fn=stub_run,
    )

    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    # Two events in a row — verify the warning fires once, not twice.
    await queue.put(_make_event(entity="light.living_room"))
    await queue.put(_make_event(entity="light.basement"))

    with caplog.at_level("WARNING"):
        task = asyncio.create_task(loop.run(queue, stop))
        for _ in range(200):
            if len(stub_run.calls) >= 2:
                break
            await asyncio.sleep(0.01)
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # Both events dispatched (the loop did NOT crash on QwenUnreachable).
    assert len(stub_run.calls) == 2
    for call in stub_run.calls:
        assert "UNREACHABLE" in call["prompt"]
    # qwen_unreachable telemetry fires on every event.
    unreachable_events = emitter.of_kind("cozylobe_qwen_unreachable")
    assert len(unreachable_events) == 2
    # But the WARNING log fires once per outage, not per event.
    warnings = [
        rec for rec in caplog.records
        if rec.levelname == "WARNING" and "qwen unreachable" in rec.message
    ]
    assert len(warnings) == 1
    # No backstop note since classification was None.
    notes_dir = tmp_path / "inner" / "notes"
    assert not notes_dir.exists() or not list(notes_dir.iterdir())


@pytest.mark.asyncio
async def test_wake_loop_critical_kind_fast_path_surfaces_without_agent(
    tmp_path, monkeypatch
) -> None:
    """doorbell_pressed → urgent surface, no qwen call, no run_agent."""
    monkeypatch.setattr(
        "alice_cozylobe.surfaces.DEFAULT_MIND", tmp_path
    )

    emitter = CapturingEmitter()
    qwen = _StubQwen(unreachable=True)
    stub_run = _StubRunAgent()
    loop = WakeLoop(
        emitter=emitter,
        qwen_client=qwen,
        run_agent_fn=stub_run,
    )

    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    await queue.put(
        CozyHemEvent(
            kind="doorbell_pressed",
            entity_id="doorbell.front_door",
            payload={"entity_id": "doorbell.front_door"},
            received_at=time.time(),
        )
    )
    task = asyncio.create_task(loop.run(queue, stop))
    for _ in range(100):
        if emitter.of_kind("cozylobe_critical_surfaced"):
            break
        await asyncio.sleep(0.01)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # No qwen call, no agent dispatch on the CRITICAL fast path.
    assert qwen.call_count == 0
    assert stub_run.calls == []
    # A surface file landed under inner/surface/.
    surfaces = list((tmp_path / "inner" / "surface").iterdir())
    assert len(surfaces) == 1
    assert "doorbell" in surfaces[0].name
    body = surfaces[0].read_text()
    assert "CRITICAL" in body
    assert "doorbell_pressed" in body


# ---------------------------------------------------------------------------
# Surface emitter
# ---------------------------------------------------------------------------


def test_write_observation_note_writes_well_formed_file(tmp_path) -> None:
    """The note carries the canonical frontmatter + body and lands in
    inner/notes/ under a deterministic filename."""
    fixed_ts = 1716552000.0  # 2024-05-24 10:40:00 UTC
    path = write_observation_note(
        "Living room light on during evening scene.",
        slug="light-living-room-on",
        tags=("lobe-observation", "lobe-low"),
        mind=tmp_path,
        now=fixed_ts,
    )
    assert path.is_file()
    assert path.parent == tmp_path / "inner" / "notes"
    assert path.name.endswith("-cozylobe-light-living-room-on.md")
    content = path.read_text()
    assert content.startswith("---\n")
    assert "created: 2024-05-24" in content
    assert "source: cozylobe" in content
    assert "tags: [lobe-low, lobe-observation]" in content
    assert "Living room light on" in content


def test_write_urgent_surface_writes_well_formed_file(tmp_path) -> None:
    """The surface carries the cozylobe-urgent prefix + carries extra
    frontmatter through to the file."""
    fixed_ts = 1716552000.0
    path = write_urgent_surface(
        "Doorbell pressed at front door.",
        slug="doorbell-front-door",
        mind=tmp_path,
        now=fixed_ts,
        extra_frontmatter={"urgency": "CRITICAL", "event_kind": "doorbell_pressed"},
    )
    assert path.parent == tmp_path / "inner" / "surface"
    assert "-cozylobe-urgent-doorbell-front-door.md" in path.name
    content = path.read_text()
    assert "urgency: CRITICAL" in content
    assert "event_kind: doorbell_pressed" in content
    assert "Doorbell pressed" in content


def test_build_slug_sanitizes_unsafe_chars() -> None:
    assert build_slug("Light.Living Room", "ON!") == "light-living-room-on"
    assert build_slug("") == "event"
    assert build_slug("!@#$") == "event"


# ---------------------------------------------------------------------------
# AgentSpec — registry sanity
# ---------------------------------------------------------------------------


def test_default_registry_includes_cozylobe() -> None:
    assert "cozylobe" in default_registry


def test_cozylobe_spec_carries_vault_boundary_rules() -> None:
    spec = default_registry.get("cozylobe")
    rule_ids = {rule.id for rule in spec.behavioral_constraints}
    assert "vault-read-only" in rule_ids
    assert "urgency-via-surface" in rule_ids
    assert "no-direct-cozyhem-mutation" in rule_ids
    assert "lobe-quiet-on-link-loss" in rule_ids


def test_cozylobe_spec_runs_background_always_on() -> None:
    """The lobe is supervisor-launched, not per-issue. Lifecycle and
    scope reflect that."""
    spec = default_registry.get("cozylobe")
    assert spec.scope == "background"
    assert spec.lifecycle == "always-on"


def test_cozylobe_build_spec_strips_signal_and_experiment_mcp() -> None:
    """The lobe's only escalation path is inner/surface/ — it must not
    reach Signal directly."""
    spec = default_registry.get("cozylobe")
    built = spec.build_spec()
    assert "mcp__alice__send_message" not in built.allowed_tools
    assert "mcp__alice__run_experiment" not in built.allowed_tools
    # Keeps the write surface needed for inner/notes + inner/surface.
    assert "Write" in built.allowed_tools
    assert "Read" in built.allowed_tools


def test_cozylobe_prompt_names_vault_paths() -> None:
    """The vault-read-only constraint must name the inner/notes +
    inner/surface paths verbatim so the model can route correctly."""
    spec = default_registry.get("cozylobe")
    built = spec.build_spec()
    prompt = built.append_system_prompt or ""
    assert "inner/notes/" in prompt
    assert "inner/surface/" in prompt
    assert "cortex-memory" in prompt


# ---------------------------------------------------------------------------
# Qwen client — graceful-degrade path
# ---------------------------------------------------------------------------


class _UnreachableClient:
    """httpx.AsyncClient stand-in whose POST always raises ConnectError."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        raise httpx.ConnectError("nope")


@pytest.mark.asyncio
async def test_qwen_client_raises_qwen_unreachable_on_connect_error() -> None:
    """When the endpoint can't be reached, ``classify`` surfaces a
    :class:`QwenUnreachable` so the wake loop catches it cleanly."""
    client = QwenClient(
        "http://nowhere:1",
        http_client_factory=_UnreachableClient,
    )
    event = _make_event()
    with pytest.raises(QwenUnreachable):
        await client.classify(event)


@dataclass
class _CannedResponse:
    body: dict

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.body


@dataclass
class _CannedClient:
    """httpx.AsyncClient stand-in returning a fixed JSON body."""

    body: dict = field(default_factory=dict)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        return _CannedResponse(self.body)


@pytest.mark.asyncio
async def test_qwen_client_parses_actions_list() -> None:
    canned_body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "actions": [
                                {
                                    "urgency": "HIGH",
                                    "intent": "investigate",
                                    "entity_ids": ["light.kitchen"],
                                    "summary": "Unexpected light cycle",
                                    "reasoning": "Out of pattern",
                                }
                            ]
                        }
                    )
                }
            }
        ]
    }
    client = QwenClient(
        "http://nowhere:1",
        http_client_factory=lambda: _CannedClient(body=canned_body),
    )
    classification = await client.classify(_make_event())
    assert classification.urgency == "HIGH"
    assert classification.intent == "investigate"
    assert classification.summary == "Unexpected light cycle"


@pytest.mark.asyncio
async def test_qwen_client_raises_on_missing_actions() -> None:
    """A response missing the 'actions' list is treated as upstream
    breakage — QwenUnreachable so the wake loop degrades gracefully."""
    canned_body = {
        "choices": [
            {"message": {"content": json.dumps({"not_actions": []})}}
        ]
    }
    client = QwenClient(
        "http://nowhere:1",
        http_client_factory=lambda: _CannedClient(body=canned_body),
    )
    with pytest.raises(QwenUnreachable):
        await client.classify(_make_event())
