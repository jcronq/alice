"""Translate OpenAI-compatible chat completions ↔ Alice types.

Hermes (NousResearch) is served via an OpenAI-schema
``/v1/chat/completions`` endpoint. This module isolates the
schema-translation layer so :mod:`kernels.hermes.kernel` can stay
focused on the request-loop + observability wiring.

Mapping:

- OpenAI assistant message → Alice blocks
  - ``choice.message.content`` (str)                 → :class:`TextBlock`
  - ``choice.message.reasoning_content`` (str)       → :class:`ThinkingBlock`
    (some OpenAI-compat backends expose the chain-of-thought under
    this key when reasoning mode is on; harmless if absent).
  - ``choice.message.tool_calls[]``                  → :class:`ToolUseBlock`
    (``function.arguments`` is a JSON string per OpenAI spec; we
    parse it to a dict before handing off.)
- Alice tool allowlist → OpenAI ``tools`` array
  - Each entry becomes ``{"type": "function", "function": {"name":
    ..., "description": ..., "parameters": {...}}}``.
  - Alice's Claude-Code-style tool names (``Bash``, ``Read``, …)
    pass through as-is — OpenAI function calling uses the name
    verbatim. Tools with no Hermes equivalent (``WebFetch``,
    ``WebSearch``) drop silently, mirroring PiKernel's behaviour.
- OpenAI ``usage`` block → :class:`UsageInfo` (Anthropic-shaped
  field names so existing event-log aggregators keep working).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from core.kernel import UsageInfo


__all__ = [
    "Block",
    "TextBlock",
    "ToolUseBlock",
    "ThinkingBlock",
    "openai_message_to_blocks",
    "alice_block_to_openai",
    "alice_tools_to_openai",
    "openai_usage_to_info",
    "TOOL_SCHEMAS",
]


# --- Simple block dataclasses -------------------------------------------------
# We use in-module dataclasses (mirroring the SDK's TextBlock etc.
# shape) rather than importing from ``claude_agent_sdk`` because the
# hermes kernel must not carry an Anthropic SDK dependency — Hermes
# runs completely off the OpenAI wire.


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class ThinkingBlock:
    thinking: str


Block = Any  # union alias for downstream typing


# --- Tool schemas -------------------------------------------------------------
# Minimal JSON-Schema fragments for Alice's standard tools. OpenAI
# function calling requires each tool declaration to carry a
# ``parameters`` JSON Schema so the model knows the input shape.
# Alice tools don't ship their own OpenAPI shapes (the Anthropic SDK
# supplies them), so we mirror the field lists here.
#
# Kept intentionally coarse — Hermes reads these to know "which
# fields exist"; the actual validation still happens at tool-execution
# time (which HermesKernel doesn't own — see kernel.py's docstring on
# the tool-execution scope limitation).

_STRING = {"type": "string"}
_STRING_OPTIONAL = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "Bash": {
        "description": "Run a shell command and return stdout/stderr.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {**_STRING, "description": "Shell command to execute."},
                "description": _STRING_OPTIONAL,
                "timeout": _INT,
            },
            "required": ["command"],
        },
    },
    "Read": {
        "description": "Read a file from the local filesystem.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {**_STRING, "description": "Absolute file path."},
                "offset": _INT,
                "limit": _INT,
            },
            "required": ["file_path"],
        },
    },
    "Write": {
        "description": "Write content to a file, overwriting existing.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {**_STRING, "description": "Absolute file path."},
                "content": _STRING,
            },
            "required": ["file_path", "content"],
        },
    },
    "Edit": {
        "description": "Replace old_string with new_string in file.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": _STRING,
                "old_string": _STRING,
                "new_string": _STRING,
                "replace_all": _BOOL,
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    "Grep": {
        "description": "ripgrep-powered content search.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": _STRING,
                "path": _STRING_OPTIONAL,
                "glob": _STRING_OPTIONAL,
                "output_mode": _STRING_OPTIONAL,
            },
            "required": ["pattern"],
        },
    },
    "Glob": {
        "description": "Glob pattern file matching.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": _STRING,
                "path": _STRING_OPTIONAL,
            },
            "required": ["pattern"],
        },
    },
    "LS": {
        "description": "List directory contents.",
        "parameters": {
            "type": "object",
            "properties": {"path": _STRING},
            "required": ["path"],
        },
    },
    "Ls": {
        "description": "List directory contents.",
        "parameters": {
            "type": "object",
            "properties": {"path": _STRING},
            "required": ["path"],
        },
    },
    "mcp__alice__send_message": {
        "description": (
            "Send a message to the user or another configured Alice principal. "
            "Returning text alone does not deliver it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient": {
                    **_STRING,
                    "description": (
                        "'self'/'reply' for current channel, or principal id, "
                        "or E.164 number."
                    ),
                },
                "message": {**_STRING, "description": "Text to deliver."},
                "attachments": {
                    "type": "array",
                    "items": _STRING,
                    "description": "Optional attachment filesystem paths.",
                },
            },
            "required": ["recipient", "message"],
        },
    },
}


# Tools with no Hermes analog — dropped silently at translation time.
_DROP_TOOLS: frozenset[str] = frozenset({"WebFetch", "WebSearch"})


def alice_tools_to_openai(allowed: list[str]) -> list[dict[str, Any]]:
    """Translate Alice's allowed_tools list to OpenAI function schemas.

    Unknown tool names are emitted with a minimal permissive schema
    (a generic ``object`` params bag) rather than dropped — that way
    an operator whose allowlist has a custom tool still surfaces the
    request to the model, and any argument mismatch surfaces on the
    tool-execution side rather than being silently masked here.

    Returns ``[]`` when every entry drops — caller should omit the
    ``tools`` field entirely so the endpoint runs without function
    calling rather than with an empty tool set (which some backends
    reject).
    """
    out: list[dict[str, Any]] = []
    for name in allowed:
        if name in _DROP_TOOLS:
            continue
        schema = TOOL_SCHEMAS.get(name)
        if schema is None:
            # Unknown tool: register with a permissive object schema so
            # the model can still invoke it. Downstream tool-executor
            # sees the request and either dispatches or rejects.
            schema = {
                "description": f"Alice tool {name!r}.",
                "parameters": {"type": "object", "properties": {}},
            }
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": schema.get("description", ""),
                    "parameters": schema.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                },
            }
        )
    return out


# --- Message → block translation ---------------------------------------------

def openai_message_to_blocks(msg: dict) -> list[Block]:
    """Convert one OpenAI assistant ``message`` dict to Alice blocks.

    Order in the returned list is deterministic:
    ``ThinkingBlock`` (if present) → ``TextBlock`` (if content) →
    each ``ToolUseBlock`` in the order the model returned them.
    Downstream event emission relies on this ordering.

    A malformed ``tool_calls[].function.arguments`` (non-JSON string)
    is passed through with ``input={"_raw": "<string>"}`` rather than
    raising — the tool-executor layer decides how to handle bad
    arguments (a raise here would tear down the whole turn on the
    first malformed call).
    """
    blocks: list[Block] = []
    if not isinstance(msg, dict):
        return blocks

    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        blocks.append(ThinkingBlock(thinking=reasoning))

    content = msg.get("content")
    if isinstance(content, str) and content:
        blocks.append(TextBlock(text=content))
    elif isinstance(content, list):
        # Some OpenAI-compat backends return content as a list of
        # ``{"type": "text", "text": "..."}`` chunks. Concatenate.
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, dict) and chunk.get("type") == "text":
                text = chunk.get("text")
                if isinstance(text, str):
                    parts.append(text)
        joined = "".join(parts)
        if joined:
            blocks.append(TextBlock(text=joined))

    tool_calls = msg.get("tool_calls") or []
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            func = call.get("function") or {}
            name = func.get("name") or "unknown"
            args_raw = func.get("arguments")
            args: dict
            if isinstance(args_raw, dict):
                args = args_raw
            elif isinstance(args_raw, str):
                try:
                    parsed = json.loads(args_raw)
                    args = parsed if isinstance(parsed, dict) else {"_value": parsed}
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": args_raw}
            else:
                args = {}
            blocks.append(
                ToolUseBlock(id=str(call.get("id") or ""), name=str(name), input=args)
            )

    return blocks


def alice_block_to_openai(block: Any) -> dict:
    """Convert an Alice block back to an OpenAI message dict.

    Used when constructing a follow-up request in a tool-use loop:
    - :class:`TextBlock`     → ``{"role": "assistant", "content": text}``
    - :class:`ToolUseBlock`  → ``{"role": "assistant", "content": None,
                                  "tool_calls": [...]}``
    - :class:`ThinkingBlock` → dropped (not sent back to Hermes; the
      OpenAI schema has no assistant-reasoning input slot).

    Tool results (Alice's ``ToolResultBlock`` shape from the Anthropic
    SDK) are represented on the OpenAI side as a role="tool" message
    with ``tool_call_id`` — construct those with :func:`tool_result_message`
    rather than this helper.
    """
    if isinstance(block, TextBlock):
        return {"role": "assistant", "content": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input, default=str),
                    },
                }
            ],
        }
    if isinstance(block, ThinkingBlock):
        # No reciprocal slot on the OpenAI wire — drop.
        return {}
    # Duck-type any other block shape by looking for common attrs.
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return {"role": "assistant", "content": text}
    return {}


def tool_result_message(tool_use_id: str, content: Any, *, is_error: bool = False) -> dict:
    """Construct the OpenAI-format tool-result message for a follow-up
    request. Caller-facing helper for future tool-loop support."""
    if isinstance(content, str):
        payload = content
    else:
        try:
            payload = json.dumps(content, default=str)
        except (TypeError, ValueError):
            payload = str(content)
    msg = {
        "role": "tool",
        "tool_call_id": tool_use_id,
        "content": payload,
    }
    if is_error:
        # OpenAI has no standard error flag on tool messages; convention
        # is to prefix the payload. Kept explicit so downstream code can
        # detect it without a re-parse.
        msg["content"] = f"[ERROR] {payload}"
    return msg


# --- Usage translation --------------------------------------------------------

def openai_usage_to_info(raw: Optional[dict]) -> Optional[UsageInfo]:
    """Convert OpenAI ``usage`` dict to :class:`UsageInfo`.

    OpenAI shape: ``{"prompt_tokens": int, "completion_tokens": int,
    "total_tokens": int}`` (some backends add
    ``prompt_tokens_details.cached_tokens`` for prompt-cache hits).
    We map ``prompt_tokens`` → ``input_tokens`` /
    ``completion_tokens`` → ``output_tokens`` so existing event-log
    aggregators (which key off the Anthropic-shaped names) keep working
    across backends.
    """
    if not raw or not isinstance(raw, dict):
        return None
    prompt = int(raw.get("prompt_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    total = raw.get("total_tokens")
    cache_read: Optional[int] = None
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if isinstance(cached, int):
            cache_read = cached
    return UsageInfo(
        input_tokens=prompt,
        output_tokens=completion,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=None,
        total_tokens=int(total) if isinstance(total, int) else None,
    )
