"""LLM subroutines for the Stage B (ADK) workflow.

Each subroutine is a small async callable: ``(system, user) -> json``.
Tests inject a mock at the :class:`ModelCall` seam; production wires
:func:`make_default_model_call` which dispatches through google-adk's
``LiteLlm`` to the local Qwen endpoint (OpenAI-compatible).

The seam shape mirrors the ``SubAgentRunner`` pattern from
``alice_thinking.design_pipeline`` — the workflow code never imports a
concrete LLM client; tests pass a fake :class:`ModelCall` directly.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from importlib import resources
from typing import Any, Awaitable, Callable, Optional

from .types import (
    Action,
    AppendToDaily,
    CreateConflictNote,
    Diff,
    Discard,
    FrontmatterChange,
    PromoteToVault,
    RouteToSurface,
    SectionEdit,
    SurfacePayload,
    WikilinkFix,
)


__all__ = [
    "ModelCall",
    "make_default_model_call",
    "load_prompt_fragment",
    "classify_and_route_note",
    "produce_grooming_diff",
    "stale_finding_lint",
    "shadow_neighbor_tldr",
    "conflict_scan",
    "coerce_surface_payload",
]


# Async (system, user) -> raw assistant text. Tests inject a fake.
ModelCall = Callable[[str, str], Awaitable[str]]


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_json_fences(text: str) -> str:
    body = (text or "").strip()
    m = _FENCE_RE.match(body)
    return m.group(1).strip() if m else body


def _parse_json_response(text: str) -> dict[str, Any]:
    body = _strip_json_fences(text)
    if not body:
        raise ValueError("model returned empty response")
    try:
        blob = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(blob, dict):
        raise ValueError("model JSON must be an object")
    return blob


def load_prompt_fragment(name: str) -> str:
    """Load ``prompts/<name>.md`` as a package resource (small, no cache)."""
    pkg = "alice_thinking.workflows.stage_b.prompts"
    return resources.files(pkg).joinpath(f"{name}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Default model-call seam — google-adk's LiteLlm wrapper → local Qwen
# ---------------------------------------------------------------------------


def make_default_model_call(
    *,
    model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
) -> ModelCall:
    """Construct the production :class:`ModelCall`.

    Wraps ``google.adk.models.lite_llm.LiteLlm.generate_content_async``
    with the JSON-only Qwen endpoint as the OpenAI-compatible provider.
    Lazy import so module-load doesn't pull in google-adk for tests
    that inject a fake.

    ``model`` and ``api_base`` default to the LiteLLM proxy
    (``LITELLM_BASE_URL`` / ``LITELLM_QWEN_MODEL``) when set; otherwise
    fall back to the direct LAN endpoint so host-side dev still works.
    """

    if model is None:
        model = os.environ.get("LITELLM_QWEN_MODEL", "openai/qwen-local")
    if api_base is None:
        api_base = os.environ.get(
            "LITELLM_BASE_URL", "http://10.20.30.177:8033/v1"
        )
    if api_key is None:
        # Bearer token for the LiteLLM proxy (its master key); "not-required"
        # against the direct LAN fallback (unauthenticated).
        api_key = os.environ.get("LITELLM_MASTER_KEY", "not-required")

    async def _call(system_prompt: str, user_prompt: str) -> str:
        # Lazy imports — keeps test paths light + avoids a hard
        # google-adk dep at module import time.
        from google.adk.models.lite_llm import LiteLlm
        from google.adk.models.llm_request import LlmRequest
        from google.genai import types as gtypes

        adapter = LiteLlm(
            model=model, api_base=api_base, api_key=api_key, drop_params=True
        )
        req = LlmRequest(
            model=model,
            contents=[
                gtypes.Content(
                    role="user",
                    parts=[gtypes.Part.from_text(text=user_prompt)],
                ),
            ],
            config=gtypes.GenerateContentConfig(
                system_instruction=system_prompt,
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

    return _call


# ---------------------------------------------------------------------------
# classify_and_route_note
# ---------------------------------------------------------------------------


def _coerce_action(blob: dict[str, Any]) -> Action:
    action = str(blob.get("action", "")).strip()
    if action == "promote_to_vault":
        target = blob.get("target_path") or ""
        content = blob.get("new_content") or ""
        if not target or not content:
            raise ValueError("promote_to_vault requires target_path and new_content")
        return PromoteToVault(
            target_path=pathlib.Path(target),
            new_content=str(content),
            reason=str(blob.get("reason", "")),
        )
    if action == "append_to_daily":
        line = blob.get("line") or ""
        if not line:
            raise ValueError("append_to_daily requires line")
        return AppendToDaily(line=str(line), reason=str(blob.get("reason", "")))
    if action == "create_conflict_note":
        slug = blob.get("slug") or ""
        body = blob.get("body") or ""
        if not slug or not body:
            raise ValueError("create_conflict_note requires slug and body")
        return CreateConflictNote(
            slug=str(slug), body=str(body), reason=str(blob.get("reason", ""))
        )
    if action == "route_to_surface":
        payload = blob.get("surface_payload") or {}
        if not isinstance(payload, dict) or "surface_type" not in payload:
            raise ValueError(
                "route_to_surface requires surface_payload with surface_type"
            )
        return RouteToSurface(
            surface_payload=dict(payload), reason=str(blob.get("reason", ""))
        )
    if action == "discard":
        return Discard(reason=str(blob.get("reason", "")))
    raise ValueError(f"unknown action {action!r}")


async def classify_and_route_note(
    *,
    note_path: pathlib.Path,
    note_body: str,
    vault_index: Optional[dict[str, Any]] = None,
    model_call: ModelCall,
) -> Action:
    """One inbox note → typed :class:`Action`."""
    system_prompt = load_prompt_fragment("classify_note")
    index_summary = (
        "\n\n## vault index summary\n\n" + json.dumps(vault_index, indent=2)
        if vault_index
        else ""
    )
    user_prompt = (
        f"## note path\n\n{note_path.name}\n\n"
        f"## note body\n\n{note_body}{index_summary}"
    )
    raw = await model_call(system_prompt, user_prompt)
    return _coerce_action(_parse_json_response(raw))


# ---------------------------------------------------------------------------
# produce_grooming_diff
# ---------------------------------------------------------------------------


def _coerce_diff(blob: dict[str, Any]) -> Diff:
    fm: list[FrontmatterChange] = []
    for entry in blob.get("frontmatter_changes") or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not key:
            continue
        new_value = entry.get("new_value")
        fm.append(
            FrontmatterChange(
                key=str(key),
                new_value=None if new_value is None else str(new_value),
            )
        )
    wl: list[WikilinkFix] = []
    for entry in blob.get("wikilink_fixes") or []:
        if not isinstance(entry, dict):
            continue
        old_t = entry.get("old_target")
        new_t = entry.get("new_target")
        if old_t and new_t:
            wl.append(WikilinkFix(old_target=str(old_t), new_target=str(new_t)))
    se: list[SectionEdit] = []
    for entry in blob.get("section_edits") or []:
        if not isinstance(entry, dict):
            continue
        h = entry.get("heading")
        b = entry.get("new_body")
        if h and b is not None:
            se.append(SectionEdit(heading=str(h), new_body=str(b)))
    return Diff(
        frontmatter_changes=fm,
        wikilink_fixes=wl,
        section_edits=se,
        rationale=str(blob.get("rationale", "")),
    )


async def produce_grooming_diff(
    *,
    target_path: pathlib.Path,
    current_content: str,
    vault_index: Optional[dict[str, Any]] = None,
    model_call: ModelCall,
) -> Diff:
    system_prompt = load_prompt_fragment("produce_grooming_diff")
    index_summary = (
        "\n\n## vault index summary\n\n" + json.dumps(vault_index, indent=2)
        if vault_index
        else ""
    )
    user_prompt = (
        f"## target path\n\n{target_path}\n\n"
        f"## current content\n\n{current_content}{index_summary}"
    )
    raw = await model_call(system_prompt, user_prompt)
    return _coerce_diff(_parse_json_response(raw))


# ---------------------------------------------------------------------------
# Side-check subroutines
# ---------------------------------------------------------------------------


async def stale_finding_lint(
    *,
    note_path: pathlib.Path,
    note_body: str,
    neighbor_snippets: list[str],
    model_call: ModelCall,
) -> dict[str, Any]:
    """Returns ``{verdict: "resolved"|"still_open", summary: ...}``."""
    system_prompt = load_prompt_fragment("stale_finding_lint")
    snippets = "\n\n---\n\n".join(neighbor_snippets) or "(no neighbors)"
    user_prompt = (
        f"## candidate note\n\n{note_path}\n\n{note_body}\n\n"
        f"## neighbor snippets\n\n{snippets}"
    )
    raw = await model_call(system_prompt, user_prompt)
    return _parse_json_response(raw)


async def shadow_neighbor_tldr(
    *,
    neighbor_path: pathlib.Path,
    neighbor_body: str,
    hub_path: pathlib.Path,
    hub_body: str,
    model_call: ModelCall,
) -> dict[str, Any]:
    """Returns ``{tldr: "..."}`` — empty string means no write needed."""
    system_prompt = load_prompt_fragment("shadow_neighbor")
    user_prompt = (
        f"## dormant neighbor\n\n{neighbor_path}\n\n{neighbor_body}\n\n"
        f"## hub note\n\n{hub_path}\n\n{hub_body}"
    )
    raw = await model_call(system_prompt, user_prompt)
    return _parse_json_response(raw)


async def conflict_scan(
    *,
    target_path: pathlib.Path,
    target_body: str,
    neighbor_pairs: list[tuple[pathlib.Path, str]],
    model_call: ModelCall,
) -> dict[str, Any]:
    """Returns ``{verdict: "conflict"|"no_conflict", slug?, summary}``."""
    system_prompt = load_prompt_fragment("conflict_scan")
    neighbors_text = (
        "\n\n---\n\n".join(f"### {p}\n\n{b}" for p, b in neighbor_pairs)
        or "(no neighbors)"
    )
    user_prompt = (
        f"## target\n\n{target_path}\n\n{target_body}\n\n"
        f"## neighbors\n\n{neighbors_text}"
    )
    raw = await model_call(system_prompt, user_prompt)
    return _parse_json_response(raw)


# ---------------------------------------------------------------------------
# Helpers — surface payload coercion (used by drain_inbox dispatcher)
# ---------------------------------------------------------------------------


def coerce_surface_payload(payload: dict[str, Any]) -> SurfacePayload:
    surface_type = str(payload.get("surface_type") or "stage-b-routed").strip()
    body = str(payload.get("body") or "")
    extra = payload.get("extra_frontmatter") or {}
    if not isinstance(extra, dict):
        extra = {}
    return SurfacePayload(
        surface_type=surface_type, body=body, extra_frontmatter=dict(extra)
    )
