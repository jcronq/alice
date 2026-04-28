"""Narrative summarizer — feeds Alice's recent interactions to Claude
and streams back a human-readable narrative.

Uses the same OAuth path the hemispheres use (CLAUDE_CODE_OAUTH_TOKEN from
alice.env). The Agent SDK is invoked without tools — this is pure
summarization, not agentic work.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import time
from dataclasses import dataclass
from typing import AsyncIterator

from . import aggregators, bucket_cache, sources
from .settings import Paths


DEFAULT_MODEL = "claude-sonnet-4-6"
BUCKET_MODEL = "claude-haiku-4-5"  # one-shot bucket summaries — Haiku is plenty
DEFAULT_MAX_SECONDS = 90
CACHE_TTL_SECONDS = 300  # 5 min — refresh if window content changed recently


@dataclass
class NarrativeRequest:
    window_seconds: int
    max_events: int = 500


def load_oauth_token() -> str | None:
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if token:
        return token
    env_file = pathlib.Path(
        os.environ.get("ALICE_CONFIG")
        or pathlib.Path.home() / ".config" / "alice" / "alice.env"
    )
    if not env_file.is_file():
        return None
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def build_digest(paths: Paths, window_seconds: int, max_events: int) -> dict:
    """Pull raw events + artifacts, filter to window, return a compact digest."""
    now_ts = time.time()
    cutoff = now_ts - window_seconds
    all_events = sources.load_all(paths)

    in_window = [e for e in all_events if e.ts >= cutoff]
    wakes = [w for w in aggregators.group_wakes(all_events) if w.start_ts >= cutoff]
    turns = [t for t in aggregators.group_turns(all_events) if t.start_ts >= cutoff]

    surfaces = [e for e in in_window if e.kind in ("surface_pending", "surface_resolved")]
    emergencies = [e for e in in_window if e.kind in ("emergency_pending", "emergency_resolved")]
    notes = [e for e in in_window if e.kind in ("note_pending", "note_consumed")]
    thoughts = [e for e in in_window if e.kind == "thought_written"]

    # Tool histogram (tight activity fingerprint).
    tools: dict[str, int] = {}
    for e in in_window:
        if e.kind == "tool_use":
            name = e.detail.get("name") or "?"
            tools[name] = tools.get(name, 0) + 1

    return {
        "window_seconds": window_seconds,
        "generated_at": now_ts,
        "event_count": len(in_window),
        "wakes": [_wake_summary(w) for w in wakes[-30:]],
        "turns": [_turn_summary(t) for t in turns[-30:]],
        "surfaces": [_artifact_summary(e) for e in surfaces[-20:]],
        "emergencies": [_artifact_summary(e) for e in emergencies[-20:]],
        "notes": [_artifact_summary(e) for e in notes[-20:]],
        "thoughts": [_artifact_summary(e) for e in thoughts[-20:]],
        "tools": tools,
        "directive": sources.read_directive(paths.inner)[:1200],
    }


def _wake_summary(w) -> dict:
    return {
        "wake_id": w.wake_id,
        "start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(w.start_ts)),
        "status": w.status,
        "duration_s": (w.duration_ms or 0) / 1000.0,
        "tools": w.tools,
        "events": len(w.events),
        "cost_usd": w.total_cost_usd,
    }


def _turn_summary(t) -> dict:
    return {
        "turn_id": t.turn_id,
        "kind": t.kind,
        "start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.start_ts)),
        "sender": t.sender_name,
        "surface_id": t.surface_id,
        "emergency_id": t.emergency_id,
        "inbound": _trim(t.inbound or "", 280),
        "outbound": _trim(t.outbound or "", 280),
        "error": t.error,
        "duration_s": (t.duration_ms or 0) / 1000.0,
        "tools": t.tools,
    }


def _artifact_summary(e) -> dict:
    d = e.detail or {}
    return {
        "id": e.correlation_id,
        "kind": e.kind,
        "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.ts)),
        "body": _trim(d.get("body") or "", 400),
        "trailer": d.get("trailer"),
    }


def _trim(s: str, cap: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= cap else s[: cap - 1] + "…"


def render_prompt(digest: dict, window_label: str) -> str:
    """Build the prompt Claude will narrate over."""
    return f"""You are writing a short narrative summary of what Alice has been doing.

Alice has two hemispheres:
- **Speaking Alice** (Opus, the voice that talks to the owner and their contacts over Signal).
- **Thinking Alice** (Sonnet, a quieter background process that wakes on a timer to groom her own memory and knowledge base in `alice-mind/`).

They pass files between each other: *surfaces* (thinking → speaking: an insight sharp enough to voice), *notes* (speaking → thinking: fleeting thoughts filed for later grooming), *emergencies* (external monitors → speaking, bypassing quiet hours), *thoughts* (thinking's own wake journal).

Below is a machine-generated digest of what happened over the last {window_label}. Write a short narrative (aim for 250–400 words, markdown, no headers, no bullet walls) that:

1. Tells the story of what Alice did, in natural prose. Weave thinking wakes, signal turns, and cross-hemisphere artifacts into one thread — don't list them.
2. Calls out anything notable: errors, timeouts, emergencies voiced or downgraded, repeated themes, unusual tool patterns, gaps of silence, the latest direction of Alice's grooming.
3. Refers to Alice in third person. The owner and their contacts are the people she talks to. Be specific — use actual topic keywords you see in the digest rather than generic words like "conversation" or "tasks."
4. If almost nothing happened, say so plainly in one or two sentences.

Write only the narrative, nothing else. No preamble. No conclusion section. No bullet points unless the digest is genuinely a list of discrete items.

## Digest

```json
{json.dumps(digest, ensure_ascii=False, indent=2, default=str)}
```
"""


# ---------------------------------------------------------------------------
# Cache — avoids re-calling Claude on page refresh.


_CACHE: dict[str, tuple[float, str]] = {}


def cache_key(digest: dict) -> str:
    # Hash the digest content so any change invalidates the cache.
    blob = json.dumps(digest, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def cache_get(key: str) -> str | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    saved_at, text = entry
    if time.time() - saved_at > CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return text


def cache_put(key: str, text: str) -> None:
    _CACHE[key] = (time.time(), text)


# ---------------------------------------------------------------------------
# Streaming via Agent SDK


async def stream_narrative(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    max_seconds: int = DEFAULT_MAX_SECONDS,
) -> AsyncIterator[dict]:
    """Yield events: {"type": "chunk", "text": "..."} and finally {"type": "done"}
    or {"type": "error", "message": "..."}."""
    token = load_oauth_token()
    if not token:
        yield {"type": "error", "message": "CLAUDE_CODE_OAUTH_TOKEN missing from env and alice.env"}
        return
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token

    try:
        # Import lazily so startup doesn't require the SDK/claude CLI to exist.
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError as exc:
        yield {"type": "error", "message": f"claude-agent-sdk not installed: {exc}"}
        return

    options = ClaudeAgentOptions(
        model=model,
        allowed_tools=[],
        cwd="/tmp",
    )
    try:
        async with asyncio.timeout(max_seconds):
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield {"type": "chunk", "text": block.text}
                    if msg.error:
                        yield {"type": "error", "message": str(msg.error)}
                        return
                elif isinstance(msg, ResultMessage):
                    yield {
                        "type": "result",
                        "duration_ms": msg.duration_ms,
                        "cost_usd": msg.total_cost_usd,
                        "session_id": msg.session_id,
                    }
                    if msg.is_error:
                        yield {"type": "error", "message": msg.result or "claude returned is_error"}
                        return
    except asyncio.TimeoutError:
        yield {"type": "error", "message": f"timed out after {max_seconds}s"}
        return
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
        return

    yield {"type": "done"}


WINDOW_PRESETS = {
    "1h": (3600, "hour"),
    "6h": (6 * 3600, "6 hours"),
    "24h": (24 * 3600, "24 hours"),
    "7d": (7 * 86400, "week"),
    "30d": (30 * 86400, "month"),
}


def window_from_label(label: str) -> tuple[int, str]:
    return WINDOW_PRESETS.get(label, WINDOW_PRESETS["24h"])


# ---------------------------------------------------------------------------
# Bucketed cache — each bucket is summarized once, cached to disk for 7 days,
# merged on demand. Overlapping windows (e.g. "last 24h" vs. "last 25h") share
# 95%+ of their buckets so subsequent queries are near-instant.

# bucket_seconds chosen so any window has ~6-30 buckets (a tractable merge input).
WINDOW_BUCKET_SECONDS = {
    "1h":   600,          # 10-min buckets → 6
    "6h":   1800,         # 30-min buckets → 12
    "24h":  3600,         # 1-hour buckets → 24
    "7d":   6 * 3600,     # 6-hour buckets → 28
    "30d":  86400,        # 1-day buckets → 30
}

MAX_CONCURRENT_BUCKET_GENERATIONS = 4


def bucket_seconds_for(window_label: str) -> int:
    return WINDOW_BUCKET_SECONDS.get(window_label, WINDOW_BUCKET_SECONDS["24h"])


def align_down(ts: float, step: int) -> int:
    return (int(ts) // step) * step


@dataclass
class BucketSlot:
    start: int
    end: int
    events: list  # list of UnifiedEvent
    content_hash: str

    def is_open(self, now_ts: float) -> bool:
        # A bucket is "open" if now is inside its range — its contents may still
        # grow, so cache lifetime should be treated as volatile for this one.
        return self.start <= now_ts < self.end


def build_buckets(paths: Paths, window_seconds: int, window_label: str,
                  now_ts: float | None = None) -> list[BucketSlot]:
    now_ts = now_ts or time.time()
    bucket_seconds = bucket_seconds_for(window_label)
    end = align_down(now_ts, bucket_seconds) + bucket_seconds   # include the current open bucket
    start = align_down(now_ts - window_seconds, bucket_seconds)

    all_events = sources.load_all(paths)
    events_in_window = [e for e in all_events if start <= e.ts < end]

    # Partition by bucket index.
    slots: dict[int, list] = {}
    bstart = start
    while bstart < end:
        slots[bstart] = []
        bstart += bucket_seconds
    for ev in events_in_window:
        idx = align_down(ev.ts, bucket_seconds)
        if idx in slots:
            slots[idx].append(ev)

    out: list[BucketSlot] = []
    for bs, evs in sorted(slots.items()):
        out.append(
            BucketSlot(
                start=bs,
                end=bs + bucket_seconds,
                events=evs,
                content_hash=_hash_events(evs),
            )
        )
    return out


def _hash_events(evs: list) -> str:
    """Stable hash over (ts, kind, summary) of events in the bucket."""
    h = hashlib.sha256()
    for e in evs:
        h.update(f"{e.ts:.3f}|{e.kind}|{e.summary}".encode("utf-8", errors="replace"))
    return h.hexdigest()[:16]


def _bucket_digest(slot: BucketSlot) -> str:
    """Compact human-readable events list for the per-bucket LLM prompt."""
    if not slot.events:
        return "(no events)"
    lines = []
    for ev in slot.events[:200]:  # defensive cap
        ts = time.strftime("%H:%M:%S", time.localtime(ev.ts))
        lines.append(f"[{ts}] {ev.hemisphere}/{ev.kind}: {ev.summary}")
    return "\n".join(lines)


def _bucket_prompt(slot: BucketSlot) -> str:
    start = time.strftime("%Y-%m-%d %H:%M", time.localtime(slot.start))
    end = time.strftime("%H:%M", time.localtime(slot.end))
    return f"""Summarize what happened in Alice's systems during this short time window. Write 1-3 sentences. Be specific: name tool calls, topics, people, surfaces, emergencies. No preamble, no bullet points, no header.

Window: {start} → {end}

Events:
{_bucket_digest(slot)}
"""


async def _summarize_bucket(slot: BucketSlot) -> bucket_cache.BucketSummary:
    """Call Claude for one bucket. Returns a BucketSummary ready to cache."""
    if not slot.events:
        return bucket_cache.BucketSummary(
            bucket_start=slot.start,
            bucket_seconds=slot.end - slot.start,
            content_hash=slot.content_hash,
            event_count=0,
            summary="",  # empty → merge step skips
            cost_usd=0.0,
            duration_ms=0,
            generated_at=time.time(),
        )
    prompt = _bucket_prompt(slot)
    started = time.time()
    text, cost = await _run_once(prompt, max_output_tokens_hint=300)
    return bucket_cache.BucketSummary(
        bucket_start=slot.start,
        bucket_seconds=slot.end - slot.start,
        content_hash=slot.content_hash,
        event_count=len(slot.events),
        summary=text.strip(),
        cost_usd=cost,
        duration_ms=int((time.time() - started) * 1000),
        generated_at=time.time(),
    )


async def _run_once(prompt: str, *, max_output_tokens_hint: int = 500) -> tuple[str, float]:
    """Non-streaming LLM call — used for per-bucket summaries."""
    token = load_oauth_token()
    if not token:
        raise RuntimeError("CLAUDE_CODE_OAUTH_TOKEN missing")
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )
    options = ClaudeAgentOptions(
        model=BUCKET_MODEL,
        allowed_tools=[],
        cwd="/tmp",
    )
    parts: list[str] = []
    cost = 0.0
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
        elif isinstance(msg, ResultMessage):
            cost = float(msg.total_cost_usd or 0)
            if msg.is_error:
                raise RuntimeError(msg.result or "claude is_error")
    return "".join(parts), cost


async def ensure_bucket_cache(
    slots: list[BucketSlot],
    *,
    progress_cb=None,
) -> list[bucket_cache.BucketSummary]:
    """For each slot, return a cached or freshly-generated BucketSummary.

    Open (in-progress) buckets always regenerate so the "now" edge stays live.
    All others prefer the on-disk cache.
    """
    now_ts = time.time()
    results: list[bucket_cache.BucketSummary | None] = [None] * len(slots)
    to_generate: list[tuple[int, BucketSlot]] = []

    for idx, slot in enumerate(slots):
        force = slot.is_open(now_ts)
        cached = None if force else bucket_cache.read(
            bucket_seconds=slot.end - slot.start,
            bucket_start=slot.start,
            content_hash=slot.content_hash,
        )
        if cached is not None:
            results[idx] = cached
        else:
            to_generate.append((idx, slot))

    if progress_cb:
        await progress_cb({"cached": sum(1 for r in results if r is not None),
                           "total": len(slots), "pending": len(to_generate)})

    sem = asyncio.Semaphore(MAX_CONCURRENT_BUCKET_GENERATIONS)

    async def _one(idx: int, slot: BucketSlot):
        async with sem:
            summary = await _summarize_bucket(slot)
            # Only persist closed buckets — open buckets will change as events accumulate.
            if not slot.is_open(now_ts):
                try:
                    bucket_cache.write(summary)
                except OSError:
                    pass
            results[idx] = summary
            if progress_cb:
                done = sum(1 for r in results if r is not None)
                await progress_cb({"cached": done, "total": len(slots), "pending": len(slots) - done})

    await asyncio.gather(*(_one(i, s) for i, s in to_generate))
    return [r for r in results if r is not None]


def render_merge_prompt(
    summaries: list[bucket_cache.BucketSummary], window_label: str
) -> str:
    lines = []
    for s in summaries:
        if not s.summary:
            continue
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.bucket_start))
        lines.append(f"[{ts}] ({s.event_count} events) {s.summary}")
    body = "\n".join(lines) if lines else "(no activity in window)"
    return f"""Weave the following per-time-period summaries of Alice's activity into one narrative (~250-400 words, markdown, no headers, no bullet walls). Keep proper nouns and specifics. Third person. If nothing happened, say so in one or two sentences.

Alice has two hemispheres: Speaking (Opus, voices Signal to the owner and their contacts) and Thinking (Sonnet, background grooming of alice-mind). They pass artifacts between each other — surfaces, notes, emergencies, thoughts.

Window: last {window_label}

Per-period summaries (chronological):

{body}

Write only the narrative, nothing else.
"""
