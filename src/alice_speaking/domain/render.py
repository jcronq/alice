"""Per-transport rendering: convert Alice's markdown response into the
shape a given channel can deliver.

Two layers cooperate:

1. :func:`capability_prompt_fragment` tells Alice up-front what the
   channel can render (no markdown for Signal, full markdown for CLI,
   limited for Discord). This is the cheap win — she writes in the right
   shape on the first try.

2. :func:`render` is the safety net. It runs in the transport's
   ``send()`` path and strips/transforms markdown to match
   :class:`Capabilities`. It's deterministic (no LLM call) and runs
   even if Alice ignored the capability prompt.

Stripping uses ``mistune`` to parse markdown to AST and walk it emitting
plain text. Regex strippers were considered and rejected — too many
edge cases (nested code fences, escaped asterisks inside quotes, ATX
vs setext headings).
"""

from __future__ import annotations

from typing import Iterable

from ..transports.base import Capabilities


# ---------------------------------------------------------------------------
# Public entry points


def render(text: str, caps: Capabilities) -> list[str]:
    """Convert Alice's text into chunks ready to hand to a transport.

    Applies markdown transformation per ``caps.markdown`` then splits
    into chunks of at most ``caps.max_message_bytes``.
    """
    if caps.markdown == "none":
        text = strip_markdown(text)
    elif caps.markdown == "limited":
        text = strip_unsupported_markdown(text)
    return _chunk(text, caps.max_message_bytes)


def capability_prompt_fragment(transport_name: str, caps: Capabilities) -> str:
    """Build a system-prompt fragment telling Alice the channel's shape.

    Appended to the system prompt for the duration of one turn.
    """
    parts = [f"You are responding via the **{transport_name}** transport."]
    if caps.markdown == "none":
        parts.append(
            "Format: PLAIN TEXT only. No markdown — no **bold**, no _italics_, "
            "no `inline code`, no ``` code fences, no # headings, no - bullet "
            "markers, no [links](url). Write as if for SMS."
        )
    elif caps.markdown == "limited":
        parts.append(
            "Format: limited markdown. **bold** and _italics_ work, ``` code "
            "fences work, but headings (# ##) and tables do not render — "
            "use plain paragraphs instead."
        )
    else:
        parts.append("Format: full markdown. The client renders it.")

    parts.append(
        f"Length budget: at most {caps.max_message_bytes} bytes per message. "
        "Be terse."
    )
    if not caps.code_blocks:
        parts.append("No code blocks — paste short snippets inline only.")
    if caps.reactions:
        parts.append(
            "Reactions: prefer a single emoji acknowledgement (👍, ❤️, etc.) "
            "for short confirmations rather than a text reply."
        )
    if caps.interactive:
        parts.append(
            "This is an interactive session — the user is waiting at a "
            "terminal. Reply promptly; long deliberation costs them wall time."
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Markdown stripping (mistune AST walk)


def strip_markdown(text: str) -> str:
    """Aggressive: render markdown AST as plain text. Drops formatting,
    keeps the words. Intended for transports that render zero markdown
    (Signal).
    """
    return _walk_to_plain(_parse(text)).strip("\n")


def strip_unsupported_markdown(text: str) -> str:
    """Conservative: keep inline emphasis + code fences, flatten headings
    to plain paragraphs, normalize lists. For Discord-style limited
    markdown.
    """
    return _walk_to_limited(_parse(text)).strip("\n")


def _parse(text: str) -> list[dict]:
    """Parse markdown into mistune's AST (a list of token dicts).

    Imported lazily so unit tests that don't exercise rendering don't
    need mistune installed.
    """
    import mistune

    md = mistune.create_markdown(renderer="ast")
    tokens = md(text)
    if not isinstance(tokens, list):
        # Newer mistune versions return list directly; older returned tuple.
        tokens = list(tokens) if tokens else []
    return tokens


def _walk_to_plain(tokens: Iterable[dict]) -> str:
    """Walk an AST emitting plain text. Headings become paragraphs;
    emphasis is dropped; lists become hyphen-bullet lines; code fences
    become indented blocks; links become "text (url)".
    """
    out: list[str] = []
    for tok in tokens:
        out.append(_render_plain_token(tok))
    return "".join(out)


def _render_plain_token(tok: dict) -> str:
    t = tok.get("type", "")
    children = tok.get("children") or []

    # Block-level
    if t == "heading":
        return _walk_to_plain(children) + "\n\n"
    if t == "paragraph":
        return _walk_to_plain(children) + "\n\n"
    if t == "block_code":
        body = (tok.get("raw") or "").rstrip()
        if not body:
            return ""
        # Indent each line so the block is visually distinct without ```.
        return "\n".join("    " + line for line in body.splitlines()) + "\n\n"
    if t == "block_quote":
        body = _walk_to_plain(children)
        # Prefix lines with "> " for clarity.
        prefixed = "\n".join("> " + line if line else ">" for line in body.splitlines())
        return prefixed + "\n\n"
    if t == "list":
        return _walk_to_plain(children)
    if t == "list_item":
        body = _walk_to_plain(children).strip("\n")
        return f"- {body}\n"
    if t == "thematic_break":
        return "---\n\n"
    if t == "block_html":
        return (tok.get("raw") or "") + "\n"
    if t == "linebreak":
        return "\n"

    # Inline
    if t == "text":
        return tok.get("raw") or ""
    if t == "codespan":
        return tok.get("raw") or ""
    if t == "emphasis" or t == "strong":
        return _walk_to_plain(children)
    if t == "link":
        body = _walk_to_plain(children).strip()
        url = tok.get("url") or ""
        if not body:
            return url
        if url and url != body:
            return f"{body} ({url})"
        return body
    if t == "image":
        alt = tok.get("alt") or ""
        url = tok.get("url") or ""
        return f"[image: {alt or url}]"
    if t == "softbreak":
        return " "
    if t == "blank_line":
        return ""

    # Unknown / unhandled — recurse children, otherwise drop.
    if children:
        return _walk_to_plain(children)
    return ""


def _walk_to_limited(tokens: Iterable[dict]) -> str:
    """Limited markdown: keep emphasis + code, flatten headings, no tables."""
    out: list[str] = []
    for tok in tokens:
        out.append(_render_limited_token(tok))
    return "".join(out)


def _render_limited_token(tok: dict) -> str:
    t = tok.get("type", "")
    children = tok.get("children") or []

    if t == "heading":
        # Discord doesn't render headings as bigger text in plain channels;
        # bold them instead so they stand out.
        return f"**{_walk_to_limited(children)}**\n\n"
    if t == "paragraph":
        return _walk_to_limited(children) + "\n\n"
    if t == "block_code":
        body = (tok.get("raw") or "").rstrip()
        info = tok.get("info") or ""
        if not body:
            return ""
        return f"```{info}\n{body}\n```\n\n"
    if t == "block_quote":
        return f"> {_walk_to_limited(children)}\n"
    if t == "list":
        return _walk_to_limited(children)
    if t == "list_item":
        body = _walk_to_limited(children).strip("\n")
        return f"- {body}\n"
    if t == "thematic_break":
        return "---\n\n"
    if t == "linebreak":
        return "\n"

    if t == "text":
        return tok.get("raw") or ""
    if t == "codespan":
        return f"`{tok.get('raw') or ''}`"
    if t == "emphasis":
        return f"_{_walk_to_limited(children)}_"
    if t == "strong":
        return f"**{_walk_to_limited(children)}**"
    if t == "link":
        body = _walk_to_limited(children).strip()
        url = tok.get("url") or ""
        return f"[{body}]({url})" if url else body
    if t == "image":
        alt = tok.get("alt") or ""
        url = tok.get("url") or ""
        return f"[image: {alt or url}]({url})" if url else f"[image: {alt}]"
    if t == "softbreak":
        return " "
    if t == "blank_line":
        return ""

    if children:
        return _walk_to_limited(children)
    return ""


# ---------------------------------------------------------------------------
# Chunking


def _chunk(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` bytes (UTF-8).

    Splitting is byte-aware (UTF-8 multi-byte characters won't be cut)
    and prefers paragraph / line breaks for readability. Empty chunks
    are dropped. A single chunk if the whole thing fits.
    """
    if not text:
        return []
    if len(text.encode("utf-8")) <= limit:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining.encode("utf-8")) <= limit:
            parts.append(remaining)
            break
        # Find the longest prefix whose UTF-8 encoding fits in `limit`.
        # Start optimistic: assume 1 byte/char, then back off.
        cut = _byte_safe_cut(remaining, limit)
        head = remaining[:cut]
        # Prefer a paragraph break, then a line break, then a sentence
        # break, then a space — each within the safe head.
        for sep in ("\n\n", "\n", ". ", " "):
            idx = head.rfind(sep)
            if idx > limit // 2:
                cut = idx + len(sep)
                head = remaining[:cut]
                break
        parts.append(head.rstrip())
        remaining = remaining[cut:].lstrip()
    return [p for p in parts if p]


def _byte_safe_cut(s: str, limit: int) -> int:
    """Return the largest character index ``i`` such that
    ``s[:i].encode('utf-8')`` is at most ``limit`` bytes.
    """
    encoded = s.encode("utf-8")
    if len(encoded) <= limit:
        return len(s)
    # Walk the string, accumulating byte cost.
    total = 0
    for i, ch in enumerate(s):
        cost = len(ch.encode("utf-8"))
        if total + cost > limit:
            return i
        total += cost
    return len(s)
