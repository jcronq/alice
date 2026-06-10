"""Build and push the "what is thinking thinking about" caption to the alice-face LCD.

Thinking owns the 16x2 caption sidecar on the alice-face ESP32 (the 20x4
face panel is Speaking's). The ESP32 firmware exposes ``POST /caption
{"text": "..."}`` and word-wraps the body into the 32-char (16×2) sidecar.
This module is the driver: it picks the latest thinking-wake note off
disk, asks a local model to summarize it, and pushes it.

Design choices, all small:

- Source: ``~/alice-mind/inner/thoughts/$(date +%Y-%m-%d)/`` — files are
  ``HHMMSS-wake.md`` (and longer-prefixed variants). Pick the latest by
  mtime; fall back to yesterday's directory if today's is empty.
- Summarizer: ``qwen-local`` via the alice-litellm proxy's
  OpenAI-compatible ``/v1/chat/completions`` endpoint. Defaults target
  the in-container proxy at ``http://alice-litellm:4000/v1`` with the
  master key ``sk-alice-local``; override via ``face_caption`` in
  ``alice.config.json`` (``base_url``, ``model``, ``api_key``). No
  OAuth — this closes the last Anthropic-direct call site outside
  Speaking + worker subagents (cost-audit thread, 2026-06-10).
- Cache: in-memory dict keyed by ``(path, mtime_ns)`` so a stable wake
  is summarized once per restart. The loop is a long-running process;
  the dict survives across iterations.
- Push: ``POST {ALICE_FACE_URL}/caption`` matching :mod:`face_presence`
  conventions. ``ALICE_FACE_URL`` default mirrors :data:`face_presence.DEFAULT_URL`
  (``http://10.20.30.205:8080``). Fallback URL ``http://10.20.30.171:8080``
  if the primary refuses the connection. 503 from the firmware means no
  sidecar is wired — caller logs and skips.

Every error path logs and returns. Never raise out of :func:`tick`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
from typing import Any, Optional

import httpx


log = logging.getLogger(__name__)


# LiteLLM proxy target. From inside the alice container the proxy is
# reachable as ``alice-litellm:4000``; from the host (or a sibling
# container without the alice-litellm DNS) point at the LAN IP via
# ``face_caption.base_url`` in alice.config.json.
DEFAULT_LITELLM_BASE_URL = "http://alice-litellm:4000/v1"
DEFAULT_LITELLM_MODEL = "qwen-local"
# Master key shared by every LiteLLM call site; matches
# ``LITELLM_MASTER_KEY`` env / sandbox compose default.
DEFAULT_LITELLM_API_KEY = "sk-alice-local"
LITELLM_MAX_TOKENS = 64

# Caption sidecar limit. The 16x2 LCD is 16 chars wide x 2 rows = 32
# chars total. The firmware word-wraps so the model can emit a single
# line — we just enforce the byte ceiling on output before sending.
MAX_CAPTION_CHARS = 32

# Body slice for the summarizer prompt. The wakes are usually < 500
# chars of meaningful prose; trimming bounds the input token cost.
MAX_BODY_CHARS = 500

# Network timeouts. The LiteLLM proxy proxies to a LAN Qwen runtime;
# 30s mirrors core.llm_client's :data:`DEFAULT_TIMEOUT_SECONDS`. The LCD
# push is fire-and-forget on a ~2s budget.
LITELLM_TIMEOUT_SECONDS = 30.0
FACE_TIMEOUT_SECONDS = 2.0

# Sampling for the summary call. Low temperature — this is a closed-form
# rewrite task, not generative.
LITELLM_TEMPERATURE = 0.4

# Default URLs. Primary is the ESP32 on the LAN; fallback is the pi
# proxy that used to relay through ``face_state``. Override with
# ``ALICE_FACE_URL`` (primary) — fallback stays hardcoded since it's a
# diagnostic last-ditch, not an operator knob.
DEFAULT_FACE_URL = "http://10.20.30.205:8080"
FALLBACK_FACE_URL = "http://10.20.30.171:8080"

# Where the thinking wakes drop their notes.
DEFAULT_THOUGHTS_ROOT = pathlib.Path.home() / "alice-mind" / "inner" / "thoughts"

# alice.config.json location (mirror of alice_thinking.wake._apply_config_overrides).
DEFAULT_CONFIG_PATH = (
    pathlib.Path.home() / "alice-mind" / "config" / "alice.config.json"
)

# System prompt — verbatim per spec. DO NOT "improve".
SUMMARIZER_SYSTEM_PROMPT = (
    "Summarize what Alice's thinking process is currently working on. "
    "Output ONLY the summary, no preamble, no quotes, no punctuation at "
    "the end. Max 32 characters. Plain English, like 'reviewing cozyhem' "
    "or 'idle; queue blocked'."
)


def _load_face_caption_config(
    path: pathlib.Path = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Read the ``face_caption`` section of ``alice.config.json``.

    Returns a dict with ``base_url`` / ``model`` / ``api_key`` keys,
    falling back to the module defaults when the file or section is
    absent or malformed. Mirrors the loader shape in
    :mod:`alice_thinking.wake._apply_config_overrides` — same
    "CLI/env > config file > module defaults" precedence is applied
    by callers that want it (here the defaults live in this module).
    """
    cfg: dict[str, Any] = {
        "base_url": DEFAULT_LITELLM_BASE_URL,
        "model": DEFAULT_LITELLM_MODEL,
        "api_key": DEFAULT_LITELLM_API_KEY,
    }
    if not path.is_file():
        return cfg
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("face_caption: cannot read %s: %s", path, exc)
        return cfg
    section = (parsed or {}).get("face_caption") or {}
    if not isinstance(section, dict):
        log.warning("face_caption: alice.config.json face_caption is not an object")
        return cfg
    for key in ("base_url", "model", "api_key"):
        value = section.get(key)
        if isinstance(value, str) and value:
            cfg[key] = value
    return cfg


def _latest_wake_path(
    thoughts_root: pathlib.Path, now: Optional[dt.datetime] = None
) -> Optional[pathlib.Path]:
    """Return the most recently modified wake file under today's (or
    yesterday's, as fallback) ``inner/thoughts/YYYY-MM-DD/`` directory.

    Returns ``None`` if neither day has any files.
    """
    today = (now or dt.datetime.now()).date()
    yesterday = today - dt.timedelta(days=1)
    for day in (today, yesterday):
        day_dir = thoughts_root / day.isoformat()
        if not day_dir.is_dir():
            continue
        try:
            entries = [p for p in day_dir.iterdir() if p.is_file()]
        except OSError as exc:
            log.warning("face_caption: cannot list %s: %s", day_dir, exc)
            continue
        if not entries:
            continue
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return entries[0]
    return None


def _strip_frontmatter(text: str) -> str:
    """Strip a leading YAML frontmatter block (``---\\n...\\n---\\n``) if present."""
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    if len(lines) < 2:
        return text
    # Find the closing ``---`` after line 0.
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "".join(lines[i + 1 :]).lstrip()
    # No closing marker — treat the whole thing as body.
    return text


def _truncate_caption(text: str) -> str:
    """Clamp ``text`` to :data:`MAX_CAPTION_CHARS`, trimming whitespace
    and any trailing punctuation we leak past the model's instructions.
    """
    cleaned = " ".join(text.split())  # collapse newlines / runs of spaces
    if len(cleaned) > MAX_CAPTION_CHARS:
        cleaned = cleaned[:MAX_CAPTION_CHARS].rstrip()
    return cleaned.rstrip(".,;:!?")


def _summarize_via_qwen(
    body: str,
    *,
    base_url: str = DEFAULT_LITELLM_BASE_URL,
    model: str = DEFAULT_LITELLM_MODEL,
    api_key: str = DEFAULT_LITELLM_API_KEY,
    client: Optional[httpx.Client] = None,
) -> Optional[str]:
    """Call the LiteLLM proxy's OpenAI-compatible ``/chat/completions``
    endpoint and return the trimmed summary, or ``None`` on failure.

    Same prompt shape as the prior Haiku call site: a ``system`` message
    pinning the output format and a ``user`` message carrying the wake
    body (truncated to :data:`MAX_BODY_CHARS`). Qwen3 builds are
    reasoning models; ``chat_template_kwargs.enable_thinking=false``
    keeps the assistant emitting prose in ``content`` rather than
    burning tokens on ``reasoning_content`` — same fix
    :class:`core.llm_client.LLMClient` uses. LiteLLM's
    ``drop_params: true`` strips this safely if a backend ignores it.
    """
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "max_tokens": LITELLM_MAX_TOKENS,
        "temperature": LITELLM_TEMPERATURE,
        "stream": False,
        "messages": [
            {"role": "system", "content": SUMMARIZER_SYSTEM_PROMPT},
            {"role": "user", "content": body[:MAX_BODY_CHARS]},
        ],
        "chat_template_kwargs": {"enable_thinking": False},
    }
    url = base_url.rstrip("/") + "/chat/completions"
    owns_client = client is None
    try:
        client = client or httpx.Client(timeout=LITELLM_TIMEOUT_SECONDS)
        try:
            resp = client.post(url, json=payload, headers=headers)
        finally:
            if owns_client:
                client.close()
    except httpx.HTTPError as exc:
        log.warning("face_caption: LiteLLM POST failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning(
            "face_caption: LiteLLM returned %d: %s",
            resp.status_code,
            resp.text[:200],
        )
        return None
    try:
        blob = resp.json()
        content = blob["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("face_caption: malformed LiteLLM response: %s", exc)
        return None
    if not isinstance(content, str):
        log.warning("face_caption: LiteLLM content is not a string")
        return None
    raw = content.strip()
    if not raw:
        log.warning("face_caption: LiteLLM returned empty content")
        return None
    return _truncate_caption(raw)


def summarize(
    body: str,
    *,
    config_path: pathlib.Path = DEFAULT_CONFIG_PATH,
    client: Optional[httpx.Client] = None,
) -> Optional[str]:
    """Public single-shot helper: read ``alice.config.json`` for the
    LiteLLM endpoint and summarize ``body`` into a sidecar caption.

    Returns the trimmed caption string, or ``None`` on any failure
    (unreachable proxy, malformed response, empty content). Convenience
    for ad-hoc CLI smoke tests and the in-loop
    :class:`FaceCaptionDriver`; the loop itself uses
    :func:`_summarize_via_qwen` directly so it can reuse a client.
    """
    cfg = _load_face_caption_config(config_path)
    return _summarize_via_qwen(
        body,
        base_url=cfg["base_url"],
        model=cfg["model"],
        api_key=cfg["api_key"],
        client=client,
    )


def _push_caption(
    caption: str,
    *,
    primary_url: str,
    fallback_url: Optional[str],
    client: Optional[httpx.Client] = None,
) -> bool:
    """POST ``{"text": caption}`` to ``/caption``. Returns True on 2xx."""
    body = {"text": caption}
    owns_client = client is None
    client = client or httpx.Client(timeout=FACE_TIMEOUT_SECONDS)
    try:
        for url in (primary_url, fallback_url):
            if not url:
                continue
            try:
                resp = client.post(f"{url.rstrip('/')}/caption", json=body)
            except (httpx.HTTPError, OSError) as exc:
                log.info("face_caption: %s unreachable: %s", url, exc)
                continue
            if 200 <= resp.status_code < 300:
                log.info("face_caption: pushed %r to %s", caption, url)
                return True
            log.info(
                "face_caption: %s returned %d: %s",
                url,
                resp.status_code,
                resp.text[:120],
            )
        return False
    finally:
        if owns_client:
            client.close()


class FaceCaptionDriver:
    """Long-running driver: cache wakes by (path, mtime), summarize, push.

    Constructed once; ``tick()`` runs one loop iteration. The cache is a
    plain dict — keys are ``(str(path), mtime_ns)`` so a re-written wake
    (different mtime) gets re-summarized.
    """

    def __init__(
        self,
        *,
        thoughts_root: Optional[pathlib.Path] = None,
        face_url: Optional[str] = None,
        fallback_url: Optional[str] = FALLBACK_FACE_URL,
        config_path: Optional[pathlib.Path] = None,
    ) -> None:
        self._thoughts_root = thoughts_root or DEFAULT_THOUGHTS_ROOT
        self._face_url = (
            face_url
            if face_url is not None
            else os.environ.get("ALICE_FACE_URL", DEFAULT_FACE_URL)
        )
        self._fallback_url = fallback_url
        self._config_path = config_path or DEFAULT_CONFIG_PATH
        self._cache: dict[tuple[str, int], str] = {}
        self._last_pushed: Optional[str] = None

    def _resolve_caption(self) -> Optional[str]:
        path = _latest_wake_path(self._thoughts_root)
        if path is None:
            log.info("face_caption: no wake notes found under %s", self._thoughts_root)
            return None
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError as exc:
            log.warning("face_caption: stat(%s) failed: %s", path, exc)
            return None
        key = (str(path), mtime_ns)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("face_caption: read(%s) failed: %s", path, exc)
            return None
        body = _strip_frontmatter(raw).strip()
        if not body:
            log.info("face_caption: %s has empty body after frontmatter", path)
            return None
        summary = summarize(body, config_path=self._config_path)
        if not summary:
            return None
        self._cache[key] = summary
        return summary

    def tick(self) -> Optional[str]:
        """Run one iteration. Returns the caption that was pushed, or
        ``None`` if nothing was pushed (no wake, no token, push failed,
        or the caption hasn't changed since the last push).

        Coalesces duplicate consecutive captions so we don't spam the
        face — matches the :mod:`face_presence` policy.
        """
        try:
            caption = self._resolve_caption()
        except Exception as exc:  # noqa: BLE001
            log.warning("face_caption: resolve failed: %s", exc, exc_info=True)
            return None
        if not caption:
            return None
        if caption == self._last_pushed:
            return None
        if not self._face_url:
            log.info("face_caption: ALICE_FACE_URL empty; skip push")
            return None
        ok = _push_caption(
            caption,
            primary_url=self._face_url,
            fallback_url=self._fallback_url,
        )
        if not ok:
            return None
        self._last_pushed = caption
        return caption


__all__ = [
    "DEFAULT_FACE_URL",
    "DEFAULT_LITELLM_BASE_URL",
    "DEFAULT_LITELLM_MODEL",
    "FALLBACK_FACE_URL",
    "MAX_CAPTION_CHARS",
    "SUMMARIZER_SYSTEM_PROMPT",
    "FaceCaptionDriver",
    "_latest_wake_path",
    "_strip_frontmatter",
    "_summarize_via_qwen",
    "_truncate_caption",
    "summarize",
]
