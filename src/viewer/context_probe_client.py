"""Viewer-side glue for the speaking daemon's context-snapshot RPC.

The viewer container shares ``/state`` with the worker (both bind-mount
the host's ``~/.local/state/alice``), so the worker's CLI socket lives
at ``/state/alice.sock`` from inside the viewer. We connect there
directly and speak the same wire protocol the worker accepts — no
``bin/alice``, no docker-exec hop. Decomposes the resulting snapshot
into a structure the ``/context`` template can render, with tiktoken
proxy-token weights per component for the donut.

Why tiktoken instead of the official anthropic ``count_tokens``: the
viewer doesn't (and shouldn't) ship the anthropic SDK, and the donut
only needs proportional accuracy. The cl100k_base encoder is close
enough for "look, this MCP server's tool definitions are eating ~30%
of the context" — and it's offline.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import pathlib
from typing import Any, Optional


log = logging.getLogger(__name__)


SNAPSHOT_TIMEOUT_SECONDS = 15.0
DEFAULT_SOCKET_PATH = "/state/alice.sock"


@functools.lru_cache(maxsize=1)
def _encoder():
    """Lazy + cached tiktoken encoder. cl100k_base is the GPT-4-class
    encoder — not Claude's, but close enough for the proportional
    breakdown the donut uses. Pre-cached so per-request cost is just
    encode() calls."""
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def _tokens(text: Optional[str]) -> int:
    """Approximate token count for ``text``. Falls back to a chars/4
    heuristic if tiktoken errors out (unlikely — the encoder is
    self-contained)."""
    if not text:
        return 0
    try:
        return len(_encoder().encode(text))
    except Exception:  # noqa: BLE001
        log.exception("tiktoken encode failed; falling back to chars/4")
        return max(1, len(text) // 4)


def _resolve_socket_path(socket_path: Optional[str]) -> str:
    return (
        socket_path
        or os.environ.get("ALICE_CLI_SOCKET")
        or DEFAULT_SOCKET_PATH
    )


async def fetch_snapshot(
    *,
    socket_path: Optional[str] = None,
    timeout: float = SNAPSHOT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Connect to the worker's CLI socket and request a context snapshot.

    Speaks the line-delimited JSON protocol documented in
    :mod:`alice_speaking.transports.cli`: send
    ``{"type": "context"}\\n``, drain events until ``done`` or
    ``error``, return the ``context_snapshot`` payload.

    Raises:
        FileNotFoundError: the socket doesn't exist (worker not up).
        TimeoutError: didn't receive a terminal event in ``timeout`` s.
        RuntimeError: the daemon replied with ``error`` (e.g. probe
            unwired) or closed the connection without ``done``.
    """
    path = _resolve_socket_path(socket_path)
    if not pathlib.Path(path).exists():
        raise FileNotFoundError(
            f"alice CLI socket not found at {path} — is the worker up?"
        )
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path), timeout=timeout
        )
    except (PermissionError, ConnectionRefusedError, OSError) as exc:
        raise RuntimeError(
            f"could not connect to alice socket at {path}: {exc}"
        ) from exc

    try:
        request = json.dumps({"type": "context"}) + "\n"
        writer.write(request.encode("utf-8"))
        await writer.drain()
        return await asyncio.wait_for(
            _drain_until_snapshot(reader), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"alice context RPC did not complete in {timeout}s"
        ) from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _drain_until_snapshot(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Pump JSON events off the socket until ``done`` or ``error``.

    Returns the ``context_snapshot`` payload. Raises ``RuntimeError`` if
    the stream closes early or the daemon emits ``error`` / produces no
    snapshot.
    """
    snapshot: Optional[dict] = None
    error_msg: Optional[str] = None
    while True:
        line = await reader.readline()
        if not line:
            raise RuntimeError(
                "alice closed the socket before a context_snapshot arrived"
            )
        try:
            event = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        etype = event.get("type")
        if etype == "context_snapshot":
            snapshot = event.get("data")
        elif etype == "error":
            error_msg = event.get("message") or "unspecified"
        elif etype == "done":
            break
    if error_msg is not None:
        raise RuntimeError(f"alice replied error: {error_msg}")
    if snapshot is None:
        raise RuntimeError("no context_snapshot event arrived before done")
    return snapshot


def decompose(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Bucket a snapshot into the categories the /context donut renders.

    Each component carries ``tokens`` (approximate) and ``label`` so the
    template doesn't need to know about the snapshot's internal shape.
    Returns:
        {
            "components": [{name, tokens, detail}, ...],
            "total_tokens": int,
            "snapshot": {raw snapshot for the data table},
        }
    """
    sp = snapshot.get("system_prompt") or {}
    sp_tokens = _tokens(sp.get("text")) if sp.get("text") is not None else (
        _approx_tokens_from_chars(sp.get("chars", 0))
    )

    preamble = snapshot.get("pending_preamble") or {}
    preamble_tokens = (
        _tokens(preamble.get("text"))
        if preamble.get("text") is not None
        else _approx_tokens_from_chars(preamble.get("chars", 0))
    )

    tools = snapshot.get("tools") or {}
    builtin = tools.get("builtin") or []
    custom = tools.get("custom") or []
    # Tool *names* are tiny — most of the cost is the JSON Schema for
    # each tool's input. We don't have that here (the daemon doesn't
    # ship schemas through the probe), so this number underweights
    # tools relative to reality. Honest about it in the template
    # ("name-only estimate"). Each name takes ~3-5 tokens.
    builtin_tokens = sum(_tokens(name) for name in builtin)
    custom_tokens = sum(_tokens(name) for name in custom)

    mcp = snapshot.get("mcp_servers") or {}
    mcp_components: list[dict[str, Any]] = []
    mcp_total = 0
    for name in sorted(mcp.keys()):
        spec = mcp[name] or {}
        defs = spec.get("tools") or []
        if defs:
            # Real definitions wired through — count name + description
            # + serialized input_schema. This is much closer to the
            # actual on-the-wire tool size the model sees.
            toks = _tokens(name)
            for tdef in defs:
                toks += _tokens(tdef.get("name") or "")
                toks += _tokens(tdef.get("description") or "")
                schema = tdef.get("input_schema")
                if schema:
                    import json as _json

                    toks += _tokens(_json.dumps(schema, separators=(",", ":")))
            detail_extra = "with schemas"
        else:
            # Fallback: name-only estimate (small, undercount).
            names = spec.get("tool_names") or []
            toks = _tokens(name) + sum(_tokens(n) for n in names)
            detail_extra = "name-only estimate"
        mcp_total += toks
        mcp_components.append(
            {
                "name": f"mcp:{name}",
                "tokens": toks,
                "detail": (
                    f"{spec.get('tool_count', 0)} tools "
                    f"({spec.get('type', '?')}, {detail_extra})"
                ),
            }
        )

    components: list[dict[str, Any]] = [
        {
            "name": "system prompt",
            "tokens": sp_tokens,
            "detail": f"{sp.get('chars', 0)} chars",
        },
        {
            "name": "builtin tools",
            "tokens": builtin_tokens,
            "detail": f"{len(builtin)} tools (name-only estimate)",
        },
        {
            "name": "custom tools",
            "tokens": custom_tokens,
            "detail": f"{len(custom)} tools (name-only estimate)",
        },
        *mcp_components,
    ]
    if preamble_tokens:
        components.append(
            {
                "name": "pending preamble",
                "tokens": preamble_tokens,
                "detail": f"{preamble.get('chars', 0)} chars",
            }
        )
    components = [c for c in components if c["tokens"] > 0]
    total = sum(c["tokens"] for c in components)
    return {
        "components": components,
        "total_tokens": total,
        "snapshot": snapshot,
    }


def _approx_tokens_from_chars(chars: int) -> int:
    """Fallback when the snapshot was fetched with --no-text. Rough
    chars-per-token of ~4 for English-ish text."""
    return max(0, chars // 4)


__all__ = [
    "fetch_snapshot",
    "decompose",
    "SNAPSHOT_TIMEOUT_SECONDS",
    "DEFAULT_SOCKET_PATH",
]
