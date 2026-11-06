"""Urgency-rank classifier for status-site asks.

Four ranks, from the agentry-dashboard model (overview.py's
urgency table):

* ``NOW``   — act immediately; agent parked / blocks critical chain
* ``TODAY`` — act before end of day; blocks a non-critical unit or
              merge
* ``WHEN``  — act when convenient / ripe (``WHEN READY`` /
              ``WHEN CONVENIENT`` — collapsed into one rank here)
* ``FYI``   — awareness only; no action

Plus an explicit ``UNSET`` rank the dashboard uses as a loud
"unclassified = red until someone classifies" signal. ``UNSET``
renders with the same red treatment as ``NOW`` so a lazy
``waiting-ask`` without a tag doesn't disappear into the weeds.

Three input sources, precedence first to last:

1. Explicit tag on the ask — ``record["urgency"] == "NOW"`` wins.
2. A bold markdown prefix in the free-text — ``**NOW**: foo``,
   ``**TODAY**: bar`` (parsed and stripped by the renderer).
3. Deterministic classifier: priority-field keyword match
   (``urgent`` / ``blocking`` → NOW, ``blocked`` → TODAY,
   ``soon`` → TODAY, ``later`` / ``fyi`` → WHEN/FYI) with an
   age-based fallback (anything older than
   ``age_bump_today_seconds`` with no explicit tag bumps to TODAY,
   anything older than ``age_bump_now_seconds`` bumps to NOW).

The classifier is pure and side-effect-free: ``classify(record)``
returns one of the five rank strings. Rank order (for sorting) is
exposed as ``RANK_ORDER`` — NOW first, FYI last; UNSET sits beside
NOW because it is treated as urgent by default.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Ranks, in sort order (lower = more urgent). UNSET sits with NOW so
# an untagged item can never bury a classified NOW item below it.
RANKS: tuple[str, ...] = ("NOW", "UNSET", "TODAY", "WHEN", "FYI")
RANK_ORDER: dict[str, int] = {r: i for i, r in enumerate(RANKS)}

# Default age thresholds (seconds). Tuned to be dashboard-like without
# being so aggressive that routine asks get pulled up to NOW overnight.
DEFAULT_AGE_BUMP_TODAY_SECONDS = 24 * 3600      # > 24h → TODAY
DEFAULT_AGE_BUMP_NOW_SECONDS = 72 * 3600        # > 72h → NOW

# Keyword patterns on priority / text — ordered so NOW wins ties.
_NOW_KEYWORDS = re.compile(
    r"\b(now|urgent|blocking|critical|p0|asap|immediately)\b",
    re.IGNORECASE,
)
_TODAY_KEYWORDS = re.compile(
    r"\b(today|soon|blocked|p1|high)\b",
    re.IGNORECASE,
)
_WHEN_KEYWORDS = re.compile(
    r"\b(when|whenever|later|eventually|p2|p3|low)\b",
    re.IGNORECASE,
)
_FYI_KEYWORDS = re.compile(
    r"\b(fyi|note|info|heads[-\s]?up|p4)\b",
    re.IGNORECASE,
)

# Matches a leading ``**NOW**:`` / ``**TODAY**:`` (bold) prefix on the
# ask text. The rank is captured; the renderer should strip it before
# showing the text so the pill isn't duplicated.
_INLINE_TAG_RE = re.compile(
    r"^\s*\*\*(NOW|TODAY|WHEN|FYI|UNSET)\*\*\s*[:\-—]\s*",
)


def normalize_rank(raw: Any) -> str | None:
    """Return the canonical rank string, or None if ``raw`` isn't one.

    Accepts case-insensitive ``now`` / ``Now`` / ``NOW`` etc., plus the
    dashboard's two WHEN variants (``WHEN READY`` / ``WHEN CONVENIENT``)
    which collapse to ``WHEN`` here.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip().upper()
    if not s:
        return None
    # Dashboard's two WHEN variants collapse to one rank.
    if s in ("WHEN READY", "WHEN CONVENIENT"):
        return "WHEN"
    return s if s in RANK_ORDER else None


def parse_inline_tag(text: str) -> tuple[str | None, str]:
    """Strip a leading ``**RANK**:`` prefix from ``text``.

    Returns ``(rank_or_None, remaining_text)``. The caller shows the
    rank as the urgency pill and the remaining text as the ask body.
    """
    if not isinstance(text, str):
        return None, ""
    m = _INLINE_TAG_RE.match(text)
    if not m:
        return None, text
    rank = normalize_rank(m.group(1))
    return rank, text[m.end() :]


def _age_seconds(record: dict, *, now: datetime | None = None) -> float:
    """Age in seconds from ``record["created"]``.

    Missing / unparseable ``created`` returns 0.0 (treated as "just
    now"); a timestamp in the future also returns 0.0 (clock skew
    tolerance — do not reward the classifier for a bad clock).
    """
    raw = record.get("created")
    if not isinstance(raw, str):
        return 0.0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    age = (now_dt - dt).total_seconds()
    return max(0.0, age)


def classify(
    record: dict,
    *,
    now: datetime | None = None,
    age_bump_today_seconds: float = DEFAULT_AGE_BUMP_TODAY_SECONDS,
    age_bump_now_seconds: float = DEFAULT_AGE_BUMP_NOW_SECONDS,
) -> str:
    """Return the urgency rank for ``record``.

    Precedence (highest wins):

    1. explicit ``record["urgency"]`` field
    2. a leading ``**RANK**:`` prefix in ``record["text"]``
    3. keyword match on ``record["priority"]`` or ``record["text"]``
    4. age bump (older asks drift toward NOW)
    5. ``UNSET`` — the default, which renders red.

    The function never raises; a malformed record returns ``UNSET``
    so the classifier can't 500 the whole render.
    """
    # 1. Explicit tag field.
    rank = normalize_rank(record.get("urgency"))
    if rank:
        return rank

    text = str(record.get("text") or "")
    # 2. Inline ``**RANK**:`` prefix.
    inline_rank, _ = parse_inline_tag(text)
    if inline_rank:
        return inline_rank

    priority = str(record.get("priority") or "")
    haystack = f"{priority}\n{text}"

    # 3. Keyword match — NOW > TODAY > WHEN > FYI, first-win.
    if _NOW_KEYWORDS.search(haystack):
        return "NOW"
    if _TODAY_KEYWORDS.search(haystack):
        return "TODAY"
    if _FYI_KEYWORDS.search(haystack):
        return "FYI"
    if _WHEN_KEYWORDS.search(haystack):
        return "WHEN"

    # 4. Age-based bump.
    age = _age_seconds(record, now=now)
    if age >= age_bump_now_seconds:
        return "NOW"
    if age >= age_bump_today_seconds:
        return "TODAY"

    # 5. Nothing matched → UNSET (rendered red, loud).
    return "UNSET"


def sort_key(record: dict, *, now: datetime | None = None) -> tuple[int, float]:
    """``(rank_order, -age)`` sort key: rank first, oldest-within-rank
    second. Use with ``records.sort(key=lambda r: urgency.sort_key(r))``
    to get the dashboard's stable ordering.
    """
    rank = classify(record, now=now)
    # Negative age so ``min()`` returns the oldest within a rank.
    return (RANK_ORDER[rank], -_age_seconds(record, now=now))


def pill_html(rank: str) -> str:
    """Return the ``<span class="fx-urg RANK">RANK</span>`` snippet.

    The rank is validated via ``normalize_rank`` and falls back to
    ``UNSET`` on anything we don't recognise.
    """
    canon = normalize_rank(rank) or "UNSET"
    return f'<span class="fx-urg {canon}">{canon}</span>'


def blocked_mark_html(label: str = "blocked") -> str:
    """Short bordered ``fx-mark`` inline for a blocked item."""
    safe = re.sub(r"[^A-Za-z0-9 _-]", "", str(label))[:40] or "blocked"
    return f'<span class="fx-mark blocked">{safe}</span>'
