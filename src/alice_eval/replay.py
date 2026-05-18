"""Replay harness — fan out sampled turns to candidate models.

For each ``(turn, candidate)`` pair the harness:

1. Reconstructs the prior conversation history (turns in the same
   conversation that precede the sampled turn).
2. Runs each text segment through :func:`alice_eval.pii.redact`.
3. Builds a ``messages`` list with system + alternating user/
   assistant blocks.
4. Issues the request to the candidate's HTTP surface:
   - Anthropic-style ``/v1/messages`` for ``provider=anthropic``.
   - OpenAI-compatible ``/v1/chat/completions`` for
     ``provider=openai_compatible``.
5. Records output text, wall-clock latency, and token counts.

Outputs JSONL — one ``eval_outputs_<candidate_id>.jsonl`` per
candidate, where each line is::

    {
      "turn_id": "turn_...",
      "candidate_id": "opus" | "qwen",
      "category": "tactical" | ...,
      "output": "...",
      "latency_ms": 1820,
      "input_tokens": 1024,
      "output_tokens": 187,
      "status": "ok" | "error",
      "error": null | "string",
      "request_ts": 1779131023.821
    }

Tool-call simplification: the design's "splice the live tool result"
behaviour assumed a richer log schema. The current log just has the
final outbound text — when the historical outbound looks tool-mediated
(per :func:`alice_eval.sampling._count_tool_markers`) we paste the
outbound prose verbatim into the assistant turn. This loses the
intermediate tool steps but preserves the final assistant reply, which
is the closest thing to "live tool result context" we can stitch from
this log. See TODO(eval) comment in :func:`_build_history`.

Network safety: tests must monkeypatch the two functions
:func:`_call_anthropic` / :func:`_call_openai_compatible` (or stub
``httpx.AsyncClient``). Production callers run via the CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

import httpx

from alice_eval import pii
from alice_eval.prompt import build_system_prompt
from alice_eval.sampling import (
    CONVERSATION_GAP_SECONDS,
    _count_tool_markers,
    group_into_conversations,
    load_speaking_log,
)

__all__ = [
    "Candidate",
    "ReplayResult",
    "build_messages",
    "load_candidates",
    "load_sample",
    "main_replay",
    "replay_turn",
]


log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 3
DEFAULT_TIMEOUT_S = 120.0
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024


@dataclass(slots=True)
class Candidate:
    """A model we fan out to. ``provider`` selects the request shape;
    ``auth_env`` is searched in order for an API key (the first
    non-empty value wins). An empty ``auth_env`` means no auth header
    (local llama-server)."""

    id: str
    label: str
    provider: str
    model: str
    base_url: str | None = None
    auth_env: list[str] = field(default_factory=list)
    max_tokens: int = DEFAULT_MAX_TOKENS

    def resolve_api_key(self) -> str | None:
        for var in self.auth_env:
            value = os.environ.get(var)
            if value:
                return value
        # Fall back to the Claude Code OAuth credentials file: same long-lived
        # token the speaking daemon already uses against api.anthropic.com.
        creds = Path.home() / ".claude" / ".credentials.json"
        if creds.is_file():
            try:
                data = json.loads(creds.read_text(encoding="utf-8"))
                token = data.get("claudeAiOauth", {}).get("accessToken")
                if token:
                    return token
            except (json.JSONDecodeError, OSError):
                pass
        return None


@dataclass(slots=True)
class ReplayResult:
    """One JSONL row in the per-candidate output file."""

    turn_id: str
    candidate_id: str
    category: str
    output: str
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    status: str
    error: str | None
    request_ts: float

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self))


# ---------------------------------------------------------------------------
# Candidate / sample loading


def load_candidates(path: str | Path) -> list[Candidate]:
    """Read ``configs/eval_candidates.json`` and return a list of
    :class:`Candidate` objects.

    The config schema is ``{"candidates": [ {<candidate>}, ... ]}``.
    """
    resolved = Path(path).expanduser()
    payload = json.loads(resolved.read_text())
    rows = payload.get("candidates") or []
    return [Candidate(**row) for row in rows]


def load_sample(path: str | Path) -> list[dict]:
    """Read ``eval_sample.jsonl`` (one JSON object per line)."""
    resolved = Path(path).expanduser()
    rows: list[dict] = []
    with resolved.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Conversation reconstruction


def _build_history(
    sampled_turn: dict, all_turns: Sequence[dict]
) -> list[tuple[str, str]]:
    """Return ``[(role, content)]`` pairs preceding ``sampled_turn``.

    The history walks the same conversation (300s gap rule) that
    contains ``sampled_turn``, takes every turn before it, and emits
    a ``user``/``assistant`` pair per prior turn. We deliberately
    keep this conservative — no MCP tool fidelity, no compaction.

    TODO(eval): the design called for splicing live tool results
    back into the conversation when the historical turn was
    tool-mediated. The current speaking log only carries the *final*
    outbound prose, which already incorporates the tool result, so
    we pass that prose through verbatim. Switch to a richer source
    (e.g. recorded MCP transcripts) when one exists.
    """
    sampled_ts = float(sampled_turn["ts"])
    sender = sampled_turn.get("sender_number")
    # Conversations within ``all_turns`` re-group cheaply; the log is
    # ~1300 lines so we don't bother caching.
    same_sender = [t for t in all_turns if t.get("sender_number") == sender]
    conversations = group_into_conversations(
        same_sender, gap_seconds=CONVERSATION_GAP_SECONDS
    )
    container: list[dict] | None = None
    for conv in conversations:
        for turn in conv:
            if abs(float(turn["ts"]) - sampled_ts) < 1e-6 and (
                turn.get("inbound") == sampled_turn.get("inbound")
            ):
                container = conv
                break
        if container is not None:
            break

    if container is None:
        return []

    history: list[tuple[str, str]] = []
    for prev in container:
        if float(prev["ts"]) >= sampled_ts:
            break
        inbound = prev.get("inbound") or ""
        outbound = prev.get("outbound") or ""
        if inbound:
            history.append(("user", pii.redact(inbound)))
        if outbound:
            # Tool-mediated outbounds get a label so the candidate
            # can see this turn involved tools without us
            # reconstructing the underlying tool transcript.
            if _count_tool_markers(outbound) >= 2:
                history.append(
                    (
                        "assistant",
                        pii.redact(outbound) + "\n\n[tool-mediated turn]",
                    )
                )
            else:
                history.append(("assistant", pii.redact(outbound)))
    return history


def build_messages(
    sampled_turn: dict,
    all_turns: Sequence[dict],
    *,
    history: list[tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Build the OpenAI-style ``messages`` list (no system block —
    that's passed separately for Anthropic and inlined for OpenAI).
    """
    history = (
        history
        if history is not None
        else _build_history(sampled_turn, all_turns)
    )
    inbound = pii.redact(sampled_turn.get("inbound") or "")
    messages: list[dict[str, str]] = [
        {"role": role, "content": content} for role, content in history
    ]
    messages.append({"role": "user", "content": inbound})
    return messages


# ---------------------------------------------------------------------------
# HTTP client paths


async def _call_anthropic(
    client: httpx.AsyncClient,
    candidate: Candidate,
    *,
    system_prompt: str,
    messages: list[dict[str, str]],
    timeout_s: float,
) -> dict[str, Any]:
    """POST to ``/v1/messages`` (or candidate-configured base_url)."""
    api_key = candidate.resolve_api_key()
    if not api_key:
        raise RuntimeError(
            f"no API key in env vars {candidate.auth_env!r} for "
            f"candidate {candidate.id!r}"
        )
    base = candidate.base_url or "https://api.anthropic.com"
    url = base.rstrip("/") + "/v1/messages"
    # Anthropic accepts either an sk-ant-* API key (x-api-key) or a Claude
    # Code OAuth bearer token. Detect by prefix so the same harness works
    # under both auth modes.
    headers = {
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }
    # OAuth tokens from Claude Code start with sk-ant-oat*; API keys start
    # with sk-ant-api*. Anthropic routes them to different auth headers.
    if api_key.startswith("sk-ant-oat"):
        headers["authorization"] = f"Bearer {api_key}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    else:
        headers["x-api-key"] = api_key
    body = {
        "model": candidate.model,
        "max_tokens": candidate.max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    response = await _post_with_retry(client, url, body, headers, timeout_s)
    return response.json()


async def _call_openai_compatible(
    client: httpx.AsyncClient,
    candidate: Candidate,
    *,
    system_prompt: str,
    messages: list[dict[str, str]],
    timeout_s: float,
) -> dict[str, Any]:
    """POST to ``/chat/completions`` against an OpenAI-compatible
    endpoint (llama-server, vLLM, OpenRouter, etc.)."""
    if not candidate.base_url:
        raise ValueError(
            f"candidate {candidate.id!r} provider=openai_compatible "
            "needs base_url"
        )
    url = candidate.base_url.rstrip("/") + "/chat/completions"
    headers = {"content-type": "application/json"}
    api_key = candidate.resolve_api_key()
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    full_messages = [{"role": "system", "content": system_prompt}, *messages]
    body = {
        "model": candidate.model,
        "messages": full_messages,
        "max_tokens": candidate.max_tokens,
    }
    response = await _post_with_retry(client, url, body, headers, timeout_s)
    return response.json()


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
    *,
    max_attempts: int = 5,
) -> httpx.Response:
    """POST with exponential backoff on transient failures (429/5xx).

    Honours an upstream ``retry-after`` header when present.
    """
    delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(
                url, json=body, headers=headers, timeout=timeout_s
            )
            if response.status_code in (429, 503, 529):
                ra = response.headers.get("retry-after")
                wait = float(ra) if ra and ra.isdigit() else delay
                await asyncio.sleep(min(wait, 60.0))
                delay *= 2
                continue
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code in (429, 503, 529) and attempt < max_attempts:
                await asyncio.sleep(min(delay, 60.0))
                delay *= 2
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"exhausted {max_attempts} retries for {url}")


def _extract_anthropic_text(payload: dict[str, Any]) -> str:
    pieces: list[str] = []
    for block in payload.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            pieces.append(block.get("text", ""))
    return "".join(pieces)


def _extract_openai_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return message.get("content") or ""


# ---------------------------------------------------------------------------
# Top-level per-turn replay


async def replay_turn(
    candidate: Candidate,
    sampled_turn: dict,
    all_turns: Sequence[dict],
    *,
    system_prompt: str,
    client: httpx.AsyncClient,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ReplayResult:
    """Run one ``(candidate, turn)`` pair end-to-end and return its
    JSONL row. Errors are caught and reflected in
    ``status="error"`` — they don't propagate out of this coroutine
    so a single failure doesn't break the gather().
    """
    turn_id = sampled_turn.get("turn_id") or "turn_unknown"
    category = sampled_turn.get("sampled_category") or "unknown"
    messages = build_messages(sampled_turn, all_turns)
    request_ts = time.time()
    started = time.perf_counter()
    output = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    status = "ok"
    error: str | None = None

    try:
        if candidate.provider == "anthropic":
            payload = await _call_anthropic(
                client,
                candidate,
                system_prompt=system_prompt,
                messages=messages,
                timeout_s=timeout_s,
            )
            output = _extract_anthropic_text(payload)
            usage = payload.get("usage") or {}
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
        elif candidate.provider == "openai_compatible":
            payload = await _call_openai_compatible(
                client,
                candidate,
                system_prompt=system_prompt,
                messages=messages,
                timeout_s=timeout_s,
            )
            output = _extract_openai_text(payload)
            usage = payload.get("usage") or {}
            input_tokens = usage.get("prompt_tokens")
            output_tokens = usage.get("completion_tokens")
        else:
            raise ValueError(
                f"unknown provider {candidate.provider!r} for "
                f"candidate {candidate.id!r}"
            )
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        log.warning(
            "replay failure: candidate=%s turn=%s err=%s",
            candidate.id,
            turn_id,
            error,
        )

    latency_ms = int((time.perf_counter() - started) * 1000)
    return ReplayResult(
        turn_id=turn_id,
        candidate_id=candidate.id,
        category=category,
        output=output,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        status=status,
        error=error,
        request_ts=request_ts,
    )


# ---------------------------------------------------------------------------
# CLI / orchestration


async def _run_all(
    candidates: Sequence[Candidate],
    sample: Sequence[dict],
    *,
    log_path: Path,
    out_dir: Path,
    concurrency: int,
    system_prompt: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, list[ReplayResult]]:
    """Drive replay over the cartesian product with bounded
    concurrency, writing each candidate's JSONL to ``out_dir``."""
    all_turns = load_speaking_log(log_path)
    semaphore = asyncio.Semaphore(concurrency)
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient()
    try:
        async def _task(candidate: Candidate, turn: dict) -> ReplayResult:
            async with semaphore:
                return await replay_turn(
                    candidate,
                    turn,
                    all_turns,
                    system_prompt=system_prompt,
                    client=client,
                )

        tasks = [
            _task(candidate, turn)
            for candidate in candidates
            for turn in sample
        ]
        gathered = await asyncio.gather(*tasks, return_exceptions=False)
    finally:
        if owns_client:
            await client.aclose()

    by_candidate: dict[str, list[ReplayResult]] = {c.id: [] for c in candidates}
    for result in gathered:
        by_candidate.setdefault(result.candidate_id, []).append(result)

    out_dir.mkdir(parents=True, exist_ok=True)
    for cand_id, rows in by_candidate.items():
        path = out_dir / f"eval_outputs_{cand_id}.jsonl"
        with path.open("w") as fh:
            for row in rows:
                fh.write(row.to_jsonl() + "\n")
        print(
            f"Wrote {len(rows)} results for candidate '{cand_id}' to {path}",
            file=sys.stderr,
        )

    return by_candidate


def main_replay(
    *,
    sample_path: str | Path,
    candidates_path: str | Path,
    out_dir: str | Path,
    log_path: str | Path,
    concurrency: int = DEFAULT_CONCURRENCY,
    personae_path: str | Path | None = None,
) -> None:
    """Synchronous entry-point used by both the CLI and tests."""
    sample = load_sample(sample_path)
    candidates = load_candidates(candidates_path)
    if not candidates:
        raise ValueError("no candidates loaded; check eval_candidates.json")
    system_prompt = build_system_prompt(personae_path=personae_path)

    asyncio.run(
        _run_all(
            candidates,
            sample,
            log_path=Path(log_path).expanduser(),
            out_dir=Path(out_dir).expanduser(),
            concurrency=concurrency,
            system_prompt=system_prompt,
        )
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alice_eval.replay",
        description="Replay sampled turns through candidate models.",
    )
    parser.add_argument(
        "--sample", type=str, default="eval_sample.jsonl",
        help="Path to eval_sample.jsonl",
    )
    parser.add_argument(
        "--candidates", type=str, default="configs/eval_candidates.json",
        help="Path to the candidate config JSON",
    )
    parser.add_argument(
        "--out-dir", type=str, default="eval_outputs",
        help="Directory to write per-candidate JSONL outputs",
    )
    parser.add_argument(
        "--log", type=str,
        default="~/alice-mind/inner/state/speaking-turns.jsonl",
        help="Path to speaking-turns.jsonl (for history reconstruction)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help="Maximum in-flight HTTP requests across all candidates",
    )
    parser.add_argument(
        "--personae",
        type=str,
        default=None,
        help="Optional override path for personae.yml",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    main_replay(
        sample_path=args.sample,
        candidates_path=args.candidates,
        out_dir=args.out_dir,
        log_path=args.log,
        concurrency=args.concurrency,
        personae_path=args.personae,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
