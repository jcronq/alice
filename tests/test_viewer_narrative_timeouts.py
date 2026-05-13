"""Tests for the timeout-hardening in :mod:`alice_viewer.narrative` (issue #135).

Two guarantees are exercised:

- ``_summarize_bucket`` enforces ``BUCKET_MAX_SECONDS`` — a slow LLM
  call raises ``TimeoutError`` instead of stalling the whole fill.
- ``ensure_bucket_cache`` survives one bucket's failure: the offending
  bucket lands an empty placeholder (merge skips it), the rest of the
  fill keeps going, and the ``failed`` progress counter is surfaced.
- ``stream_narrative`` honors its wall-clock cap — a hanging SDK
  yields the SSE ``error`` event the front-end reads instead of the
  connection silently dying.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from alice_viewer import narrative as nmod


# ---------------------------------------------------------------------------
# Helpers


@dataclass
class _Slot:
    """Stand-in for :class:`narrative.BucketSlot` — only the fields the
    timeout code paths read are required, so we don't pull in the real
    event loader."""

    start: int
    end: int
    events: list
    content_hash: str

    def is_open(self, now_ts: float) -> bool:
        return self.start <= now_ts < self.end


def _slot(start: int, *, n_events: int = 3, hash_: str = "h") -> _Slot:
    # Use a synthetic "event" the real summarize path never touches —
    # we monkey-patch _run_once before any code reads ev fields.
    evs = [object()] * n_events
    return _Slot(start=start, end=start + 3600, events=evs, content_hash=hash_)


class _NoAuth:
    """``_ensure_auth`` stand-in: pretend creds are present so
    ``_run_once`` reaches the SDK call. The SDK itself is monkey-
    patched below, so no real network traffic is generated."""

    mode = "subscription"


# ---------------------------------------------------------------------------
# Per-bucket timeout


@pytest.mark.asyncio
async def test_summarize_bucket_times_out_on_slow_llm(monkeypatch):
    """A ``_run_once`` call that exceeds ``BUCKET_MAX_SECONDS`` raises
    ``TimeoutError`` — the per-bucket cap actually fires."""

    monkeypatch.setattr(nmod, "BUCKET_MAX_SECONDS", 0.05)

    async def slow_run_once(prompt, *, max_output_tokens_hint=500):
        await asyncio.sleep(1.0)
        return "should never be reached", 0.0

    monkeypatch.setattr(nmod, "_run_once", slow_run_once)
    monkeypatch.setattr(nmod, "_bucket_prompt", lambda slot: "stub-prompt")

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await nmod._summarize_bucket(_slot(1000))


@pytest.mark.asyncio
async def test_summarize_bucket_returns_summary_on_success(monkeypatch):
    """Sanity check: a quick ``_run_once`` produces a populated
    :class:`BucketSummary`, so the timeout wrapper isn't accidentally
    truncating the happy path."""

    async def fast_run_once(prompt, *, max_output_tokens_hint=500):
        return "  a tidy summary  ", 0.0042

    monkeypatch.setattr(nmod, "_run_once", fast_run_once)
    monkeypatch.setattr(nmod, "_bucket_prompt", lambda slot: "stub-prompt")

    summary = await nmod._summarize_bucket(_slot(2000, n_events=5))
    assert summary.summary == "a tidy summary"
    assert summary.event_count == 5
    assert summary.cost_usd == pytest.approx(0.0042)


# ---------------------------------------------------------------------------
# ensure_bucket_cache resilience


@pytest.mark.asyncio
async def test_ensure_bucket_cache_isolates_one_slow_bucket(monkeypatch, tmp_path):
    """One bucket's timeout must not stall the rest of the fill.

    Slot 0 sleeps past the (tiny) per-bucket cap → placeholder with
    empty summary. Slot 1 returns quickly → populated summary. The
    progress callback observes the ``failed`` counter incrementing.
    """

    # Disk writes for closed buckets go through bucket_cache.write —
    # redirect to a tmpdir so the test doesn't touch the operator's
    # real cache. (Open buckets aren't persisted, but the success-path
    # bucket below is closed relative to a far-future now_ts.)
    monkeypatch.setenv("ALICE_VIEWER_CACHE_DIR", str(tmp_path))

    monkeypatch.setattr(nmod, "BUCKET_MAX_SECONDS", 0.05)
    monkeypatch.setattr(nmod, "_bucket_prompt", lambda slot: "stub-prompt")

    async def selective_run_once(prompt, *, max_output_tokens_hint=500):
        # Distinguish the two slots by inspecting the call counter —
        # first call is "slow", second is "fast". This avoids leaning
        # on which slot the gather() schedules first because the
        # semaphore is 4 (both run concurrently).
        nonlocal call_idx
        idx = call_idx
        call_idx += 1
        if idx == 0:
            await asyncio.sleep(1.0)
            return "never", 0.0
        return "bucket-1 summary", 0.01

    call_idx = 0
    monkeypatch.setattr(nmod, "_run_once", selective_run_once)

    progress_events: list[dict[str, Any]] = []

    async def progress_cb(info):
        progress_events.append(dict(info))

    slots = [_slot(1000, hash_="h0"), _slot(5000, hash_="h1")]
    results = await nmod.ensure_bucket_cache(slots, progress_cb=progress_cb)

    assert len(results) == 2
    # One success, one timeout placeholder (empty summary).
    summaries = sorted(r.summary for r in results)
    assert summaries == ["", "bucket-1 summary"]

    # The final progress event should report 2/2 ready with 1 failed.
    final = progress_events[-1]
    assert final["cached"] == 2
    assert final["total"] == 2
    assert final["pending"] == 0
    assert final["failed"] == 1


@pytest.mark.asyncio
async def test_ensure_bucket_cache_all_success(monkeypatch, tmp_path):
    """All-success path still reports ``failed=0`` in progress —
    guards against the new field accidentally defaulting to truthy."""

    monkeypatch.setenv("ALICE_VIEWER_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(nmod, "_bucket_prompt", lambda slot: "stub-prompt")

    async def fast(prompt, *, max_output_tokens_hint=500):
        return "ok", 0.0

    monkeypatch.setattr(nmod, "_run_once", fast)

    progress_events: list[dict[str, Any]] = []

    async def cb(info):
        progress_events.append(dict(info))

    results = await nmod.ensure_bucket_cache(
        [_slot(1000, hash_="a"), _slot(5000, hash_="b")], progress_cb=cb
    )
    assert all(r.summary == "ok" for r in results)
    assert progress_events[-1]["failed"] == 0


# ---------------------------------------------------------------------------
# stream_narrative wall-clock timeout


@pytest.mark.asyncio
async def test_stream_narrative_emits_error_on_wall_clock_timeout(monkeypatch):
    """When the SDK iterator hangs past ``DEFAULT_MAX_SECONDS``,
    ``stream_narrative`` must emit a structured ``error`` event so the
    SSE handler in the viewer can surface it (vs. the connection just
    closing on the client)."""

    import claude_agent_sdk as sdk

    async def hanging_query(*, prompt, options):
        # Never yield — simulate a stalled provider stream.
        await asyncio.sleep(10.0)
        if False:  # pragma: no cover — keeps this a generator function
            yield None

    monkeypatch.setattr(sdk, "query", hanging_query)
    monkeypatch.setattr(nmod, "_ensure_auth", lambda: _NoAuth())

    events = []
    async for ev in nmod.stream_narrative("prompt", max_seconds=0.1):
        events.append(ev)

    assert any(e.get("type") == "error" for e in events), events
    err = next(e for e in events if e.get("type") == "error")
    # The TimeoutError branch surfaces a message that mentions the cap;
    # callers downstream just propagate ``message`` verbatim to SSE.
    assert "timed out" in err["message"].lower()


@pytest.mark.asyncio
async def test_stream_narrative_emits_error_when_no_credentials(monkeypatch):
    """Sanity check the auth-missing branch still emits ``error`` —
    the SSE handler treats it the same as the timeout."""

    class _Missing:
        mode = "none"

    monkeypatch.setattr(nmod, "_ensure_auth", lambda: _Missing())

    events = [ev async for ev in nmod.stream_narrative("prompt", max_seconds=5)]
    assert events and events[0].get("type") == "error"
    assert "credential" in events[0]["message"].lower()
