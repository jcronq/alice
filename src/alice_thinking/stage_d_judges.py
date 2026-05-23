"""Stage D dual-judge quality gate — judge call sites.

Two independent judges (Qwen + Haiku) score a candidate Stage D synthesis
against one shared rubric (T1/T2/T3/T4 + novelty). Both judges score the
same quality definition; the prompt texts differ per model for
bias-compensation:

- Qwen judge — restrict format, damp verbosity, frame reject as valid.
- Haiku judge — invite skepticism, default to critical, frame thoroughness
  as the value-add.

Source of truth for the prompt texts is
``cortex-memory/research/2026-05-09-stage-d-judge-prompts.md`` — the strings
embedded below are copied verbatim and substitute the synthesis text into
the ``{{synthesis_text}}`` placeholder.

Call shapes:

- :func:`judge_qwen` dispatches via google-adk's LiteLlm wrapper to the LAN
  Qwen endpoint at ``http://10.20.30.177:8033/v1`` (model
  ``openai/Qwen3-Coder-Next``). Reuses the pattern from
  :mod:`alice_thinking.workflows.stage_b.subroutines`.
- :func:`judge_haiku` dispatches via the anthropic SDK using
  ``claude-haiku-4-5-20251001``. Reads ``ANTHROPIC_API_KEY`` from env.

Both return a typed :class:`Verdict`. Malformed model output raises
:class:`JudgeOutputError` — let the caller decide retry semantics.

Imports of the underlying SDK clients are lazy (inside each judge
function) so module import stays cheap and tests can mock at the call
site without pulling in google-adk / anthropic.
"""

from __future__ import annotations

import json
import os
import re
from typing import Literal, Optional, TypedDict

from core.config.auth import ensure_auth_env


__all__ = [
    "Verdict",
    "JudgeOutputError",
    "judge_qwen",
    "judge_haiku",
    "QWEN_JUDGE_PROMPT_TEMPLATE",
    "HAIKU_JUDGE_PROMPT_TEMPLATE",
]


Tier = Literal["T1", "T2", "T3", "T4"]
Decision = Literal["ship", "reject"]


class Verdict(TypedDict):
    """One judge's verdict on one synthesis attempt.

    Fields match the JSONL schema in
    ``src/viewer/STAGE_D_SCHEMA.md`` ; ``novel`` is a bool here per
    the judge-prompts design and is stored as-is by the pipeline.
    """

    tier: Tier
    novel: bool
    reason: str
    decision: Decision


class JudgeOutputError(ValueError):
    """Raised when a judge returns output that can't be parsed into a
    :class:`Verdict`. Caller decides retry semantics."""


# ---------------------------------------------------------------------------
# Prompt templates — verbatim from
# cortex-memory/research/2026-05-09-stage-d-judge-prompts.md
# ---------------------------------------------------------------------------


# Qwen — restrict format, damp verbosity, explicit reject permission.
QWEN_JUDGE_PROMPT_TEMPLATE = """\
You are a Stage D quality judge evaluating a synthesis note produced by
combining two research notes. Your job is to rate the synthesis honestly.
A rejection is a valid and useful output — do not penalize yourself for
saying reject.

Rubric:
T1 — Non-obvious connection that changes approach to either domain.
T2 — Real but predictable connection. Correct and useful, but obvious
     once pointed out.
T3 — Forced or abstract-level only. Doesn't survive contact with
     concrete practice in either domain.
T4 — Null result. The domains share no actionable structure.

Novelty: Has this exact pair (A→B) produced a synthesis before? If yes,
does this add a genuinely new angle, or restate the same connection?

Source A:
---
{source_a_text}
---

Source B:
---
{source_b_text}
---

Prior synthesis for this pair (if any):
---
{prior_pair_synthesis}
---

The synthesis to evaluate:
---
{synthesis_text}
---

Output EXACTLY this JSON, nothing else:
{{"tier": "T1|T2|T3|T4", "novel": true|false, "reason": "<one sentence>", "decision": "ship|reject"}}

Constraints:
- Tier must be T1, T2, T3, or T4.
- Novel is true only if this pair has NOT produced a prior synthesis, OR
  if this synthesis identifies a genuinely new angle on a previously
  synthesized pair.
- Reason must be ONE sentence. No preamble. No explanation beyond the
  one sentence.
- Decision is ship if (tier is T1) OR (tier is T2 AND novel is true).
  Reject otherwise.
"""


# Haiku — invite skepticism, default to critical, thoroughness frame.
HAIKU_JUDGE_PROMPT_TEMPLATE = """\
You are a Stage D quality judge evaluating a synthesis note produced by
combining two research notes. Your job is to be thorough and skeptical.
Finding reasons to reject is your specialty — that is valuable work.
A careful rejection is better than a lazy acceptance.

Rubric:
T1 — Non-obvious connection that changes approach to either domain.
T2 — Real but predictable connection. Correct and useful, but obvious
     once pointed out.
T3 — Forced or abstract-level only. Doesn't survive contact with
     concrete practice in either domain.
T4 — Null result. The domains share no actionable structure.

Novelty: Has this exact pair (A→B) produced a synthesis before? If yes,
does this synthesis add a genuinely new angle, or restate the same
connection?

Source A:
---
{source_a_text}
---

Source B:
---
{source_b_text}
---

Prior synthesis for this pair (if any):
---
{prior_pair_synthesis}
---

The synthesis to evaluate:
---
{synthesis_text}
---

Output EXACTLY this JSON, nothing else:
{{"tier": "T1|T2|T3|T4", "novel": true|false, "reason": "<one sentence>", "decision": "ship|reject"}}

Constraints:
- Default to skepticism. A synthesis must earn its tier rating.
- If the connection is interesting but requires intellectual leaps to
  justify, it's T3 — not T2.
- If the synthesis is technically correct but the connection between
  domains is obvious, it's T2 — not T1.
- Novel is true only if this pair has NOT produced a prior synthesis,
  OR if this synthesis identifies a genuinely new angle on a previously
  synthesized pair.
- Reason must be ONE sentence. Be specific about WHY.
- Decision is ship if (tier is T1) OR (tier is T2 AND novel is true).
  Reject otherwise.
"""


# ---------------------------------------------------------------------------
# JSON parsing — fenced-block tolerant, mirrors design_pipeline.py
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_json_fences(text: str) -> str:
    body = (text or "").strip()
    m = _FENCE_RE.match(body)
    if m:
        return m.group(1).strip()
    return body


_VALID_TIERS = {"T1", "T2", "T3", "T4"}
_VALID_DECISIONS = {"ship", "reject"}


def _parse_verdict(text: str) -> Verdict:
    """Parse a judge's raw text response into a :class:`Verdict`.

    Tolerates ```json fences. Raises :class:`JudgeOutputError` on any
    structural problem (empty, non-JSON, missing field, bad enum, wrong
    type).
    """
    body = _strip_json_fences(text)
    if not body:
        raise JudgeOutputError("judge returned empty response")
    try:
        blob = json.loads(body)
    except json.JSONDecodeError as exc:
        raise JudgeOutputError(f"judge returned invalid JSON: {exc}") from exc
    if not isinstance(blob, dict):
        raise JudgeOutputError("judge JSON must be an object")

    tier = blob.get("tier")
    if tier not in _VALID_TIERS:
        raise JudgeOutputError(f"judge returned bad tier: {tier!r}")

    novel_raw = blob.get("novel")
    if isinstance(novel_raw, bool):
        novel = novel_raw
    elif isinstance(novel_raw, str) and novel_raw.lower() in ("yes", "true"):
        novel = True
    elif isinstance(novel_raw, str) and novel_raw.lower() in ("no", "false"):
        novel = False
    else:
        raise JudgeOutputError(f"judge returned bad novel: {novel_raw!r}")

    decision = blob.get("decision")
    if decision not in _VALID_DECISIONS:
        raise JudgeOutputError(f"judge returned bad decision: {decision!r}")

    reason = blob.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise JudgeOutputError("judge missing or empty reason")

    return Verdict(
        tier=tier,  # type: ignore[typeddict-item]
        novel=novel,
        reason=reason.strip(),
        decision=decision,  # type: ignore[typeddict-item]
    )


def _format_prompt(template: str, *, synthesis: str, source_a_text: str,
                   source_b_text: str, prior_pair_synthesis: Optional[str]) -> str:
    """Substitute the four placeholders into a judge prompt template."""
    prior = prior_pair_synthesis if prior_pair_synthesis else "(none — first attempt on this pair)"
    return template.format(
        synthesis_text=synthesis,
        source_a_text=source_a_text,
        source_b_text=source_b_text,
        prior_pair_synthesis=prior,
    )


# ---------------------------------------------------------------------------
# Qwen judge — google-adk LiteLlm against the LAN endpoint
# ---------------------------------------------------------------------------


def judge_qwen(
    *,
    synthesis: str,
    source_a_text: str,
    source_b_text: str,
    prior_pair_synthesis: Optional[str],
) -> Verdict:
    """Run the Qwen judge against ``synthesis`` and return its
    :class:`Verdict`.

    Dispatches via google-adk's LiteLlm wrapper to the local Qwen
    endpoint (OpenAI-compatible). No auth required — the LAN endpoint
    is unauthenticated.

    Raises :class:`JudgeOutputError` if the model returns malformed
    output (caller retries).
    """

    prompt = _format_prompt(
        QWEN_JUDGE_PROMPT_TEMPLATE,
        synthesis=synthesis,
        source_a_text=source_a_text,
        source_b_text=source_b_text,
        prior_pair_synthesis=prior_pair_synthesis,
    )
    raw = _call_qwen(prompt)
    return _parse_verdict(raw)


def _call_qwen(prompt: str) -> str:
    """Synchronous wrapper around the LiteLlm async call. Lazy-imports
    google-adk so module import stays light and tests can monkeypatch
    this seam."""

    import asyncio

    from google.adk.models.lite_llm import LiteLlm
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types as gtypes

    model = "openai/Qwen3-Coder-Next"
    api_base = "http://10.20.30.177:8033/v1"

    async def _run() -> str:
        adapter = LiteLlm(
            model=model, api_base=api_base, api_key="not-required", drop_params=True
        )
        req = LlmRequest(
            model=model,
            contents=[
                gtypes.Content(
                    role="user",
                    parts=[gtypes.Part.from_text(text=prompt)],
                ),
            ],
            config=gtypes.GenerateContentConfig(
                system_instruction="",
                temperature=0.0,
            ),
        )
        async for resp in adapter.generate_content_async(req, stream=False):
            content = resp.content
            if content is None or not content.parts:
                continue
            for part in content.parts:
                text = getattr(part, "text", None)
                if text:
                    return text.strip()
        return ""

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Haiku judge — direct anthropic SDK
# ---------------------------------------------------------------------------


HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"


def judge_haiku(
    *,
    synthesis: str,
    source_a_text: str,
    source_b_text: str,
    prior_pair_synthesis: Optional[str],
) -> Verdict:
    """Run the Haiku judge against ``synthesis`` and return its
    :class:`Verdict`.

    Dispatches via the anthropic SDK using ``claude-haiku-4-5-20251001``.
    Reads ``ANTHROPIC_API_KEY`` from the environment — same key the
    kernel uses.

    Raises :class:`JudgeOutputError` if the model returns malformed
    output (caller retries) or if ``ANTHROPIC_API_KEY`` is unset.
    """
    prompt = _format_prompt(
        HAIKU_JUDGE_PROMPT_TEMPLATE,
        synthesis=synthesis,
        source_a_text=source_a_text,
        source_b_text=source_b_text,
        prior_pair_synthesis=prior_pair_synthesis,
    )
    raw = _call_haiku(prompt)
    return _parse_verdict(raw)


def _call_haiku(prompt: str) -> str:
    """Synchronous wrapper around the Anthropic Messages call. Lazy
    imports the SDK and is the seam tests monkeypatch."""

    # Populate ANTHROPIC_API_KEY (and friends) for the current auth mode
    # before reading — without this, non-``api`` modes silently miss the key.
    ensure_auth_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise JudgeOutputError("ANTHROPIC_API_KEY not set in environment")

    import anthropic  # type: ignore

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=HAIKU_MODEL_ID,
        max_tokens=512,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    # The Messages API returns a list of content blocks. Concatenate any
    # text blocks; ignore tool-use / thinking blocks (we don't request them).
    parts: list[str] = []
    for block in getattr(msg, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip()
