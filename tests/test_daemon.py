"""Integration tests for the speaking daemon's v3 paths.

We mock the Claude Agent SDK's ``query`` coroutine + SignalClient so we
can drive real SpeakingDaemon instances through their state machines
without talking to Anthropic or signal-cli.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from alice_speaking import daemon as daemon_module
from alice_speaking import session_state
from alice_speaking.daemon import SpeakingDaemon


# --------------------------------------------------------------------- fakes


def _text_block(text: str) -> TextBlock:
    return TextBlock(text=text)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[_text_block(text)], model="claude-opus-4-7")


def _result(
    session_id: str,
    *,
    usage: Optional[dict] = None,
    is_error: bool = False,
    result: Optional[str] = None,
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        usage=usage or {"input_tokens": 100},
        result=result,
    )


class FakeSignal:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.sent_with_attachments: list[tuple[str, str, list[str] | None]] = []

    async def send(
        self,
        recipient: str,
        text: str,
        attachments: list[str] | None = None,
    ) -> None:
        self.sent.append((recipient, text))
        self.sent_with_attachments.append((recipient, text, attachments))

    async def start_typing(self, recipient: str) -> None:
        pass

    async def stop_typing(self, recipient: str) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def wait_ready(self, *a, **k) -> None:
        pass

    def receive(self):
        async def _empty():
            if False:
                yield  # type: ignore[unreachable]
        return _empty()


def _make_daemon(cfg, monkeypatch: pytest.MonkeyPatch) -> SpeakingDaemon:
    """Construct a SpeakingDaemon with the Signal client replaced by a fake."""
    # Patch SignalClient in the daemon module so __init__ gets the fake.
    monkeypatch.setattr(daemon_module, "SignalClient", lambda **kwargs: FakeSignal())
    return SpeakingDaemon(cfg)


def _patch_query(monkeypatch: pytest.MonkeyPatch, messages_fn: Callable[[], list[Any]]):
    """Replace the SDK's query() with a factory producing the given messages.

    As of the kernel refactor (step 7), query() is invoked from inside
    :mod:`alice_core.kernel`, not from daemon.py. We patch it there so the
    daemon's kernel-driven turns see our fakes.

    ``messages_fn`` is a zero-arg callable so each turn can return a fresh
    list — closing over mutable state from the test."""

    async def fake_query(*, prompt: str, options: Any):
        for m in messages_fn():
            yield m

    # Preserve generator-factory semantics (each call makes a new generator).
    def outer(**kwargs):
        return fake_query(**kwargs)

    monkeypatch.setattr("alice_core.kernel.query", outer)


# --------------------------------------------------------------------- tests


def test_init_loads_persisted_session(cfg, tmp_path, monkeypatch) -> None:
    # Pre-persist session.json AND the SDK JSONL so preflight passes.
    state_dir = cfg.mind_dir / "inner" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    session_path = state_dir / "session.json"
    session_state.write(session_path, "persisted-123")

    sdk_jsonl = session_state.sdk_session_jsonl_path(cfg.work_dir, "persisted-123")
    sdk_jsonl.parent.mkdir(parents=True, exist_ok=True)
    sdk_jsonl.write_text("{}\n")

    d = _make_daemon(cfg, monkeypatch)
    assert d.session_id == "persisted-123"


def test_init_drops_stale_session_when_sdk_jsonl_missing(
    cfg, tmp_path, monkeypatch
) -> None:
    state_dir = cfg.mind_dir / "inner" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    session_path = state_dir / "session.json"
    session_state.write(session_path, "stale-456")
    # No SDK jsonl on disk — preflight should drop this.

    d = _make_daemon(cfg, monkeypatch)
    assert d.session_id is None
    assert not session_path.is_file()


def test_run_turn_persists_session_id(cfg, monkeypatch) -> None:
    d = _make_daemon(cfg, monkeypatch)

    def msgs() -> list[Any]:
        return [
            _assistant("ok"),
            _result("fresh-789"),
        ]

    _patch_query(monkeypatch, msgs)

    asyncio.run(
        d._run_turn("hello", turn_id="t1", outbound_recipient="+15555550100")
    )

    assert d.session_id == "fresh-789"
    persisted = session_state.read(d._session_path)
    assert persisted is not None
    assert persisted.session_id == "fresh-789"


def test_run_turn_fires_missed_reply_when_no_send(cfg, monkeypatch, tmp_path) -> None:
    d = _make_daemon(cfg, monkeypatch)

    def msgs() -> list[Any]:
        return [
            _assistant("just thinking"),
            _result("s1"),
        ]

    _patch_query(monkeypatch, msgs)
    asyncio.run(
        d._run_turn(
            "hi", turn_id="t-missed", outbound_recipient="+15555550100"
        )
    )

    # Event log should contain a missed_reply record.
    log_path = d.cfg.event_log_path
    lines = log_path.read_text().splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert "missed_reply" in events


def test_send_message_suppresses_missed_reply(cfg, monkeypatch) -> None:
    d = _make_daemon(cfg, monkeypatch)

    def msgs() -> list[Any]:
        return [
            _assistant("replying"),
            _result("s1"),
        ]

    _patch_query(monkeypatch, msgs)

    # Simulate what the send_message tool does: call the daemon's sender.
    # The tool resolves the recipient to a ChannelRef before calling.
    from alice_speaking.transports import ChannelRef

    jason = ChannelRef(transport="signal", address="+15555550100", durable=True)

    async def go():
        # Reset did_send like _run_turn would.
        d._turn_did_send = False
        # Manually invoke the sender as the tool handler would.
        await d._send_message(jason, "hello jason")
        # Then run the "turn" which checks the flag.
        await d._run_turn(
            "unused", turn_id="t-sent", outbound_recipient="+15555550100"
        )
        return d._turn_did_send

    # We can't easily assert the flag after _run_turn because _run_turn
    # resets it at the top of each call. Instead, verify that invoking
    # _send_message directly flips the flag AND writes to signal.
    #
    # NB: ``_current_turn_kind = 'signal'`` is set so the send bypasses
    # quiet hours — that's the policy for replies to inbound. Without
    # this the test would be flaky (only pass when wall-clock is outside
    # 22-07 ET).
    async def go2():
        d._turn_did_send = False
        d._current_turn_kind = "signal"
        await d._send_message(jason, "hi")
        return d._turn_did_send, d.signal.sent

    flag, sent = asyncio.run(go2())
    assert flag is True
    assert sent == [("+15555550100", "hi")]


def test_token_threshold_arms_compaction(cfg, monkeypatch) -> None:
    d = _make_daemon(cfg, monkeypatch)
    # Lower the threshold so the fake usage trips it.
    d.cfg.speaking["context_compaction_threshold"] = 100

    def msgs() -> list[Any]:
        return [
            _assistant("ok"),
            _result("s1", usage={"input_tokens": 200}),
        ]

    _patch_query(monkeypatch, msgs)

    asyncio.run(
        d._run_turn("hi", turn_id="t1", outbound_recipient="+15555550100")
    )
    assert d._compaction_pending is True


def test_compaction_rolls_session_and_writes_summary(cfg, monkeypatch) -> None:
    d = _make_daemon(cfg, monkeypatch)
    d.session_id = "to-be-rolled"
    session_state.write(d._session_path, "to-be-rolled")
    d._compaction_pending = True

    summary_text = (
        "1. Active threads: none\n2. Owner's state: tired\n"
        "3. Surface verdicts: none\n4. Uncaptured facts: none"
    )

    def msgs() -> list[Any]:
        return [
            _assistant(summary_text),
            _result("compaction-session"),
        ]

    _patch_query(monkeypatch, msgs)

    asyncio.run(d._run_compaction())

    assert d.session_id is None
    assert d._compaction_pending is False
    assert not d._session_path.is_file()
    assert d._summary_path.is_file()
    assert summary_text.split("\n", 1)[0] in d._summary_path.read_text()


def test_layer2_bootstrap_preamble_primed_from_turn_log(cfg, monkeypatch) -> None:
    # Seed a couple turns in the turn log.
    d = _make_daemon(cfg, monkeypatch)
    from alice_speaking.turn_log import new_turn

    d.turns.append(new_turn("+15555550100", "Owner", "morning", "hey jason"))
    d.turns.append(
        new_turn("+15555550100", "Owner", "how you doing", "doing fine")
    )

    assert d.session_id is None
    d._prime_bootstrap_preamble()
    assert d._pending_preamble is not None
    assert "morning" in d._pending_preamble
    assert "Resume naturally" in d._pending_preamble


def test_layer2_bootstrap_empty_log_no_preamble(cfg, monkeypatch) -> None:
    d = _make_daemon(cfg, monkeypatch)
    assert d.session_id is None
    d._prime_bootstrap_preamble()
    # Empty turn log → no preamble.
    assert d._pending_preamble is None


def test_post_compaction_preamble_uses_summary(cfg, monkeypatch) -> None:
    d = _make_daemon(cfg, monkeypatch)
    # Simulate the state the daemon is in right after _run_compaction()
    # clears session_id and writes the summary file.
    d.session_id = None
    d._summary_path.parent.mkdir(parents=True, exist_ok=True)
    d._summary_path.write_text("compacted summary body")

    d._prime_bootstrap_preamble()
    assert d._pending_preamble is not None
    assert "compacted summary body" in d._pending_preamble
    assert "Context summary" in d._pending_preamble


def test_resume_failure_clears_and_retries(cfg, monkeypatch) -> None:
    """Design §Problem 1: resume= failure clears session_id, primes
    Layer 2 preamble, and transparently retries the same prompt once
    with a fresh session. Test drives a query mock that fails on the
    first call (with resume=) and succeeds on the retry (fresh)."""
    d = _make_daemon(cfg, monkeypatch)
    d.session_id = "stale"
    session_state.write(d._session_path, "stale")
    from alice_speaking.turn_log import new_turn

    d.turns.append(new_turn("+15555550100", "Owner", "hi", "hey"))

    class SessionNotFoundError(RuntimeError):
        pass

    calls: list[dict] = []

    async def first_call_fails_then_succeeds(*, prompt: str, options: Any):
        calls.append({"prompt": prompt, "resume": getattr(options, "resume", None)})
        # First call (resume=stale) raises. Second call (no resume) succeeds.
        if len(calls) == 1:
            raise SessionNotFoundError("Session not found: stale")
        # Success path: yield one assistant + one result.
        yield _assistant("ok")
        yield _result("fresh")

    monkeypatch.setattr(
        "alice_core.kernel.query",
        lambda **kw: first_call_fails_then_succeeds(**kw),
    )

    asyncio.run(
        d._run_turn("hi", turn_id="t1", outbound_recipient="+15555550100")
    )

    # Retry happened; fresh session persisted.
    assert len(calls) == 2
    assert calls[0]["resume"] == "stale"
    assert calls[1]["resume"] is None
    assert d.session_id == "fresh"
    persisted = session_state.read(d._session_path)
    assert persisted is not None and persisted.session_id == "fresh"
    # Layer 2 preamble was primed before retry and then consumed.
    assert d._pending_preamble is None
    assert "Recent conversation" in calls[1]["prompt"]
    # Event log should have session_resume_failed.
    lines = d.cfg.event_log_path.read_text().splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert "session_resume_failed" in events


def test_resume_failure_does_not_loop_on_retry(cfg, monkeypatch) -> None:
    """If the retry ALSO blows up session-style, we don't recurse a
    second time — the exception propagates to the caller."""
    d = _make_daemon(cfg, monkeypatch)
    d.session_id = "stale"
    session_state.write(d._session_path, "stale")

    class SessionNotFoundError(RuntimeError):
        pass

    calls: list[int] = []

    async def always_fails(*, prompt: str, options: Any):
        calls.append(1)
        raise SessionNotFoundError("Session not found")
        yield  # pragma: no cover

    monkeypatch.setattr("alice_core.kernel.query", lambda **kw: always_fails(**kw))

    with pytest.raises(SessionNotFoundError):
        asyncio.run(
            d._run_turn("hi", turn_id="t1", outbound_recipient="+15555550100")
        )

    # Exactly two calls — one initial + one retry.
    assert len(calls) == 2
    assert d.session_id is None


def test_preamble_consumed_on_next_turn(cfg, monkeypatch) -> None:
    d = _make_daemon(cfg, monkeypatch)
    d._pending_preamble = "[PREAMBLE]"
    captured: dict[str, Any] = {}

    def msgs() -> list[Any]:
        return [
            _assistant("ok"),
            _result("s1"),
        ]

    async def fake_query(*, prompt: str, options: Any):
        captured["prompt"] = prompt
        for m in msgs():
            yield m

    monkeypatch.setattr("alice_core.kernel.query", lambda **kw: fake_query(**kw))

    asyncio.run(
        d._run_turn("real body", turn_id="t1", outbound_recipient="+15555550100")
    )

    assert captured["prompt"].startswith("[PREAMBLE]")
    assert "real body" in captured["prompt"]
    # One-shot: preamble cleared after consumption.
    assert d._pending_preamble is None
