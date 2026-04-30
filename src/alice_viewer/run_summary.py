"""Haiku-generated single-sentence summaries for completed thinking wakes.

The timeline shows one row per run. Wake rows used to label themselves
with the first non-empty thinking/text block — raw reasoning that often
read mid-sentence and didn't capture what the wake actually did. This
module replaces that with a one-shot Haiku summary call per wake.

- Cache: ``$ALICE_VIEWER_CACHE_DIR/run-summaries/<run_id>.json`` (default
  ``~/.local/state/alice/viewer-cache/run-summaries``).
- Generation: ``schedule(run_id, events)`` fires a fire-and-forget
  background task on the running event loop. The first render after a
  wake ends shows the fallback summary; subsequent renders pick up the
  cached Haiku summary.
- Cache survives indefinitely — wake events are immutable once ended,
  so the summary stays valid forever. Delete the file by hand to
  re-summarize.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from typing import Any


HAIKU_MODEL = "claude-haiku-4-5"

# Bump when the prompt or output shape changes — cached entries with a
# lower schema are treated as missing so summaries regenerate lazily.
CACHE_SCHEMA = 2


def cache_dir() -> pathlib.Path:
    override = os.environ.get("ALICE_VIEWER_CACHE_DIR")
    base = pathlib.Path(override) if override else (
        pathlib.Path.home() / ".local/state/alice/viewer-cache"
    )
    return base / "run-summaries"


def _cache_path(run_id: str) -> pathlib.Path:
    # Sanitize: run ids are short hex strings or "wake-<int>" — safe by
    # construction, but be paranoid about path traversal.
    safe = run_id.replace("/", "_").replace("..", "__")
    return cache_dir() / f"{safe}.json"


def read(run_id: str) -> str | None:
    """Return the cached summary for ``run_id``, or None.

    Returns None for entries written under an older CACHE_SCHEMA so that
    a prompt/format change naturally re-summarizes wakes on next view.
    """
    path = _cache_path(run_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("schema", 1) < CACHE_SCHEMA:
        return None
    summary = data.get("summary")
    return summary if isinstance(summary, str) and summary else None


def write(run_id: str, summary: str, *, cost_usd: float | None = None) -> None:
    path = _cache_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "summary": summary,
        "generated_at": time.time(),
        "model": HAIKU_MODEL,
        "schema": CACHE_SCHEMA,
    }
    if cost_usd is not None:
        payload["cost_usd"] = cost_usd
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    tmp.replace(path)


# In-flight set keeps async tasks alive until they finish. asyncio's
# create_task() can be GC'd if no strong reference is kept; we hold one
# here and discard on completion.
_inflight: set[asyncio.Task] = set()


def schedule(run_id: str, events: list) -> None:
    """Fire-and-forget summary generation for ``run_id``.

    Idempotent: if a summary is cached or a generation is already
    in-flight for this id, this is a no-op. Safe to call from a sync
    context that lives inside a FastAPI request — uses the running event
    loop. If no loop is running (e.g. during an aggregator unit test),
    the task is silently skipped.
    """
    if read(run_id):
        return
    if any(getattr(t, "_alice_run_id", None) == run_id for t in _inflight):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop — caller is sync (e.g. test harness)
    task = loop.create_task(_generate(run_id, events))
    setattr(task, "_alice_run_id", run_id)
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


def _build_prompt(events: list) -> str:
    """Compact narrative of a wake's events as a Haiku prompt.

    Samples the wake's thinking across its full arc — first thought, a
    middle thought, and the last two — plus all tool calls and the final
    assistant_text. The previous version only fed the FIRST thinking
    block as "Initial intent", which biased Haiku toward describing what
    the wake set out to do rather than what it actually accomplished
    end-to-end.
    """
    thoughts: list[str] = []
    final_text = None
    tool_lines: list[str] = []
    for ev in events:
        if ev.kind == "thinking":
            t = (ev.detail.get("text") or "").strip()
            if t:
                thoughts.append(t[:500])
        elif ev.kind == "assistant_text":
            t = (ev.detail.get("text") or "").strip()
            if t:
                final_text = t[:600]
        elif ev.kind == "tool_use":
            name = ev.detail.get("name", "?")
            raw_input = ev.detail.get("input", "")
            primary = _tool_primary(name, raw_input)
            tool_lines.append(
                f"- {name}" + (f": {primary[:200]}" if primary else "")
            )

    sampled_thoughts = _sample_thoughts(thoughts)

    parts = [
        "Summarize the FULL ARC of this thinking wake — what I actually",
        "accomplished from start to finish, not just what I set out to do.",
        "1–3 short sentences, ~240 characters max. Cover distinct phases",
        "if there were several (e.g. groom + fix + verify), in order.",
        "FIRST PERSON past tense — 'I groomed X, then fixed Y' not",
        "'Alice groomed X'. You ARE Alice; the wake is yours.",
        "No quotes, no preamble like 'In this wake', just the summary.",
        "",
    ]
    if sampled_thoughts:
        parts.append("Thinking samples (across the wake, in order):")
        for i, t in enumerate(sampled_thoughts, 1):
            parts.append(f"[{i}] {t}")
        parts.append("")
    if tool_lines:
        parts.append(f"Tool calls in order ({len(tool_lines)}):")
        parts.extend(tool_lines[:40])
        if len(tool_lines) > 40:
            parts.append(f"…and {len(tool_lines) - 40} more")
        parts.append("")
    if final_text:
        parts.append(f"Closing text: {final_text}")

    return "\n".join(parts)


def _sample_thoughts(thoughts: list[str]) -> list[str]:
    """Pick first, middle, and last two thoughts so the prompt covers the
    full arc rather than only the opening intent."""
    n = len(thoughts)
    if n <= 4:
        return thoughts
    return [thoughts[0], thoughts[n // 2], thoughts[-2], thoughts[-1]]


def _tool_primary(name: str, raw_input: Any) -> str:
    """Best-effort one-line description of what a tool call did."""
    if not raw_input:
        return ""
    if isinstance(raw_input, str):
        try:
            parsed = json.loads(raw_input)
        except (json.JSONDecodeError, ValueError):
            return raw_input[:200]
    else:
        parsed = raw_input
    if not isinstance(parsed, dict):
        return str(parsed)[:200]
    for key in ("file_path", "command", "pattern", "url", "query", "content", "message"):
        v = parsed.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


async def _generate(run_id: str, events: list) -> None:
    prompt = _build_prompt(events)
    try:
        from alice_core.auth import ensure_auth_env
        from alice_core.kernel import AgentKernel, KernelSpec
        from alice_core.events import CapturingEmitter
    except ImportError:
        return
    ensure_auth_env()

    emitter = CapturingEmitter()
    kernel = AgentKernel(emitter, correlation_id=f"summary-{run_id}", silent=True)
    spec = KernelSpec(
        model=HAIKU_MODEL,
        allowed_tools=[],
        cwd=pathlib.Path("/tmp"),
    )
    try:
        result = await kernel.run(prompt, spec)
    except Exception:  # noqa: BLE001
        return
    summary = (result.text or "").strip()
    if not summary:
        return
    # Strip surrounding quotes if Haiku added them despite the instruction.
    if (summary.startswith('"') and summary.endswith('"')) or (
        summary.startswith("'") and summary.endswith("'")
    ):
        summary = summary[1:-1]
    summary = summary.replace("\n", " ").strip()[:280]
    write(run_id, summary, cost_usd=result.cost_usd)


__all__ = ["read", "write", "schedule", "cache_dir"]
