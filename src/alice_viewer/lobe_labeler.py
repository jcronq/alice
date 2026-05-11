"""LLM-derived display labels for memory-graph lobes.

The mechanical ``cl-<top-hub>`` slug minted by :mod:`cluster_registry`
gives a stable identity but is often a poor *display* name — the top
hub of a fitness lobe might be a dated deload-week note, leaving the
operator squinting at ``cl-2026-q1-deload`` to figure out what's
actually inside.

This module synthesises a short human-readable name by feeding the
local Qwen endpoint each lobe's member titles plus the first ~200
characters of body text per note. The output is a 2-6 word phrase
like "fitness deload cycles" or "stage-d dual-judge wireup".

Network seam: :func:`call_qwen_async` is the only place we touch the
LAN endpoint. Tests pass a stub via the ``llm_call`` kwarg so they
never hit the wire.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from typing import Awaitable, Callable, Optional


# Hard cap on what we hand the UI. Long labels would force bubble-text
# wrapping/truncation and defeat the readability win.
MAX_LABEL_CHARS = 60

# Cap on how many member notes we feed the LLM per lobe. Above this the
# prompt grows past the point where adding more notes meaningfully
# changes the label, and Qwen latency starts to hurt.
MAX_MEMBERS_FED = 50

# Default snippet length per member. Long enough to capture the opening
# sentence after frontmatter; short enough that 50 members fit
# comfortably in the prompt.
DEFAULT_SNIPPET_CHARS = 200

# YAML frontmatter delimiter. We strip the front block before sampling
# the body so titles/tags don't dominate the snippet.
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)

# Markdown leading whitespace, headings, and wikilink brackets just add
# noise to the snippet — collapse them to plain text.
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:[|#][^\]]*)?\]\]")


# ---------------------------------------------------------------------------
# Snippet extraction
# ---------------------------------------------------------------------------


def extract_first_chunk(
    path: pathlib.Path | str,
    max_chars: int = DEFAULT_SNIPPET_CHARS,
) -> str:
    """Read ``path`` and return the first ``max_chars`` of body text.

    Strips YAML frontmatter, markdown heading sigils, and wikilink
    brackets so the LLM sees a clean prose excerpt rather than a wall
    of ``[[link]]`` tokens.

    Returns ``""`` if the file is missing or unreadable — callers
    should treat that as "snippet unavailable" rather than an error.
    """
    p = pathlib.Path(path)
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    body = _FRONTMATTER_RE.sub("", raw, count=1)
    body = _HEADING_RE.sub("", body)
    body = _WIKILINK_RE.sub(r"\1", body)
    body = body.strip()
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars]
    # Try to break at a word boundary so the snippet doesn't end mid-word.
    space = cut.rfind(" ")
    if space >= max_chars - 30:
        cut = cut[:space]
    return cut.rstrip() + "…"


# ---------------------------------------------------------------------------
# Prompt construction + output sanitisation
# ---------------------------------------------------------------------------


_PROMPT_TEMPLATE = """\
You are labeling a cluster of related notes from a personal knowledge \
base. Each note has a title and a short excerpt. Read them and produce \
a 2-6 word lowercase phrase that captures what the cluster is *about* \
— the shared topic, project, or theme. Prefer concrete domain language \
over generic words like "notes", "research", or "ideas".

Output rules:
- One short phrase, 2-6 words.
- Lowercase. No punctuation except hyphens between compound words.
- No quotes, no leading/trailing whitespace, no explanation.
- Just the phrase, nothing else.

Notes in this cluster:

{members}

Phrase:"""


def format_lobe_prompt(members: list[dict[str, str]]) -> str:
    """Build the LLM prompt from a list of member dicts.

    Each member dict carries ``label`` (note title) and ``snippet``
    (first-paragraph excerpt). Empty snippets are rendered as
    ``(no excerpt)`` so the LLM still sees the title.
    """
    lines: list[str] = []
    for i, m in enumerate(members[:MAX_MEMBERS_FED], 1):
        title = (m.get("label") or "").strip() or "(untitled)"
        snippet = (m.get("snippet") or "").strip() or "(no excerpt)"
        lines.append(f"{i}. {title}\n   {snippet}")
    return _PROMPT_TEMPLATE.format(members="\n\n".join(lines))


_SANITISE_RE = re.compile(r'^[\s"\']+|[\s"\'.]+$')


def sanitise_label(raw: str) -> str:
    """Trim whitespace/quotes/trailing periods, lowercase, and cap length.

    Qwen sometimes wraps the answer in quotes or appends a period
    despite the prompt rules. This is the last-line defence so the UI
    always gets a clean, fixed-shape string.
    """
    s = _SANITISE_RE.sub("", raw or "").lower()
    # Collapse interior whitespace runs to single spaces.
    s = re.sub(r"\s+", " ", s)
    if not s:
        return ""
    if len(s) > MAX_LABEL_CHARS:
        cut = s[:MAX_LABEL_CHARS]
        space = cut.rfind(" ")
        if space >= MAX_LABEL_CHARS - 12:
            cut = cut[:space]
        s = cut.rstrip()
    return s


# ---------------------------------------------------------------------------
# Qwen call (LAN endpoint, OpenAI-compatible via direct httpx POST)
# ---------------------------------------------------------------------------


# Discovered via ``/v1/models`` on the LAN endpoint
# (Qwen3.6 35B running under llamacpp). Important: this is a *thinking*
# model — by default it spends ~150 tokens on ``reasoning_content``
# before emitting ``content``. We disable that via the
# ``enable_thinking: False`` chat-template kwarg below; with it on a
# label call would either burn 200 completion tokens or finish with
# ``content=""`` if max_tokens is too tight.
QWEN_MODEL = "Qwen3.6-35B-A3B-Q8_K_XL"
QWEN_API_BASE = "http://10.20.30.177:8033/v1"

# Hard cap: with thinking disabled, the model only needs a few tokens
# for the phrase. Keep it small so latency is bounded if the model
# goes off-script.
QWEN_MAX_TOKENS = 32

# Per-call wall-clock budget. The /memory page already takes ~1s on a
# warm registry; a stuck endpoint shouldn't tack on 30s of dead air.
QWEN_TIMEOUT_S = 15.0


async def call_qwen_async(prompt: str) -> str:
    """Send ``prompt`` to the LAN Qwen endpoint and return the raw text.

    Plain httpx POST against the OpenAI-compatible chat-completions
    surface. No auth (LAN endpoint is unauthenticated). The viewer
    image carries httpx already; we deliberately avoid pulling in
    google-adk just for one chat call.

    Returns ``""`` on transport errors or empty responses; callers
    treat that as "label unavailable" rather than raising.
    """
    import httpx

    payload = {
        "model": QWEN_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": QWEN_MAX_TOKENS,
        # Skip the reasoning pass — labeling is a closed-form task and
        # the chain-of-thought just burns tokens. Drops a typical call
        # from ~190 completion tokens to ~5.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    async with httpx.AsyncClient(timeout=QWEN_TIMEOUT_S) as client:
        resp = await client.post(
            f"{QWEN_API_BASE}/chat/completions", json=payload
        )
        resp.raise_for_status()
        data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0].get("message") or {}).get("content") or ""
    return msg.strip()


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------


LlmCall = Callable[[str], Awaitable[str]]


async def compute_label_async(
    members: list[dict[str, str]],
    *,
    llm_call: Optional[LlmCall] = None,
) -> str:
    """Compute a display label for a lobe given its members.

    ``members`` is a list of ``{"label": ..., "snippet": ...}`` dicts —
    one per member note, in the caller's preferred order (typically
    top hubs first so they dominate when truncation kicks in).

    Returns ``""`` if the LLM produced empty output or the call raised;
    callers should treat that as "no label" and fall back to the
    mechanical hub name.
    """
    if not members:
        return ""
    if llm_call is None:
        llm_call = call_qwen_async
    prompt = format_lobe_prompt(members)
    try:
        raw = await llm_call(prompt)
    except Exception:
        # Network blip / endpoint down — keep going with the empty
        # label and let the UI fall back to the hub name.
        return ""
    return sanitise_label(raw)


def compute_label_sync(
    members: list[dict[str, str]],
    *,
    llm_call: Optional[LlmCall] = None,
) -> str:
    """Synchronous wrapper around :func:`compute_label_async`.

    Convenience for non-async call sites (e.g. one-off scripts);
    the FastAPI routes use the async form directly.
    """
    return asyncio.run(compute_label_async(members, llm_call=llm_call))
