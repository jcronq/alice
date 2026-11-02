"""Post-flight cost actuals — scan a worker scratch dir for token usage.

The ``claude -p --output-format json`` wrapper the dispatcher spawns
emits a Claude Agent SDK result envelope on stdout (written to
``result.json`` in the worker's scratch dir). We first try to pull
``usage`` out of that envelope; if the envelope is empty / malformed /
missing (worker crashed before emission, log-only invocation) we fall
back to a regex scan of ``stderr.log`` for ``Tokens used: input=X
output=Y`` style lines.

The reader returns ``None`` instead of guessing: a missing token count
is a *known unknown*, not an invitation to make up a dollar figure.
The dispatcher writes ``actual_usd: null`` with a human-readable
``cost_attribution_note`` when that happens, so Jason can see which
bugs lacked instrumentation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from .rates import rates_for


# Fallback regex for workers that didn't emit tidy JSON. Case-
# insensitive; tolerates ``=`` or ``:`` as the separator and either
# ``input/output`` or ``in/out`` abbreviations. The pricing module
# uses the first match for each key — order doesn't matter.
_TOKEN_LINE_RE = re.compile(
    r"(?i)\btokens?\s+used\b[^0-9]*"
    r"(?=.*\b(?:input|in)\s*[=:]\s*(?P<input>\d+))"
    r"(?=.*\b(?:output|out)\s*[=:]\s*(?P<output>\d+))"
)

# Looser fallback: separate ``input_tokens: NNN`` and
# ``output_tokens: NNN`` lines anywhere in the log (SDK verbose mode).
_LOOSE_INPUT_RE = re.compile(r"(?i)\binput_tokens[^0-9]{1,8}(\d+)")
_LOOSE_OUTPUT_RE = re.compile(r"(?i)\boutput_tokens[^0-9]{1,8}(\d+)")
_LOOSE_CACHE_READ_RE = re.compile(r"(?i)\bcache_read_input_tokens[^0-9]{1,8}(\d+)")
_LOOSE_CACHE_WRITE_RE = re.compile(
    r"(?i)\bcache_(?:creation|write)_input_tokens[^0-9]{1,8}(\d+)"
)
_LOOSE_MODEL_RE = re.compile(r"(?i)\bmodel['\"\s:=]{1,8}((?:anthropic/)?claude[-\w.]+)")


def _descend_for_usage(obj: object) -> dict | None:
    """Depth-first search for a ``usage`` dict inside the SDK envelope.

    The SDK wraps its real payload under ``result`` (sometimes as a
    stringified JSON blob). We descend through both dicts and lists
    and return the first dict that looks like a usage block (has
    ``input_tokens``).
    """
    if isinstance(obj, dict):
        if "input_tokens" in obj and isinstance(obj.get("input_tokens"), int):
            return obj
        inner = obj.get("usage")
        if isinstance(inner, dict) and "input_tokens" in inner:
            return inner
        result_field = obj.get("result")
        if isinstance(result_field, str):
            try:
                parsed = json.loads(result_field)
            except ValueError:
                parsed = None
            if parsed is not None:
                found = _descend_for_usage(parsed)
                if found is not None:
                    return found
        for value in obj.values():
            found = _descend_for_usage(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _descend_for_usage(item)
            if found is not None:
                return found
    return None


def _descend_for_model(obj: object) -> str:
    """Pull the model id out of the SDK envelope (envelope-level, not aliases)."""
    if isinstance(obj, dict):
        model = obj.get("model")
        if isinstance(model, str) and model:
            return model
        for value in obj.values():
            found = _descend_for_model(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _descend_for_model(item)
            if found:
                return found
    return ""


def _parse_result_json(path: Path) -> dict | None:
    """Return ``{input_tokens, output_tokens, model, cache_read, cache_write}`` or None.

    None means the file isn't a parseable envelope OR has no usage
    section. The caller then tries the stderr fallback.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    usage = _descend_for_usage(data)
    if usage is None:
        return None
    try:
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
    except (TypeError, ValueError):
        return None
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(
        usage.get("cache_creation_input_tokens")
        or usage.get("cache_write_input_tokens")
        or 0
    )
    model = _descend_for_model(data)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "model": model,
    }


def _parse_stderr_fallback(path: Path) -> dict | None:
    """Scan the stderr log for token counters. Returns dict or None."""
    if not path.is_file():
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    if not text:
        return None

    # Preferred form: a single ``Tokens used: input=X output=Y`` line.
    for match in _TOKEN_LINE_RE.finditer(text):
        try:
            input_tokens = int(match.group("input"))
            output_tokens = int(match.group("output"))
        except (TypeError, ValueError):
            continue
        model_match = _LOOSE_MODEL_RE.search(text)
        model = model_match.group(1) if model_match else ""
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "model": model,
        }

    # Looser form: separate ``input_tokens: N`` / ``output_tokens: N``
    # lines (SDK verbose mode / stream-events dumps).
    input_match = _LOOSE_INPUT_RE.search(text)
    output_match = _LOOSE_OUTPUT_RE.search(text)
    if input_match and output_match:
        try:
            input_tokens = int(input_match.group(1))
            output_tokens = int(output_match.group(1))
        except (TypeError, ValueError):
            return None
        cache_read_match = _LOOSE_CACHE_READ_RE.search(text)
        cache_write_match = _LOOSE_CACHE_WRITE_RE.search(text)
        cache_read = int(cache_read_match.group(1)) if cache_read_match else 0
        cache_write = int(cache_write_match.group(1)) if cache_write_match else 0
        model_match = _LOOSE_MODEL_RE.search(text)
        model = model_match.group(1) if model_match else ""
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "model": model,
        }

    return None


def read_worker_tokens(scratch_dir: Path) -> dict | None:
    """Return the worker's actual token counts, or None if unreadable.

    Checks ``result.json`` first (SDK envelope), falls back to
    ``stderr.log`` regex. The returned dict always carries five keys:
    ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
    ``cache_write_tokens``, ``model``. ``model`` may be the empty
    string if neither surface named a model — callers price with the
    model they *dispatched* in that case.
    """
    result_path = scratch_dir / "result.json"
    structured = _parse_result_json(result_path)
    if structured is not None:
        return structured
    stderr_path = scratch_dir / "stderr.log"
    return _parse_stderr_fallback(stderr_path)


def actual_cost_from_tokens(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    *,
    rates: Mapping[str, float] | None = None,
) -> float:
    """Dollars for a measured token-usage row.

    ``input_tokens`` is the *regular* input (non-cached). The SDK's
    ``usage.input_tokens`` field is already net of cache hits, so you
    pass that value here along with ``cache_read_tokens`` and
    ``cache_write_tokens`` from the same block without
    double-counting.
    """
    rate_dict = rates if rates is not None else rates_for(model)
    pt = max(0, int(input_tokens))
    ot = max(0, int(output_tokens))
    cr = max(0, int(cache_read_tokens))
    cw = max(0, int(cache_write_tokens))
    return (
        pt / 1_000_000 * rate_dict["input_per_mtok"]
        + ot / 1_000_000 * rate_dict["output_per_mtok"]
        + cr / 1_000_000 * rate_dict["cache_read_per_mtok"]
        + cw / 1_000_000 * rate_dict["cache_write_per_mtok"]
    )
