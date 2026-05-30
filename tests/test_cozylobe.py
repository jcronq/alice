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
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
import pytest

from alice_cozylobe import (
    ActivityFetcher,
    ActivitySnapshot,
    CozyHemEvent,
    LLMClient,
    LLMUnreachable,
    QwenClassification,
    SSEConsumer,
    WakeLoop,
    write_observation_note,
    write_urgent_surface,
)
from alice_cozylobe.activity_fetcher import base_url_from_events_url
from alice_cozylobe.daemon import CozylobeDaemon
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
    raises :class:`LLMUnreachable` when configured to. Lets tests
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
            raise LLMUnreachable("simulated outage")
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
        llm_client=qwen,
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
    """When qwen raises LLMUnreachable, the wake loop logs once,
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
        llm_client=qwen,
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

    # Both events dispatched (the loop did NOT crash on LLMUnreachable).
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
        llm_client=qwen,
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
async def test_llm_client_raises_llm_unreachable_on_connect_error() -> None:
    """When the endpoint can't be reached, ``classify`` surfaces a
    :class:`LLMUnreachable` so the wake loop catches it cleanly."""
    client = LLMClient(
        "http://nowhere:1",
        http_client_factory=_UnreachableClient,
    )
    event = _make_event()
    with pytest.raises(LLMUnreachable):
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
async def test_llm_client_parses_actions_list() -> None:
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
    client = LLMClient(
        "http://nowhere:1",
        http_client_factory=lambda: _CannedClient(body=canned_body),
    )
    classification = await client.classify(_make_event())
    assert classification.urgency == "HIGH"
    assert classification.intent == "investigate"
    assert classification.summary == "Unexpected light cycle"


@pytest.mark.asyncio
async def test_llm_client_raises_on_missing_actions() -> None:
    """A response missing the 'actions' list is treated as upstream
    breakage — LLMUnreachable so the wake loop degrades gracefully."""
    canned_body = {
        "choices": [
            {"message": {"content": json.dumps({"not_actions": []})}}
        ]
    }
    client = LLMClient(
        "http://nowhere:1",
        http_client_factory=lambda: _CannedClient(body=canned_body),
    )
    with pytest.raises(LLMUnreachable):
        await client.classify(_make_event())


# ---------------------------------------------------------------------------
# Activity fetcher — periodic-mode HTTP client.
# ---------------------------------------------------------------------------


@dataclass
class _CannedGetResponse:
    """httpx.Response stand-in for ActivityFetcher GET calls."""

    body: Any
    status: int = 200

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status),
            )

    def json(self) -> Any:
        return self.body


class _CannedGetClient:
    """httpx.AsyncClient stand-in for ActivityFetcher.

    ``responses`` maps URL path suffix → either a body to return or an
    Exception to raise. Captures every URL called for assertion.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.urls_called: list[str] = []

    async def __aenter__(self) -> "_CannedGetClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, url: str, **kwargs: Any) -> _CannedGetResponse:
        self.urls_called.append(url)
        for suffix, value in self._responses.items():
            if url.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                return _CannedGetResponse(body=value)
        raise httpx.ConnectError(f"no canned response for {url}")


def test_base_url_from_events_url_strips_path() -> None:
    assert (
        base_url_from_events_url("http://aimax1:8000/api/v1/events")
        == "http://aimax1:8000"
    )
    assert (
        base_url_from_events_url("https://example.com/api/v1/events")
        == "https://example.com"
    )


@pytest.mark.asyncio
async def test_activity_fetcher_returns_snapshot_on_happy_path() -> None:
    """All three endpoints succeed → ActivitySnapshot with populated
    fields and no partial errors."""
    client = _CannedGetClient(
        {
            "/api/v1/entities/states": {"light.kitchen": "on"},
            "/api/v1/anthem/status": {"connected": True, "power": "off"},
            "/api/v1/lights/": [{"name": "kitchen", "on": True}],
        }
    )
    fetcher = ActivityFetcher(
        "http://example:8000",
        http_client_factory=lambda: client,
        clock=lambda: 42.0,
    )
    snap = await fetcher.fetch()
    assert snap is not None
    assert snap.entity_states == {"light.kitchen": "on"}
    assert snap.anthem_status == {"connected": True, "power": "off"}
    assert snap.lights == [{"name": "kitchen", "on": True}]
    assert snap.fetched_at == 42.0
    assert snap.partial_errors == []
    # The fetcher exercised exactly the documented endpoints.
    assert any("/api/v1/entities/states" in u for u in client.urls_called)
    assert any("/api/v1/anthem/status" in u for u in client.urls_called)
    assert any("/api/v1/lights/" in u for u in client.urls_called)


@pytest.mark.asyncio
async def test_activity_fetcher_returns_none_and_logs_once_on_connect_error(
    caplog,
) -> None:
    """Every endpoint raising ConnectError → fetch() returns None and
    a single WARNING fires on the first outage, not on subsequent
    fetches inside the same outage."""

    def factory():
        return _CannedGetClient(
            {
                "/api/v1/entities/states": httpx.ConnectError("nope"),
                "/api/v1/anthem/status": httpx.ConnectError("nope"),
                "/api/v1/lights/": httpx.ConnectError("nope"),
            }
        )

    fetcher = ActivityFetcher(
        "http://nowhere:1",
        http_client_factory=factory,
    )

    with caplog.at_level("WARNING"):
        first = await fetcher.fetch()
        second = await fetcher.fetch()

    assert first is None
    assert second is None
    warnings = [
        rec
        for rec in caplog.records
        if rec.levelname == "WARNING"
        and "cozyhem-engine unreachable" in rec.message
    ]
    assert len(warnings) == 1, (
        f"expected one warning per outage, got {len(warnings)}"
    )


@pytest.mark.asyncio
async def test_activity_fetcher_returns_partial_snapshot_on_mixed_failure() -> None:
    """One endpoint fails, two succeed → partial snapshot with the
    error recorded but the snapshot itself non-None so periodic
    reasoning can still proceed."""
    client = _CannedGetClient(
        {
            "/api/v1/entities/states": {"light.kitchen": "on"},
            "/api/v1/anthem/status": httpx.ConnectError("anthem down"),
            "/api/v1/lights/": [{"name": "kitchen", "on": True}],
        }
    )
    fetcher = ActivityFetcher(
        "http://example:8000",
        http_client_factory=lambda: client,
    )
    snap = await fetcher.fetch()
    assert snap is not None
    assert snap.entity_states == {"light.kitchen": "on"}
    assert snap.anthem_status is None
    assert snap.lights == [{"name": "kitchen", "on": True}]
    assert any("anthem_status" in err for err in snap.partial_errors)


# ---------------------------------------------------------------------------
# Periodic wake mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_wake_ticks_at_configured_cadence() -> None:
    """Set a small cadence + capture how many ticks fire in a short
    window. The first tick fires immediately (cadence after work), then
    each subsequent tick after one cadence period. Asserts at least
    two ticks landed inside the window."""
    snapshot = ActivitySnapshot(
        entity_states={"light.kitchen": "on"},
        anthem_status={"connected": True},
        lights=[],
        fetched_at=time.time(),
    )
    fetch_calls: list[float] = []

    async def fake_fetch() -> Optional[ActivitySnapshot]:
        fetch_calls.append(time.time())
        return snapshot

    emitter = CapturingEmitter()
    stub_run = _StubRunAgent()
    loop = WakeLoop(
        emitter=emitter,
        llm_client=None,
        run_agent_fn=stub_run,
        fetch_activity=fake_fetch,
        periodic_cadence_s=0.05,
    )

    stop = asyncio.Event()
    task = asyncio.create_task(loop.run_periodic(stop))
    try:
        # Window large enough for at least two ticks at a 0.05s cadence.
        for _ in range(200):
            if len(stub_run.calls) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    assert len(stub_run.calls) >= 2
    # Each call goes through the cozylobe AgentSpec with a
    # periodic_review correlation prefix.
    for call in stub_run.calls:
        assert call["agent_name"] == "cozylobe"
        assert call["correlation_id"].startswith("cozylobe-periodic_review-")
        assert "snapshot of the home's recent activity" in call["prompt"]
        assert "light.kitchen" in call["prompt"]


@pytest.mark.asyncio
async def test_periodic_wake_skips_tick_when_snapshot_is_none() -> None:
    """fetch_activity returning None (cozyhem unreachable) must NOT
    trigger run_agent. The tick is silently skipped + telemetry
    records the skipped reason."""

    async def unreachable_fetch() -> Optional[ActivitySnapshot]:
        return None

    emitter = CapturingEmitter()
    stub_run = _StubRunAgent()
    loop = WakeLoop(
        emitter=emitter,
        llm_client=None,
        run_agent_fn=stub_run,
        fetch_activity=unreachable_fetch,
        periodic_cadence_s=0.02,
    )

    stop = asyncio.Event()
    task = asyncio.create_task(loop.run_periodic(stop))
    try:
        for _ in range(100):
            if emitter.of_kind("cozylobe_periodic_skipped_unreachable"):
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    assert stub_run.calls == []
    assert emitter.of_kind("cozylobe_periodic_skipped_unreachable")


@pytest.mark.asyncio
async def test_periodic_wake_dispatches_periodic_review_event() -> None:
    """One tick → run_agent gets a periodic_review-flavored prompt and
    correlation_id. Distinct from the SSE-event prompt shape so
    downstream tracing can tell them apart."""
    snap = ActivitySnapshot(
        entity_states={"sensor.front_door": "closed"},
        anthem_status={"connected": False},
        lights=[{"name": "living_room", "on": False}],
        fetched_at=1716552000.0,
    )

    async def fake_fetch() -> Optional[ActivitySnapshot]:
        return snap

    emitter = CapturingEmitter()
    stub_run = _StubRunAgent()
    loop = WakeLoop(
        emitter=emitter,
        llm_client=None,
        run_agent_fn=stub_run,
        fetch_activity=fake_fetch,
        periodic_cadence_s=10.0,  # plenty of time for exactly one tick
    )

    stop = asyncio.Event()
    task = asyncio.create_task(loop.run_periodic(stop))
    try:
        for _ in range(100):
            if stub_run.calls:
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    assert len(stub_run.calls) == 1
    call = stub_run.calls[0]
    assert call["agent_name"] == "cozylobe"
    assert call["correlation_id"] == "cozylobe-periodic_review-1716552000"
    # Periodic prompt frames the task as a snapshot review, NOT an
    # event triage.
    assert "snapshot of the home's recent activity" in call["prompt"]
    # The snapshot fields are inlined so the agent can reason about
    # actual state.
    assert "sensor.front_door" in call["prompt"]
    # Telemetry: periodic tick + handled both fire.
    assert emitter.of_kind("cozylobe_periodic_tick")
    assert emitter.of_kind("cozylobe_periodic_handled")


@pytest.mark.asyncio
async def test_periodic_task_cancels_cleanly_on_stop() -> None:
    """Setting the stop event mid-cadence cancels the periodic task
    cleanly. No exceptions leak out; the task exits quickly without
    waiting the full cadence."""

    async def fake_fetch() -> Optional[ActivitySnapshot]:
        return ActivitySnapshot(fetched_at=time.time())

    emitter = CapturingEmitter()
    stub_run = _StubRunAgent()
    loop = WakeLoop(
        emitter=emitter,
        llm_client=None,
        run_agent_fn=stub_run,
        fetch_activity=fake_fetch,
        periodic_cadence_s=60.0,  # long cadence; stop must short-circuit it
    )

    stop = asyncio.Event()
    task = asyncio.create_task(loop.run_periodic(stop))
    # Let the first tick run so we're in the cadence-sleep window.
    for _ in range(100):
        if stub_run.calls:
            break
        await asyncio.sleep(0.01)
    assert stub_run.calls, "first tick should have fired before stop"

    # Now trip stop and verify the task exits in well under the
    # 60s cadence.
    start = time.time()
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    elapsed = time.time() - start
    assert elapsed < 1.0, (
        f"periodic task took {elapsed:.2f}s to exit after stop; "
        "interruptible sleep is broken"
    )
    # Task ended cleanly — neither cancelled nor errored.
    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None


@pytest.mark.asyncio
async def test_periodic_task_disabled_when_no_fetcher() -> None:
    """WakeLoop without a fetch_activity callable should no-op the
    periodic path — run_periodic returns immediately so the daemon
    can supervise it without crashing."""
    emitter = CapturingEmitter()
    loop = WakeLoop(emitter=emitter, llm_client=None)
    stop = asyncio.Event()
    # Must return promptly even though stop is never set.
    await asyncio.wait_for(loop.run_periodic(stop), timeout=1.0)


# ---------------------------------------------------------------------------
# Daemon — periodic task supervision.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_supervises_periodic_task_alongside_sse(monkeypatch) -> None:
    """The daemon's run() loop should create three tasks (sse, wake,
    periodic). Smoke-test by patching the constructors so each task
    immediately exits and asserting all three names appeared."""
    task_names: list[str] = []
    real_create = asyncio.create_task

    def tracking_create(coro, *, name=None):
        if name:
            task_names.append(name)
        return real_create(coro, name=name)

    monkeypatch.setattr(asyncio, "create_task", tracking_create)

    # Stub out the three runner methods so daemon.run exits quickly.
    async def fast_sse_run(self, queue, stop):
        return

    async def fast_wake_run(self, queue, stop):
        return

    async def fast_periodic_run(self, stop):
        return

    monkeypatch.setattr(
        "alice_cozylobe.daemon.SSEConsumer.run", fast_sse_run
    )
    monkeypatch.setattr(WakeLoop, "run", fast_wake_run)
    monkeypatch.setattr(WakeLoop, "run_periodic", fast_periodic_run)

    daemon = CozylobeDaemon(
        log_path=pathlib.Path("/dev/null"),
        qwen_endpoint=None,
    )
    rc = await daemon.run()
    assert rc == 0
    assert "cozylobe-sse" in task_names
    assert "cozylobe-wake" in task_names
    assert "cozylobe-periodic" in task_names
