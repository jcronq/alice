"""Stage the pi-coding-agent ``models.json`` model registry.

pi-coding-agent reads its provider/model registry from
``~/.pi/agent/models.json`` (or ``$PI_AGENT_DIR/models.json``).
Alice's source-of-truth registry lives in the vault at
``~/alice-mind/config/pi-models.json`` — that's where the
``litellm`` provider (pointing pi at the LiteLLM proxy that fronts
the local Qwen runtimes) is declared.

This module is the alice-pi-managed staging step that copies
vault → pi runtime location at the start of every PiKernel run.
Previously this lived in the worker container's entrypoint.sh —
wrong layer, since it's pi-backend-specific config. The codex→pi
*auth* bridge stays in entrypoint.sh; that's a different concern
(secret material, not registry config).

Fail-soft contract: missing source = no-op, malformed/unwritable
= warning event + no-op. Pi can always fall back to its built-in
providers if the staging step does nothing.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any, Callable, Optional


__all__ = ["ensure_pi_models_json"]


_DEFAULT_SOURCE = pathlib.Path("/home/alice/alice-mind/config/pi-models.json")


def _resolve_source(explicit: Optional[pathlib.Path]) -> pathlib.Path:
    if explicit is not None:
        return explicit
    override = os.environ.get("ALICE_PI_MODELS_JSON")
    if override:
        return pathlib.Path(override)
    return _DEFAULT_SOURCE


def _resolve_dest(explicit: Optional[pathlib.Path]) -> pathlib.Path:
    if explicit is not None:
        return explicit
    pi_agent_dir = os.environ.get("PI_AGENT_DIR")
    if pi_agent_dir:
        return pathlib.Path(pi_agent_dir) / "models.json"
    return pathlib.Path.home() / ".pi" / "agent" / "models.json"


def _files_match(src: pathlib.Path, dst: pathlib.Path) -> bool:
    """Cheap equality check: stat first, full byte compare only if
    size matches. Avoids reading the file twice when the answer is
    obviously 'different size, can't match'."""
    try:
        src_stat = src.stat()
        dst_stat = dst.stat()
    except OSError:
        return False
    if src_stat.st_size != dst_stat.st_size:
        return False
    try:
        return src.read_bytes() == dst.read_bytes()
    except OSError:
        return False


def ensure_pi_models_json(
    *,
    source: Optional[pathlib.Path] = None,
    dest: Optional[pathlib.Path] = None,
    emit: Optional[Callable[..., Any]] = None,
) -> bool:
    """Stage the pi model registry from ``source`` to ``dest``.

    Returns ``True`` if a write happened, ``False`` if no-op (source
    absent, destination already matches, or a fail-soft error path).
    Never raises — registry staging must not break a wake.

    Parameters
    ----------
    source:
        Path to read from. Defaults to ``$ALICE_PI_MODELS_JSON`` if
        set, otherwise ``/home/alice/alice-mind/config/pi-models.json``.
    dest:
        Path to write to. Defaults to ``$PI_AGENT_DIR/models.json``
        if set, otherwise ``~/.pi/agent/models.json``.
    emit:
        Optional event emitter callable. Receives
        ``("pi_models_stage_failed", reason=...)`` on fail-soft errors.
        ``None`` is silent.
    """
    src = _resolve_source(source)
    dst = _resolve_dest(dest)

    if not src.exists():
        # Source absent is the documented "no managed registry"
        # case — pi falls back to its built-in providers.
        return False

    try:
        raw = src.read_bytes()
    except OSError as exc:
        _safe_emit(emit, reason=f"source_unreadable: {exc}", source=str(src))
        return False

    # Validate JSON shape — alice-pi doesn't validate the schema
    # (pi is the schema validator), but we refuse to stage a file
    # that isn't even parseable as JSON; that's almost certainly a
    # bug worth surfacing.
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        _safe_emit(emit, reason=f"source_malformed_json: {exc}", source=str(src))
        return False

    if dst.exists() and _files_match(src, dst):
        return False

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
    except OSError as exc:
        _safe_emit(emit, reason=f"write_failed: {exc}", dest=str(dst))
        return False

    return True


def _safe_emit(emit: Optional[Callable[..., Any]], **fields: Any) -> None:
    """Call ``emit`` if provided, swallowing any emitter failure.
    Observability must not break the staging contract."""
    if emit is None:
        return
    try:
        emit("pi_models_stage_failed", **fields)
    except Exception:  # noqa: BLE001 - emitter must never break us
        pass
