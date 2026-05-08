"""LLM-calling subroutines for the Stage B workflow.

Each subroutine is a small, single-task callable. Tests inject mocks via
the seam pattern from :mod:`alice_thinking.design_pipeline` (the
``model_call`` / ``ModelCall`` callable arg). Production runs dispatch
through the kernel layer (``alice_core.kernel.make_kernel`` →
PiKernel/AnthropicKernel) using the model.yml thinking backend.

Workflow code never imports a concrete kernel impl — it goes through
:func:`make_default_model_call`, which constructs a kernel from the
backend the rest of thinking already uses.
"""

from __future__ import annotations

import asyncio
import json
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
]


# A model-call seam: takes (system_prompt, user_prompt) and returns the
# raw assistant text. Same shape as ``SubAgentRunner.review_text`` in
# :mod:`alice_thinking.design_pipeline`. Tests inject a mock; production
# wires up :func:`make_default_model_call`.
ModelCall = Callable[[str, str], Awaitable[str]]


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_json_fences(text: str) -> str:
    body = (text or "").strip()
    m = _FENCE_RE.match(body)
    if m:
        return m.group(1).strip()
    return body


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
    """Load a prompt fragment from ``prompts/<name>.md`` (package
    resource). Read once per call; small files, no caching needed.
    """
    pkg = "alice_thinking.workflows.stage_b.prompts"
    return resources.files(pkg).joinpath(f"{name}.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Default model-call seam
# ---------------------------------------------------------------------------


def make_default_model_call(
    *,
    model: str,
    backend: Optional[Any] = None,
    correlation_id: Optional[str] = None,
) -> ModelCall:
    """Construct a :class:`ModelCall` that dispatches through the kernel.

    Used by production code in :mod:`alice_thinking.workflows.stage_b.runner`.
    Tests should NOT call this — they inject a fake :class:`ModelCall`
    directly.

    The kernel is constructed lazily on first call so module import
    doesn't pull in the SDK.
    """

    async def _call(system_prompt: str, user_prompt: str) -> str:
        from alice_core.events import EventLogger
        from alice_core.kernel import KernelSpec, make_kernel

        if backend is None:
            from alice_core.config.model import BackendSpec

            spec_backend = BackendSpec(backend="subscription")
        else:
            spec_backend = backend

        # No-op emitter — workflow tracks its own telemetry.
        emitter = EventLogger(pathlib.Path("/dev/null"))
        kernel = make_kernel(
            spec_backend,
            emitter,
            correlation_id=correlation_id,
            silent=True,
            short_cap=2000,
        )
        kspec = KernelSpec(
            model=model,
            allowed_tools=[],
            cwd=None,
            max_seconds=60,
            thinking="medium",
            append_system_prompt=system_prompt,
        )
        result = await kernel.run(user_prompt, kspec)
        return (result.text or "").strip()

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
    """Classify one inbox note and return the typed :class:`Action`.

    ``model_call`` is the seam tests inject. Production passes
    :func:`make_default_model_call`'s output.
    """
    system_prompt = load_prompt_fragment("classify_note")
    index_summary = ""
    if vault_index:
        index_summary = "\n\n## vault index summary\n\n" + json.dumps(
            vault_index, indent=2
        )
    user_prompt = (
        f"## note path\n\n{note_path.name}\n\n"
        f"## note body\n\n{note_body}{index_summary}"
    )
    raw = await model_call(system_prompt, user_prompt)
    blob = _parse_json_response(raw)
    return _coerce_action(blob)


# ---------------------------------------------------------------------------
# produce_grooming_diff
# ---------------------------------------------------------------------------


def _coerce_diff(blob: dict[str, Any]) -> Diff:
    fm_changes: list[FrontmatterChange] = []
    for entry in blob.get("frontmatter_changes") or []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not key:
            continue
        new_value = entry.get("new_value")
        fm_changes.append(
            FrontmatterChange(
                key=str(key),
                new_value=None if new_value is None else str(new_value),
            )
        )

    wl_fixes: list[WikilinkFix] = []
    for entry in blob.get("wikilink_fixes") or []:
        if not isinstance(entry, dict):
            continue
        old_t = entry.get("old_target")
        new_t = entry.get("new_target")
        if not old_t or not new_t:
            continue
        wl_fixes.append(WikilinkFix(old_target=str(old_t), new_target=str(new_t)))

    sec_edits: list[SectionEdit] = []
    for entry in blob.get("section_edits") or []:
        if not isinstance(entry, dict):
            continue
        heading = entry.get("heading")
        new_body = entry.get("new_body")
        if not heading or new_body is None:
            continue
        sec_edits.append(SectionEdit(heading=str(heading), new_body=str(new_body)))

    return Diff(
        frontmatter_changes=fm_changes,
        wikilink_fixes=wl_fixes,
        section_edits=sec_edits,
        rationale=str(blob.get("rationale", "")),
    )


async def produce_grooming_diff(
    *,
    target_path: pathlib.Path,
    current_content: str,
    vault_index: Optional[dict[str, Any]] = None,
    model_call: ModelCall,
) -> Diff:
    """Produce a typed :class:`Diff` for the given grooming target."""
    system_prompt = load_prompt_fragment("produce_grooming_diff")
    index_summary = ""
    if vault_index:
        index_summary = "\n\n## vault index summary\n\n" + json.dumps(
            vault_index, indent=2
        )
    user_prompt = (
        f"## target path\n\n{target_path}\n\n"
        f"## current content\n\n{current_content}{index_summary}"
    )
    raw = await model_call(system_prompt, user_prompt)
    blob = _parse_json_response(raw)
    return _coerce_diff(blob)


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
    """Return ``{"verdict": "resolved"|"still_open", "summary": "..."}``."""
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
    """Return ``{"tldr": "..."}`` — empty string means no write needed."""
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
    """Return ``{"verdict": "conflict"|"no_conflict", "slug"?, "summary"}``."""
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
# Helpers — surface payload coercion (used by Step 2 dispatcher)
# ---------------------------------------------------------------------------


def coerce_surface_payload(payload: dict[str, Any]) -> SurfacePayload:
    """Coerce a model-emitted surface_payload dict into :class:`SurfacePayload`."""
    surface_type = str(payload.get("surface_type") or "stage-b-routed").strip()
    body = str(payload.get("body") or "")
    extra = payload.get("extra_frontmatter") or {}
    if not isinstance(extra, dict):
        extra = {}
    return SurfacePayload(
        surface_type=surface_type, body=body, extra_frontmatter=dict(extra)
    )


# Make asyncio explicitly imported only when needed; keep here for parity.
_ = asyncio  # silence "unused import" if a future helper needs it
