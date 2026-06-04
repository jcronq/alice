"""Build and push the "what is thinking thinking about" caption to the alice-face LCD.

The face's 20x4 HD44780 has a 12x4 status panel (cols 8-19, rows 0-3, ~48
chars). The ESP32 firmware exposes ``POST /status {"text": "..."}`` and
word-wraps the body into the panel. This module is the driver: it picks
the latest thinking-wake note off disk, asks Haiku 4.5 for a ≤48 char
summary, and pushes it to the face.

Design choices, all small:

- Source: ``~/alice-mind/inner/thoughts/$(date +%Y-%m-%d)/`` — files are
  ``HHMMSS-wake.md`` (and longer-prefixed variants). Pick the latest by
  mtime; fall back to yesterday's directory if today's is empty.
- Summarizer: Haiku 4.5 (``claude-haiku-4-5-20251001``) via the
  Anthropic ``/v1/messages`` endpoint, authed with the long-lived OAuth
  token at ``~/.claude/.credentials.json``. Same wire pattern as
  :mod:`eval.replay`.
- Cache: in-memory dict keyed by ``(path, mtime_ns)`` so a stable wake
  is summarized once per restart. The loop is a long-running process;
  the dict survives across iterations.
- Push: ``POST {ALICE_FACE_URL}/status`` matching :mod:`face_presence`
  conventions. ``ALICE_FACE_URL`` default mirrors :data:`face_presence.DEFAULT_URL`
  (``http://10.20.30.205:8080``). Fallback URL ``http://10.20.30.171:8080``
  if the primary refuses the connection.

Every error path logs and returns. Never raise out of :func:`tick`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
from typing import Optional

import httpx


log = logging.getLogger(__name__)


# Anthropic API target — same pattern as eval.replay._call_anthropic.
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_MAX_TOKENS = 64

# Face panel limit. The status panel is 12 chars wide x 4 rows = 48
# chars total. The firmware word-wraps so the model can emit a single
# line — we just enforce the byte ceiling on output before sending.
MAX_CAPTION_CHARS = 48

# Body slice for the summarizer prompt. The wakes are usually < 500
# chars of meaningful prose; trimming bounds the input token cost.
MAX_BODY_CHARS = 500

# Network timeouts. Anthropic is generous (cap at 15s — Haiku is fast),
# the LCD push is fire-and-forget on a 1s budget.
ANTHROPIC_TIMEOUT_SECONDS = 15.0
FACE_TIMEOUT_SECONDS = 2.0

# Default URLs. Primary is the ESP32 on the LAN; fallback is the pi
# proxy that used to relay through ``face_state``. Override with
# ``ALICE_FACE_URL`` (primary) — fallback stays hardcoded since it's a
# diagnostic last-ditch, not an operator knob.
DEFAULT_FACE_URL = "http://10.20.30.205:8080"
FALLBACK_FACE_URL = "http://10.20.30.171:8080"

# OAuth credentials live at the standard Claude Code path. The token
# is an ``sk-ant-oat*`` Bearer; the API also accepts ``sk-ant-api*``
# keys via ``x-api-key``, but the speaking container is configured for
# subscription mode so we always have the OAuth flavour.
DEFAULT_CREDENTIALS_PATH = pathlib.Path.home() / ".claude" / ".credentials.json"

# Where the thinking wakes drop their notes.
DEFAULT_THOUGHTS_ROOT = pathlib.Path.home() / "alice-mind" / "inner" / "thoughts"

# System prompt — verbatim per spec. DO NOT "improve".
SUMMARIZER_SYSTEM_PROMPT = (
    "Summarize what Alice's thinking process is currently working on. "
    "Output ONLY the summary, no preamble, no quotes, no punctuation at "
    "the end. Max 48 characters. Plain English, like 'reviewing cozyhem "
    "bugs' or 'idle; queue blocked on Jason'."
)


def _read_oauth_token(path: pathlib.Path) -> Optional[str]:
    """Return the OAuth access token.

    Prefer the ``CLAUDE_CODE_OAUTH_TOKEN`` env var when set — that's what
    the s6 ``with-contenv`` shell exports from ``alice.env``, and it
    tracks the speaking daemon's auto-refreshed token. Fall back to
    reading ``credentials.json`` directly, which is fine for ad-hoc
    invocations from a developer shell but can lag the live token by
    hours when the daemon refreshes in-memory without rewriting the
    file. Returns ``None`` if neither source yields a usable token.
    """
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("face_caption: cannot read credentials at %s: %s", path, exc)
        return None
    token = (data.get("claudeAiOauth") or {}).get("accessToken")
    if not isinstance(token, str) or not token:
        log.warning("face_caption: credentials.json missing claudeAiOauth.accessToken")
        return None
    return token


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


def _summarize_via_haiku(
    body: str,
    *,
    oauth_token: str,
    client: Optional[httpx.Client] = None,
) -> Optional[str]:
    """Call Anthropic's ``/v1/messages`` with Haiku 4.5 and return the
    trimmed summary, or ``None`` on any failure.
    """
    headers = {
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
        "authorization": f"Bearer {oauth_token}",
        "anthropic-beta": "oauth-2025-04-20",
    }
    payload = {
        "model": HAIKU_MODEL,
        "max_tokens": HAIKU_MAX_TOKENS,
        "system": SUMMARIZER_SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": body[:MAX_BODY_CHARS]},
        ],
    }
    url = ANTHROPIC_BASE_URL + "/v1/messages"
    owns_client = client is None
    try:
        client = client or httpx.Client(timeout=ANTHROPIC_TIMEOUT_SECONDS)
        try:
            resp = client.post(url, json=payload, headers=headers)
        finally:
            if owns_client:
                client.close()
    except httpx.HTTPError as exc:
        log.warning("face_caption: Anthropic POST failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning(
            "face_caption: Anthropic returned %d: %s",
            resp.status_code,
            resp.text[:200],
        )
        return None
    try:
        blob = resp.json()
        chunks = blob.get("content") or []
        text_pieces = [
            c.get("text", "")
            for c in chunks
            if isinstance(c, dict) and c.get("type") == "text"
        ]
    except (ValueError, AttributeError, TypeError) as exc:
        log.warning("face_caption: malformed Anthropic response: %s", exc)
        return None
    raw = "".join(text_pieces).strip()
    if not raw:
        log.warning("face_caption: Anthropic returned empty content")
        return None
    return _truncate_caption(raw)


def _push_status(
    caption: str,
    *,
    primary_url: str,
    fallback_url: Optional[str],
    client: Optional[httpx.Client] = None,
) -> bool:
    """POST ``{"text": caption}`` to ``/status``. Returns True on 2xx."""
    body = {"text": caption}
    owns_client = client is None
    client = client or httpx.Client(timeout=FACE_TIMEOUT_SECONDS)
    try:
        for url in (primary_url, fallback_url):
            if not url:
                continue
            try:
                resp = client.post(f"{url.rstrip('/')}/status", json=body)
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
        credentials_path: Optional[pathlib.Path] = None,
    ) -> None:
        self._thoughts_root = thoughts_root or DEFAULT_THOUGHTS_ROOT
        self._face_url = (
            face_url
            if face_url is not None
            else os.environ.get("ALICE_FACE_URL", DEFAULT_FACE_URL)
        )
        self._fallback_url = fallback_url
        self._credentials_path = credentials_path or DEFAULT_CREDENTIALS_PATH
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
        token = _read_oauth_token(self._credentials_path)
        if not token:
            return None
        summary = _summarize_via_haiku(body, oauth_token=token)
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
        ok = _push_status(
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
    "FALLBACK_FACE_URL",
    "HAIKU_MODEL",
    "MAX_CAPTION_CHARS",
    "SUMMARIZER_SYSTEM_PROMPT",
    "FaceCaptionDriver",
    "_latest_wake_path",
    "_strip_frontmatter",
    "_truncate_caption",
]
