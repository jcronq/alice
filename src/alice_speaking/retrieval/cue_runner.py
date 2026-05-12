"""Cue runner — pre-turn vault retrieval for Speaking Alice.

Before Speaking composes a response, the cue runner fires:
1. Tokenizes the user query and runs an FTS5 MATCH against
   ``cortex-index.db`` (over-fetching 15 candidates).
2. Applies a type-aware boost (STATE 2.0×, BEHAVIOR 1.5×, BUCKET2 1.3×,
   BUCKET1 1.0×; +1.5× extra when frontmatter ``trigger_keywords``
   match the user query).
3. (Optional Phase 2) Calls a reranker LLM on the 15-candidate set.
4. Selects the top N (default 5), formats them into a reference
   packet, and fires a fire-and-forget ``access_count`` bump on each
   note that made the final cut.

Failure must NEVER break a turn. Every public entry path is wrapped
in try/except and returns ``""`` on any error. The integration point
in :mod:`alice_speaking.turn_runner` short-circuits when the packet
is empty, so a degraded cue runner is indistinguishable from an
unconfigured one.

Phase 2 reranker swap path
--------------------------

The reranker call goes through :func:`_call_reranker`, a thin
abstraction. v1 ships with a hardcoded Anthropic SDK call so no extra
dependency is required. To swap to a LiteLLM-fronted local model
(e.g. ``qwen3-4b`` on Strix Halo), replace the body of
:func:`_call_reranker` with an OpenAI-compatible call — LiteLLM
exposes the OpenAI shape — and route via
``cfg["reranker"]["litellm_endpoint"]``. The model name is read from
``cfg["reranker"]["model"]`` so swapping is a config change rather
than a code change once the alternate path lands.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import pathlib
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable


log = logging.getLogger(__name__)


# Type-aware boost weights. Calibrated to 1.0 across the board on 2026-05-06
# after eval (cortex-memory/research/2026-05-06-cue-runner-eval.md): the
# proposed coefficients (state 2.0, behavior 1.5, bucket2 1.3) regressed F1@5
# by 14% relative to pure FTS across every topic except `state` (which tied).
# bucket2=1.3 was the load-bearing degradation — it elevated tag-tagged
# research notes above more topically-precise ones. Constants kept (rather
# than removed) so future calibrated values can be slotted in without code
# shape change. Re-evaluate before raising any of these above 1.0.
STATE_BOOST = 1.0
BEHAVIOR_BOOST = 1.0
BUCKET2_BOOST = 1.0
BUCKET1_BOOST = 1.0
# Trigger-keyword secondary boost: a no-op in v1 because no vault notes carry
# `trigger_keywords` in frontmatter yet (proposed in
# cortex-memory/research/2026-05-05-vault-behavior-notes-jit-retrieval.md but
# not backfilled). Constant kept; re-evaluate after the backfill.
TRIGGER_KEYWORD_EXTRA = 1.0

_STATE_TYPES = {"daily", "state-snapshot", "skill"}
_BUCKET2_TAGS = {
    "cozyhem",
    "alice-architecture",
    "ripped-by-40",
    "strix-halo",
    "alice-thinking",
    "alice-speaking",
}

# Over-fetch ceiling before boost + final cut. Brief §New module step 2.
_FTS_OVER_FETCH = 15

# Defaults pulled from SPEAKING_DEFAULTS["cue_runner"]. Re-stated here
# so build_cue_packet() can fall back when callers pass a partial cfg
# (notably in tests).
# Calibrated to 3 on 2026-05-06 after the noise-reduction sweep
# (cortex-memory/research/2026-05-06-cue-runner-eval.md §10):
# top-3 has higher precision (0.39 vs 0.30 at top-5), higher F1 (0.376 vs 0.363),
# 40% fewer noise tokens injected per turn, at the cost of lower recall (0.41 vs 0.52).
# Score-floor and min-query-length gates were also tested — neither paid off.
_DEFAULT_TOP_N = 3
_DEFAULT_LINE_CAP = 5
_DEFAULT_TIMEOUT_MS = 500
_DEFAULT_PACKET_TOKEN_CEILING = 1000

# Cheap stopword filter for query tokenization. Aim is to drop common
# function words and turn "what's the latest on cozyhem?" into
# ["latest", "cozyhem"]. Conservative: when in doubt keep the word.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "she",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "yours",
        "about",
        "into",
        "over",
        "than",
        "then",
        "there",
        "these",
        "those",
        "would",
        "could",
        "should",
        "did",
        "any",
        "all",
        "some",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TRIGGER_KEYWORDS_RE = re.compile(
    r"^trigger_keywords:\s*\[(.*?)\]\s*$", re.MULTILINE | re.IGNORECASE
)
_ACCESS_COUNT_RE = re.compile(r"^access_count:\s*(\d+)\s*$", re.MULTILINE)
_LAST_ACCESSED_RE = re.compile(r"^last_accessed:\s*[^\n]*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Internal data shapes


@dataclass
class _Candidate:
    """One FTS hit, after enrichment + boost scoring."""

    slug: str
    title: str
    note_type: str
    tags: list[str]
    body: str
    path: str  # vault-relative
    fts_rank: float  # raw rank (lower is better in FTS5)
    boost: float
    final_score: float
    matched_lines: list[dict[str, Any]]
    why_relevant: str = ""


# ---------------------------------------------------------------------------
# Classification + tokenization


def classify_note(note_type: str, tags: Iterable[str]) -> float:
    """Return the multiplicative boost for a note based on type + tags.

    See brief §Type-Aware Boost. The fallback is BUCKET1_BOOST (1.0×).
    """
    tag_list = list(tags)
    if note_type in _STATE_TYPES:
        return STATE_BOOST
    if note_type == "behavior":
        return BEHAVIOR_BOOST
    if note_type == "finding" or any(t in _BUCKET2_TAGS for t in tag_list):
        return BUCKET2_BOOST
    return BUCKET1_BOOST


def _tokenize_query(query: str) -> list[str]:
    """Pull search terms out of a user query.

    Lowercased, stopword-filtered, deduped while preserving order.
    Empty result is possible (e.g. user just said "hi") — the caller
    must short-circuit on that.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(query.lower()):
        if len(raw) < 2 or raw in _STOPWORDS:
            continue
        if raw in seen:
            continue
        seen.add(raw)
        tokens.append(raw)
    return tokens


def _build_fts_match(tokens: list[str]) -> str:
    """Compose an FTS5 MATCH expression from raw tokens.

    Quote each token to neutralise FTS5 special characters (``-``,
    ``*``, ``"``, ``(``, ``)``) and join with OR so a partial keyword
    overlap still produces hits.
    """
    quoted: list[str] = []
    for t in tokens:
        cleaned = t.replace('"', "")
        if cleaned:
            quoted.append(f'"{cleaned}"')
    return " OR ".join(quoted)


def extract_matched_lines(
    body: str, terms: Iterable[str], max_n: int = _DEFAULT_LINE_CAP
) -> list[dict[str, Any]]:
    """Pull up to ``max_n`` lines from ``body`` that contain any term.

    Returns a list of ``{"n": <1-indexed line number>, "text": <stripped>}``
    dicts. Empty lines and lines under 3 chars are skipped.
    """
    term_list = [t.lower() for t in terms if t]
    if not term_list:
        return []
    hits: list[dict[str, Any]] = []
    for i, line in enumerate(body.split("\n"), start=1):
        stripped = line.strip()
        if len(stripped) < 3:
            continue
        lowered = stripped.lower()
        if any(t in lowered for t in term_list):
            hits.append({"n": i, "text": stripped})
            if len(hits) >= max_n:
                break
    return hits


# ---------------------------------------------------------------------------
# Frontmatter helpers (for trigger_keywords + access_count bumps)


def _read_trigger_keywords(vault_root: pathlib.Path, rel_path: str) -> list[str]:
    """Parse ``trigger_keywords: [a, b, c]`` from a note's frontmatter.

    Returns ``[]`` on any error (file missing, no frontmatter, no
    field). The frontmatter parsing is intentionally cheap and
    line-based — full YAML parsing would pull a dep we don't need
    here.
    """
    try:
        text = (vault_root / rel_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        return []
    fm = fm_match.group(1)
    kw_match = _TRIGGER_KEYWORDS_RE.search(fm)
    if not kw_match:
        return []
    inner = kw_match.group(1)
    out: list[str] = []
    for part in inner.split(","):
        cleaned = part.strip().strip('"').strip("'")
        if cleaned:
            out.append(cleaned.lower())
    return out


async def _bump_access(
    vault_root: pathlib.Path,
    rel_path: str,
    *,
    db_path: pathlib.Path | None = None,
    slug: str | None = None,
) -> None:
    """Fire-and-forget bump of ``access_count`` + ``last_accessed`` in
    a note's frontmatter AND (when ``db_path`` is provided) in the
    cortex-index ``note_metrics`` table.

    Best-effort across both writes: a missing frontmatter, a permissions
    error, or a race with another writer all silently no-op. The two
    writes are independent — if one succeeds and the other fails, we
    log a debug warning but never raise. Both stores are recoverable
    from each other (the seed script reconciles the DB from the
    markdown source of truth on demand). See
    ``cortex-memory/research/2026-05-11-note-metrics-data-pipeline-design.md``
    for the rationale.

    Mirrors the logic in :func:`alice_speaking.tools.memory._bump_access`
    but lives here so the cue runner doesn't import a sibling tools
    module.
    """

    def _do_bump() -> None:
        path = vault_root / rel_path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        fm_match = _FRONTMATTER_RE.match(text)
        if not fm_match:
            return
        fm = fm_match.group(1)
        today = datetime.date.today().isoformat()
        # Update or append last_accessed.
        if _LAST_ACCESSED_RE.search(fm):
            new_fm = _LAST_ACCESSED_RE.sub(f"last_accessed: {today}", fm, count=1)
        else:
            new_fm = fm.rstrip() + f"\nlast_accessed: {today}"
        # Increment access_count.
        ac_match = _ACCESS_COUNT_RE.search(new_fm)
        if ac_match:
            cur = int(ac_match.group(1))
            new_fm = _ACCESS_COUNT_RE.sub(f"access_count: {cur + 1}", new_fm, count=1)
        else:
            new_fm = new_fm.rstrip() + "\naccess_count: 1"
        new_text = "---\n" + new_fm + "\n---\n" + text[fm_match.end() :]
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError:
            return

    def _do_db_bump() -> None:
        # Resolve the slug we should target. Prefer the explicit slug
        # passed in (matches what the cue runner saw in the FTS row).
        # Fall back to filename stem — matches build_index.slug_for's
        # common case (the disambiguated-collision case only kicks in
        # for the handful of duplicate stems in the vault, and we'd
        # rather miss those than incorrectly bump a sibling note).
        target_slug = slug or pathlib.Path(rel_path).stem
        if not target_slug or db_path is None:
            return
        conn = sqlite3.connect(str(db_path))
        try:
            # Upsert so we tolerate a row that hasn't been seeded yet
            # (rare — the indexer seeds every slug — but defensive).
            conn.execute(
                """
                INSERT INTO note_metrics(slug, access_count)
                VALUES(?, 1)
                ON CONFLICT(slug) DO UPDATE SET
                    access_count = access_count + 1
                """,
                (target_slug,),
            )
            conn.commit()
        finally:
            conn.close()

    try:
        await asyncio.to_thread(_do_bump)
    except Exception:  # noqa: BLE001
        # Truly best-effort. We don't want a stray FS error to leak
        # into the turn loop via an uncaught task exception.
        log.debug("access_count bump failed for %s", rel_path, exc_info=True)

    if db_path is not None:
        try:
            await asyncio.to_thread(_do_db_bump)
        except Exception:  # noqa: BLE001
            # DB write failures are independently best-effort. The
            # frontmatter is the source of truth; the seed script can
            # reconcile any drift.
            log.debug(
                "note_metrics bump failed for slug=%s db=%s",
                slug or rel_path,
                db_path,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# DB query


def _query_fts(
    db_path: pathlib.Path, fts_match: str, limit: int = _FTS_OVER_FETCH
) -> list[tuple[str, str, str, str, str, float]]:
    """Run the FTS5 MATCH query synchronously. Caller wraps in
    :func:`asyncio.to_thread`.

    Returns a list of (slug, title, note_type, tags_json, body, rank,
    path) tuples. ``rank`` is FTS5's default scoring (lower = better);
    we pass it through unchanged.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                n.slug, n.title, n.note_type, n.tags_json, n.body,
                n.path, notes_fts.rank AS fts_rank
            FROM notes n
            JOIN notes_fts ON notes_fts.rowid = n.rowid
            WHERE notes_fts MATCH ?
            ORDER BY notes_fts.rank
            LIMIT ?
            """,
            (fts_match, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        (
            r["slug"],
            r["title"] or r["slug"],
            r["note_type"] or "",
            r["tags_json"] or "[]",
            r["body"] or "",
            r["path"] or "",
            float(r["fts_rank"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Reranker (Phase 2 — gated)


async def _call_reranker(
    query: str,
    candidates: list[_Candidate],
    cfg: dict[str, Any],
) -> list[_Candidate]:
    """Rerank ``candidates`` with an LLM. Returns the reranked list.

    v1 hardcodes the Anthropic SDK + Haiku. To swap to a LiteLLM-
    fronted local model (the long-term plan — ``qwen3-4b`` on Strix
    Halo), replace this body with an OpenAI-compatible call. LiteLLM
    exposes the OpenAI shape, so the swap is mechanical:

    .. code-block:: python

        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            base_url=cfg["reranker"]["litellm_endpoint"],
            api_key="ignored",
        )
        resp = await client.chat.completions.create(
            model=cfg["reranker"]["model"],
            messages=[...],
        )

    The model name MUST come from ``cfg["reranker"]["model"]`` —
    never hardcode it (Jason explicitly authorised this 2026-05-06
    07:30 EDT and reserved the right to swap).

    On any error (network, malformed response, hallucinated slug)
    return the input ``candidates`` unchanged so the caller falls
    back to the boost-ranked order.
    """
    reranker_cfg = cfg.get("reranker", {})
    model = reranker_cfg.get("model", "claude-haiku-4-5-20251001")
    timeout_s = reranker_cfg.get("timeout_ms", 1500) / 1000.0
    try:
        # Lazy import: the SDK is optional in v1 — without it the
        # reranker is simply disabled.
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        log.debug("anthropic SDK not installed; reranker disabled")
        return candidates

    candidates_block = _format_candidates_for_rerank(candidates)
    prompt = (
        f"User query: {query}\n\n"
        "Below are candidate vault notes pulled by FTS. Rerank by "
        "relevance to the query and return a JSON list of "
        '{"slug": "<slug>", "score": <0..1>, "why_relevant": "<one '
        'sentence>"} objects, top first. Cap at 5 entries. Do not '
        "invent slugs that aren't in the candidates. Return [] if "
        "nothing is relevant.\n\n"
        f"Candidates:\n{candidates_block}"
    )
    client = anthropic.Anthropic()
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                client.messages.create,
                model=model,
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=timeout_s,
        )
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        log.debug("reranker call failed; falling back to boost order", exc_info=True)
        return candidates

    text = ""
    try:
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text += block.text
    except (AttributeError, TypeError):
        return candidates

    return _apply_rerank(text, candidates)


def _format_candidates_for_rerank(candidates: list[_Candidate]) -> str:
    lines: list[str] = []
    for c in candidates:
        snippet = " | ".join(ml["text"][:120] for ml in c.matched_lines[:3])
        lines.append(f"- slug: {c.slug}\n  title: {c.title}\n  matched: {snippet}")
    return "\n".join(lines)


def _apply_rerank(model_text: str, candidates: list[_Candidate]) -> list[_Candidate]:
    """Parse reranker JSON output and reorder ``candidates``.

    Hallucinated slugs (not in the input) are dropped. Any parse
    failure returns the input list unchanged.
    """
    try:
        # The model may wrap JSON in prose; pull the first ``[...]``.
        match = re.search(
            r"\[\s*(?:\{.*?\})?(?:\s*,\s*\{.*?\})*\s*\]", model_text, re.DOTALL
        )
        if match is None:
            return candidates
        parsed = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return candidates
    if not isinstance(parsed, list):
        return candidates

    by_slug = {c.slug: c for c in candidates}
    reordered: list[_Candidate] = []
    seen: set[str] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or slug not in by_slug or slug in seen:
            continue
        cand = by_slug[slug]
        why = entry.get("why_relevant", "")
        if isinstance(why, str):
            cand.why_relevant = why.strip()
        reordered.append(cand)
        seen.add(slug)
    if not reordered:
        return candidates
    # Append any candidates the reranker dropped, after the reranked
    # ones, so the caller's top-N cut still has fallback content if
    # the reranker returned a single relevant entry.
    for c in candidates:
        if c.slug not in seen:
            reordered.append(c)
    return reordered


# ---------------------------------------------------------------------------
# Packet formatter


def _format_packet(candidates: list[_Candidate], packet_token_ceiling: int) -> str:
    """Render the final candidates into the prompt-prefix string.

    Stops adding entries once ``packet_token_ceiling`` (estimated as
    chars/4) is exceeded. Empty candidate list returns ``""`` — the
    caller's ``if cue_packet`` short-circuits with no preamble.
    """
    if not candidates:
        return ""

    char_budget = packet_token_ceiling * 4
    parts: list[str] = ["[VAULT CONTEXT — top matches for your query]", ""]
    used = sum(len(p) + 1 for p in parts)
    for idx, c in enumerate(candidates, start=1):
        block_lines: list[str] = [f"{idx}. **{c.title}** (`{c.slug}`)"]
        for ml in c.matched_lines:
            block_lines.append(f"   > L{ml['n']}: {ml['text']}")
        if c.why_relevant:
            block_lines.append(f"   _Why relevant: {c.why_relevant}_")
        block = "\n".join(block_lines) + "\n"
        if used + len(block) > char_budget and idx > 1:
            break
        parts.append(block)
        used += len(block)
    parts.append("[End vault context]")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public entry point


async def build_cue_packet(
    query: str,
    cfg: dict[str, Any],
    *,
    db_path: pathlib.Path | None = None,
    vault_root: pathlib.Path | None = None,
    context_slugs: Iterable[str] = (),
) -> str:
    """Return a vault-context preamble for ``query`` or ``""`` on any
    error.

    The cue runner sits in the hot path of every Speaking turn — an
    uncaught exception here is a Speaking-down event. Every code path
    in this function must be inside the top-level try/except.

    Parameters
    ----------
    query
        The raw inbound user message.
    cfg
        ``cfg.speaking["cue_runner"]`` dict from
        :data:`alice_speaking.infra.config.SPEAKING_DEFAULTS`. Must
        contain at least ``enabled``; everything else has defaults.
    db_path
        Override the indexer DB path. Defaults to
        ``cfg["db_path"]`` then to
        ``~/alice-mind/inner/state/cortex-index.db``.
    vault_root
        Override the vault root. Defaults to
        ``~/alice-mind/cortex-memory``.
    context_slugs
        Slugs already named in the conversation; deduped out of the
        packet so we don't re-summarise notes Speaking is already
        looking at.
    """
    try:
        if not cfg.get("enabled", False):
            return ""

        tokens = _tokenize_query(query)
        if not tokens:
            return ""

        resolved_db = _resolve_db_path(cfg, db_path)
        resolved_vault = _resolve_vault_root(vault_root)
        if not resolved_db.exists():
            log.debug("cue_runner: db_path %s does not exist", resolved_db)
            return ""

        timeout_s = cfg.get("timeout_ms", _DEFAULT_TIMEOUT_MS) / 1000.0
        line_cap = int(cfg.get("per_note_line_cap", _DEFAULT_LINE_CAP))
        top_n = int(cfg.get("top_n", _DEFAULT_TOP_N))
        packet_ceiling = int(
            cfg.get("packet_token_ceiling", _DEFAULT_PACKET_TOKEN_CEILING)
        )

        fts_match = _build_fts_match(tokens)
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(_query_fts, resolved_db, fts_match),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            log.debug("cue_runner: FTS query timed out (%.0fms)", timeout_s * 1000)
            return ""

        # Build candidates with type-aware boost.
        excluded = set(context_slugs or ())
        query_lower = query.lower()
        candidates: list[_Candidate] = []
        for slug, title, note_type, tags_json, body, path, fts_rank in rows:
            if slug in excluded:
                continue
            try:
                tags = json.loads(tags_json) or []
            except (json.JSONDecodeError, TypeError):
                tags = []
            boost = classify_note(note_type, tags)
            # Trigger-keyword secondary boost: if any keyword from the
            # note's frontmatter is in the user query, multiply by 1.5×.
            kws = _read_trigger_keywords(resolved_vault, path) if path else []
            if kws and any(kw in query_lower for kw in kws):
                boost *= TRIGGER_KEYWORD_EXTRA
            # FTS5's default rank is negative (lower = more relevant);
            # invert so higher final_score = better, then multiply by
            # boost to keep the boost meaningful.
            base_score = -float(fts_rank)
            final_score = base_score * boost
            matched = extract_matched_lines(body, tokens, max_n=line_cap)
            candidates.append(
                _Candidate(
                    slug=slug,
                    title=title,
                    note_type=note_type,
                    tags=tags,
                    body=body,
                    path=path,
                    fts_rank=fts_rank,
                    boost=boost,
                    final_score=final_score,
                    matched_lines=matched,
                )
            )

        if not candidates:
            return ""

        # Sort by boosted score (descending).
        candidates.sort(key=lambda c: c.final_score, reverse=True)

        # Phase 2 reranker: gated. The reranker sees the full
        # over-fetch set (post-boost) and may reorder before the
        # final cut.
        reranker_cfg = cfg.get("reranker", {})
        if reranker_cfg.get("enabled", False):
            candidates = await _call_reranker(query, candidates, cfg)

        final = candidates[:top_n]
        packet = _format_packet(final, packet_ceiling)
        if not packet:
            return ""

        # Fire-and-forget access_count bumps. Don't await — the bumps
        # don't block the turn. Wrap each in a logged background task
        # so an exception in one doesn't poison the others. Slug + DB
        # path are passed through so the bump also updates the
        # note_metrics table — without this the SQL recency boost
        # cannot fire (see
        # cortex-memory/research/2026-05-11-retrieval-data-pipeline-critical.md).
        for c in final:
            if c.path:
                _spawn_bump(resolved_vault, c.path, db_path=resolved_db, slug=c.slug)

        return packet
    except Exception:  # noqa: BLE001
        log.exception("cue_runner: unexpected error; returning empty packet")
        return ""


# ---------------------------------------------------------------------------
# Helpers that don't need to live inside the try/except


def _resolve_db_path(
    cfg: dict[str, Any], explicit: pathlib.Path | None
) -> pathlib.Path:
    if explicit is not None:
        return explicit
    raw = cfg.get("db_path") or ""
    if raw:
        return pathlib.Path(raw)
    return pathlib.Path.home() / "alice-mind" / "inner" / "state" / "cortex-index.db"


def _resolve_vault_root(explicit: pathlib.Path | None) -> pathlib.Path:
    if explicit is not None:
        return explicit
    return pathlib.Path.home() / "alice-mind" / "cortex-memory"


def _spawn_bump(
    vault_root: pathlib.Path,
    rel_path: str,
    *,
    db_path: pathlib.Path | None = None,
    slug: str | None = None,
) -> None:
    """Schedule a fire-and-forget access_count bump.

    No-ops cleanly when there's no running event loop (e.g. during
    synchronous unit tests that probe the helper directly).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(
        _bump_access(vault_root, rel_path, db_path=db_path, slug=slug)
    )
    # Swallow any exception from the background task so it doesn't
    # surface as an "unhandled exception" warning.
    task.add_done_callback(_log_bump_failure)


def _log_bump_failure(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.debug("cue_runner: access_count bump task failed: %r", exc)


__all__ = [
    "build_cue_packet",
    "classify_note",
    "extract_matched_lines",
    # Constants exposed for tests.
    "STATE_BOOST",
    "BEHAVIOR_BOOST",
    "BUCKET2_BOOST",
    "BUCKET1_BOOST",
    "TRIGGER_KEYWORD_EXTRA",
]
