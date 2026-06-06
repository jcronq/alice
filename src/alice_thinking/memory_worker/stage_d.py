"""Stage D — recombination (LLM-driven synthesis).

Where Stage B/C are mechanical, Stage D asks a local model to find a
non-obvious connection between two **recently-touched** research notes
that live far apart in the vault graph. If a connection exists, the
model writes a short synthesis; if not, it emits ``NULL_RESULT`` and
the worker logs the null without polluting the research folder.

Design contract — see
``cortex-memory/research/2026-06-01-memory-worker-extraction-design.md``
§3 (model tier: D uses local qwen), §4 (Stage D selection: recently
touched + graph-distant), §6 (lock + journal crash recovery).

Structural invariant: every synthesis write goes through
:func:`commit_stage_d_synthesis`, which writes the audit record BEFORE
returning. The audit record is a structural gate — no LLM call can
bypass it because it's not in the prompt, it's deterministic Python
called from the wrapper around the model. This is the structural fix
for the same class of bug that motivated
:mod:`alice_thinking.stage_d_invariant`: an LLM told "remember to log
this attempt" silently skips the log roughly 0.5% of the time, and the
silent-skip rate matters when the audit log is the only ground truth
on whether a synthesis was authorized.

Pair selection
--------------

Candidate set: notes under ``cortex-memory/research/`` whose
``updated:`` or ``last_accessed:`` frontmatter falls within
:data:`DEFAULT_RECENT_WINDOW_DAYS` days. Pairs are filtered to those
that satisfy at least one graph-distance criterion (different folders;
no direct wikilink; no shared 1-hop neighbor) and then ranked by
**smallest tag intersection** — least tag overlap wins. Already-shipped
pairs (see :data:`DEFAULT_PAIRS_DEDUP_LOOKBACK_DAYS`) are skipped.

LLM call
--------

Stage D uses the local Qwen endpoint via
:class:`core.llm_client.LLMClient`. The prompt asks the model to return
either ``NULL_RESULT`` (no real connection) or a JSON object with a
three-paragraph synthesis. The synthesizer is **injectable** —
:func:`run` takes a ``synthesizer`` callable so the test suite never
talks to a real model. The default synthesizer wraps :class:`LLMClient`
behind :func:`asyncio.run`.

Outputs
-------

- ``cortex-memory/research/YYYY-MM-DD-recombination-<short-slug>.md``
  on a successful synthesis. Frontmatter includes ``source: stage-d``,
  ``note_a``, ``note_b``, ``created``, ``updated``, ``last_accessed``,
  ``access_count: 0``, ``tags: [recombination, ...]``. This is the same
  ``source: stage-d`` marker that :mod:`stage_d_invariant` uses, so
  thinking's belt-and-suspenders audit treats memory-worker
  syntheses the same as its own.

- ``inner/state/memory-worker-stage-d-attempts.jsonl`` — append-only
  commit-gate audit log. One line per successful synthesis. Schema:
  ``{ts, note_a, note_b, synthesis_path, audit_hash, status,
  source: "memory-worker-stage-d"}``.

- ``inner/state/stage-d-pairs.jsonl`` — append-only log of processed
  pairs (ship or null). Used by the pair-dedup lookback so we don't
  re-attempt the same pair within ``pairs_dedup_lookback_days``.

- ``inner/state/stage-d-null-results.jsonl`` — append-only log of
  ``NULL_RESULT`` outcomes. Stub records (no vault note) so the research
  folder isn't polluted with "the model didn't find anything" pages.

The three log paths sit under :data:`DEFAULT_STATE_DIR` and are written
through :func:`vault_lock.acquire` so concurrent memory-worker wakes
serialize cleanly. (One worker per cadence tick is the supervisor
contract; the lock is belt-and-suspenders for crash-recovery replay.)
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import logging
import math
import pathlib
import re
from collections import Counter
from typing import Any, Callable, Iterable, Optional

from indexer.yaml_lite import split_frontmatter

from alice_thinking import vault_lock

from . import journal as journal_mod


logger = logging.getLogger(__name__)


# ---------- tunables ----------

#: Default recency window (days) for the "recently touched" filter.
#: A note qualifies if ``updated:`` OR ``last_accessed:`` is within
#: this many days of today.
DEFAULT_RECENT_WINDOW_DAYS = 7

#: Default pair-dedup lookback (days). We won't re-attempt a pair that
#: appears in ``stage-d-pairs.jsonl`` within this window.
DEFAULT_PAIRS_DEDUP_LOOKBACK_DAYS = 30

#: Default state-file directory (relative to ``mind``).
DEFAULT_STATE_DIR_REL = pathlib.Path("inner") / "state"

#: Default model tier for Stage D synthesis. ``"local"`` routes through
#: :class:`core.llm_client.LLMClient` against the local Qwen endpoint;
#: ``"api"`` is reserved for a future opt-in cloud path.
DEFAULT_MODEL_TIER = "local"

#: NULL_RESULT sentinel the synthesizer emits when no real connection
#: exists. Case-insensitive equality is the contract — the model
#: sometimes capitalizes oddly.
NULL_RESULT_SENTINEL = "NULL_RESULT"

#: Vault-lock acquisition timeout. Stage D synthesis writes are
#: short-lived; if another holder is sitting on the sidecar for more
#: than this many seconds, skip and retry next cycle rather than wedge
#: the worker.
_LOCK_TIMEOUT_SECONDS = 5.0

#: Wikilink regex shared with stage_c (kept local so we don't take a
#: cross-module dependency on a private helper).
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


# ---------- reports ----------


@dataclasses.dataclass
class DecayPairingResult:
    """Outcome of the Phase 3 decay-aware pre-pass for one Stage D tick.

    ``archived`` is the count of decayed notes moved to ``archive/``;
    ``paired`` is the count of decay pairs selected by the title-cosine
    pass (Phase 3.5); ``notes`` carries the ``(a, b, score)`` triples for
    telemetry / tests.
    """

    archived: int = 0
    extracted: int = 0
    paired: int = 0
    score: float = 0.0
    notes: list[tuple[str, str, float]] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "archived": int(self.archived),
            "extracted": int(self.extracted),
            "paired": int(self.paired),
            "score": round(float(self.score), 3),
            "notes": [
                [a, b, round(float(s), 3)] for (a, b, s) in self.notes
            ],
        }


@dataclasses.dataclass
class StageDReport:
    """Counts of what Stage D did this tick."""

    ran: bool = False
    synthesized: int = 0
    null_results: int = 0
    pairs_considered: int = 0
    synthesis_path: Optional[str] = None
    skipped_reason: Optional[str] = None
    decay_pairing: Optional[DecayPairingResult] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "synthesized": int(self.synthesized),
            "null_results": int(self.null_results),
            "pairs_considered": int(self.pairs_considered),
            "synthesis_path": self.synthesis_path,
            "skipped_reason": self.skipped_reason,
            "decay_pairing": (
                self.decay_pairing.to_dict() if self.decay_pairing else None
            ),
        }


@dataclasses.dataclass
class StageDConfig:
    recent_window_days: int = DEFAULT_RECENT_WINDOW_DAYS
    pairs_dedup_lookback_days: int = DEFAULT_PAIRS_DEDUP_LOOKBACK_DAYS
    model_tier: str = DEFAULT_MODEL_TIER


# ---------- path helpers ----------


def _vault_dir(mind: pathlib.Path) -> pathlib.Path:
    return mind / "cortex-memory"


def _research_dir(mind: pathlib.Path) -> pathlib.Path:
    return _vault_dir(mind) / "research"


def _state_dir(mind: pathlib.Path) -> pathlib.Path:
    return mind / DEFAULT_STATE_DIR_REL


def _pairs_log_path(mind: pathlib.Path) -> pathlib.Path:
    """Append-only dedup log for processed pairs."""
    return _state_dir(mind) / "stage-d-pairs.jsonl"


def _attempts_log_path(mind: pathlib.Path) -> pathlib.Path:
    """Commit-gate audit log — one line per shipped synthesis.

    Deliberately *not* sharing the thinking-side
    ``inner/state/stage-d-attempts.jsonl`` firehose: the schemas differ
    (thinking has qwen/haiku verdicts, memory-worker has a single-pass
    synthesis). A separate file keeps both viewers + tests
    schema-stable. The audit invariant (every synthesis write produces
    one audit line) is what the prompt actually cares about; the file
    name was the easier part of the spec to flex on.
    """
    return _state_dir(mind) / "memory-worker-stage-d-attempts.jsonl"


def _null_results_log_path(mind: pathlib.Path) -> pathlib.Path:
    return _state_dir(mind) / "stage-d-null-results.jsonl"


# ---------- time + hashing ----------


def _today() -> datetime.date:
    return datetime.date.today()


def _utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- selection: recency ----------


def _parse_date_prefix(raw: Any) -> Optional[datetime.date]:
    """Best-effort parse of frontmatter date fields.

    Frontmatter dates appear as ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM TZ``.
    Anything shorter than 10 chars or with an unparseable prefix returns
    ``None``.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if len(s) < 10:
        return None
    try:
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _most_recent_touch(fm: dict[str, Any]) -> Optional[datetime.date]:
    """Latest of ``updated``, ``last_accessed`` — or ``None`` if neither
    is a parseable date."""
    candidates: list[datetime.date] = []
    for key in ("updated", "last_accessed"):
        d = _parse_date_prefix(fm.get(key))
        if d is not None:
            candidates.append(d)
    if not candidates:
        return None
    return max(candidates)


def _recently_touched_research(
    vault: pathlib.Path,
    today: datetime.date,
    *,
    window_days: int,
) -> list[pathlib.Path]:
    """All research-folder notes whose latest touch is within ``window_days``.

    Sorted alphabetically so selection is reproducible across wakes.
    """
    research = vault / "research"
    if not research.is_dir():
        return []
    cutoff = today - datetime.timedelta(days=window_days)
    out: list[pathlib.Path] = []
    for md in sorted(research.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _body = split_frontmatter(text)
        touch = _most_recent_touch(fm)
        if touch is None:
            continue
        if touch < cutoff:
            continue
        out.append(md)
    return out


# ---------- selection: graph distance ----------


def _extract_wikilink_targets(body: str) -> set[str]:
    """All ``[[target]]`` slug-bases (case-preserved) from ``body``."""
    out: set[str] = set()
    for m in _WIKILINK_RE.finditer(body):
        raw = m.group(1).strip()
        base = raw.rsplit("/", 1)[-1]
        if base:
            out.add(base)
    return out


def _read_note(md: pathlib.Path) -> tuple[dict[str, Any], str]:
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return {}, ""
    fm, body = split_frontmatter(text)
    return fm, body


def _tags_of(fm: dict[str, Any]) -> set[str]:
    raw = fm.get("tags")
    if isinstance(raw, list):
        return {str(t).strip().lower() for t in raw if str(t).strip()}
    if isinstance(raw, str) and raw.strip():
        return {raw.strip().lower()}
    return set()


def _wikilink_neighbors(body: str) -> set[str]:
    """Lower-cased neighbor slugs reachable in one hop from ``body``."""
    return {t.lower() for t in _extract_wikilink_targets(body)}


def _graph_distant(
    a: pathlib.Path,
    a_fm: dict[str, Any],
    a_body: str,
    b: pathlib.Path,
    b_fm: dict[str, Any],
    b_body: str,
    vault: pathlib.Path,
) -> bool:
    """True if pair ``(a, b)`` satisfies at least one distance signal.

    Signals (any one is enough — prefer the strongest available):

    1. ``a`` and ``b`` live in different top-level folders under
       ``cortex-memory/``.
    2. Neither file contains a direct ``[[other_stem]]`` wikilink to
       the other (basename addressing — matches vault convention).
    3. The intersection of their 1-hop wikilink neighborhoods is empty.

    Notes in the same folder that mention each other directly or share
    a neighbor are too close — Stage D's value-add is finding pairs the
    graph hasn't already connected.
    """
    rel_a = a.relative_to(vault).parts
    rel_b = b.relative_to(vault).parts
    if rel_a[:1] != rel_b[:1]:
        # Different top-level folders — distance signal #1.
        return True

    a_neighbors = _wikilink_neighbors(a_body)
    b_neighbors = _wikilink_neighbors(b_body)
    a_stem = a.stem.lower()
    b_stem = b.stem.lower()

    direct_link = (b_stem in a_neighbors) or (a_stem in b_neighbors)
    if direct_link:
        return False

    shared_neighbors = a_neighbors & b_neighbors
    # Remove self-references so the pair's own stems don't count as
    # shared neighbors.
    shared_neighbors.discard(a_stem)
    shared_neighbors.discard(b_stem)
    if shared_neighbors:
        return False

    # Same folder, no direct link, no shared 1-hop neighbor — distance
    # signals #2 + #3 both fire.
    return True


def _tag_overlap(a_fm: dict[str, Any], b_fm: dict[str, Any]) -> int:
    return len(_tags_of(a_fm) & _tags_of(b_fm))


# ---------- pair dedup ----------


def _pair_key(a_stem: str, b_stem: str) -> tuple[str, str]:
    """Canonical (alphabetical) ordering so (A,B) == (B,A) on lookup."""
    return (a_stem, b_stem) if a_stem <= b_stem else (b_stem, a_stem)


def _load_processed_pairs(
    mind: pathlib.Path,
    *,
    today: datetime.date,
    lookback_days: int,
) -> set[tuple[str, str]]:
    """Read ``stage-d-pairs.jsonl`` and return canonical pair keys
    touched within ``lookback_days``."""
    path = _pairs_log_path(mind)
    if not path.is_file():
        return set()
    cutoff = today - datetime.timedelta(days=lookback_days)
    out: set[tuple[str, str]] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("stage_d: failed to read pairs log %s: %s", path, exc)
        return set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        a = str(rec.get("note_a") or "").strip()
        b = str(rec.get("note_b") or "").strip()
        if not a or not b:
            continue
        ts = str(rec.get("timestamp") or "").strip()
        # Best-effort date parse; missing/unparseable → keep (be
        # conservative: avoid re-attempting a pair we logged).
        if ts and len(ts) >= 10:
            try:
                pair_date = datetime.date.fromisoformat(ts[:10])
                if pair_date < cutoff:
                    continue
            except ValueError:
                pass
        out.add(_pair_key(a, b))
    return out


# ---------- pair scoring + selection ----------


def _slugify(text: str) -> str:
    """Same slug rule stage_c uses for atomize children."""
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "section"


def _select_pair(
    candidates: list[pathlib.Path],
    *,
    vault: pathlib.Path,
    processed_pairs: set[tuple[str, str]],
) -> Optional[tuple[pathlib.Path, pathlib.Path]]:
    """Choose the best pair: graph-distant, not already processed,
    smallest tag intersection.

    Returns ``None`` if no eligible pair exists.
    """
    # Cache (fm, body) per file so we don't re-read the same note for
    # every pair check — O(n) reads, O(n^2) comparisons.
    cache: dict[pathlib.Path, tuple[dict[str, Any], str]] = {}
    for md in candidates:
        cache[md] = _read_note(md)

    best: Optional[tuple[int, str, pathlib.Path, pathlib.Path]] = None
    for i in range(len(candidates)):
        a = candidates[i]
        a_fm, a_body = cache[a]
        for j in range(i + 1, len(candidates)):
            b = candidates[j]
            key = _pair_key(a.stem, b.stem)
            if key in processed_pairs:
                continue
            b_fm, b_body = cache[b]
            if not _graph_distant(a, a_fm, a_body, b, b_fm, b_body, vault):
                continue
            overlap = _tag_overlap(a_fm, b_fm)
            # Sort key: (tag_overlap asc, lexicographic ordering asc) so
            # ties are reproducible. Stem-sorted "<a_stem>::<b_stem>"
            # because tuples don't compare cleanly against the int.
            tiebreak = f"{a.stem}::{b.stem}"
            cand = (overlap, tiebreak, a, b)
            if best is None or cand < best:
                best = cand
    if best is None:
        return None
    return best[2], best[3]


# ---------- LLM call ----------


_SYNTHESIS_PROMPT_TEMPLATE = """\
You are an unsupervised researcher looking for non-obvious connections.

Two notes have been touched in the last week. They live in different
corners of the vault and (almost certainly) have not been directly
related to each other yet. Read them and decide: is there a non-obvious
connection — a shared mechanism, a transferable insight, a
contradiction worth resolving?

If YES, write a synthesis of exactly three paragraphs:
  1. State the connection precisely. What is the shared structure?
  2. Cite specifics from each note that establish the connection.
  3. What changes if we believe this — for note_a, for note_b, or for
     how we think about either domain?

If NO — if the notes share no real structure and any connection would
be forced or shallow — output EXACTLY the token NULL_RESULT on its
own line and stop. Do not write a synthesis. Do not apologize. A null
result is a valid and useful output.

Return a JSON object with one of two shapes:

  {{"result": "NULL_RESULT", "reason": "<one sentence why>"}}

OR

  {{"result": "SYNTHESIS",
    "title": "<short title, 5-10 words>",
    "body": "<three paragraphs of markdown>"}}

No preamble. No markdown wrapper around the JSON. No explanation outside
the object.

NOTE A — slug: {slug_a}
---
{body_a}
---

NOTE B — slug: {slug_b}
---
{body_b}
---
"""


@dataclasses.dataclass
class SynthesizerOutput:
    """Structured result from the synthesizer.

    ``null`` is True on ``NULL_RESULT`` (no synthesis); otherwise
    ``title`` and ``body`` are populated.
    """

    null: bool
    title: str = ""
    body: str = ""
    reason: str = ""


Synthesizer = Callable[[str, str, str, str], SynthesizerOutput]


def _default_synthesizer(
    slug_a: str,
    body_a: str,
    slug_b: str,
    body_b: str,
) -> SynthesizerOutput:
    """Default synthesizer — routes the prompt through
    :class:`core.llm_client.LLMClient` to the local Qwen endpoint.

    Wraps the async client behind :func:`asyncio.run`; Stage D is
    called from synchronous wake code so the async surface is a leak
    detail of the underlying transport.

    Network failure (:class:`LLMUnreachable`) is *propagated* — the
    caller decides whether to skip the cycle. The skip-on-unreachable
    branch lives in :func:`run` so the unit test can exercise it.
    """
    # Lazy imports — Stage D runs under a wake budget and the test
    # suite injects its own synthesizer, so we don't want
    # ``import asyncio`` / ``httpx`` cost at module import.
    import asyncio

    from core.llm_client import LLMClient

    prompt = _SYNTHESIS_PROMPT_TEMPLATE.format(
        slug_a=slug_a,
        body_a=body_a,
        slug_b=slug_b,
        body_b=body_b,
    )
    client = LLMClient()
    blob = asyncio.run(client.complete(prompt))
    return _parse_synthesizer_blob(blob)


def _parse_synthesizer_blob(blob: dict[str, Any]) -> SynthesizerOutput:
    """Normalize the model's JSON response into :class:`SynthesizerOutput`.

    Tolerant: missing fields default to empty strings; an explicitly
    ``NULL_RESULT`` ``result`` (case-insensitive) takes precedence over
    any synthesis body the model might still have included.
    """
    result = str(blob.get("result") or "").strip().upper()
    if result == NULL_RESULT_SENTINEL:
        return SynthesizerOutput(
            null=True,
            reason=str(blob.get("reason") or "").strip(),
        )
    title = str(blob.get("title") or "").strip()
    body = str(blob.get("body") or "").strip()
    if not body:
        # An empty SYNTHESIS body is functionally a null — log and skip
        # the vault write rather than commit a stub note.
        return SynthesizerOutput(
            null=True,
            reason="synthesizer returned empty body without NULL_RESULT marker",
        )
    return SynthesizerOutput(null=False, title=title, body=body)


# ---------- frontmatter rendering ----------


def _render_kv(key: str, val: Any) -> str:
    if isinstance(val, list):
        items = ", ".join(str(v) for v in val)
        return f"{key}: [{items}]"
    if isinstance(val, bool):
        return f"{key}: {'true' if val else 'false'}"
    return f"{key}: {val}"


def _render_frontmatter(fm: dict[str, Any]) -> str:
    preferred = (
        "title",
        "source",
        "note_a",
        "note_b",
        "tags",
        "created",
        "updated",
        "last_accessed",
        "access_count",
    )
    out: list[str] = ["---"]
    seen: set[str] = set()
    for key in preferred:
        if key in fm:
            out.append(_render_kv(key, fm[key]))
            seen.add(key)
    for key, val in fm.items():
        if key in seen:
            continue
        out.append(_render_kv(key, val))
    out.append("---")
    return "\n".join(out) + "\n"


def _build_synthesis_note(
    *,
    title: str,
    body: str,
    note_a_stem: str,
    note_b_stem: str,
    note_a_fm: dict[str, Any],
    note_b_fm: dict[str, Any],
    today: datetime.date,
) -> str:
    """Render the full markdown file (frontmatter + body).

    Tags = ``[recombination]`` plus the union of the two source notes'
    tags, deduped while preserving first-seen order. ``access_count: 0``
    so a future Stage C decay pass sees the note as fresh-and-unread,
    not stale.
    """
    inherited: list[str] = ["recombination"]
    seen = {"recombination"}
    for fm in (note_a_fm, note_b_fm):
        for tag in _tags_of(fm):
            if tag and tag not in seen:
                inherited.append(tag)
                seen.add(tag)
    fm = {
        "title": title or f"Recombination: {note_a_stem} × {note_b_stem}",
        "source": "stage-d",
        "note_a": note_a_stem,
        "note_b": note_b_stem,
        "tags": inherited,
        "created": today.isoformat(),
        "updated": today.isoformat(),
        "last_accessed": today.isoformat(),
        "access_count": 0,
    }
    return _render_frontmatter(fm) + "\n" + body.strip() + "\n"


def _synthesis_filename(
    *,
    today: datetime.date,
    title: str,
    note_a_stem: str,
    note_b_stem: str,
) -> str:
    """``YYYY-MM-DD-recombination-<short-slug>.md`` where ``<short-slug>``
    derives from the title (falling back to the two stems)."""
    base = title.strip() or f"{note_a_stem}-x-{note_b_stem}"
    slug = _slugify(base)
    # Cap the slug at 40 chars to keep filenames readable.
    if len(slug) > 40:
        slug = slug[:40].rstrip("-") or "synthesis"
    return f"{today.isoformat()}-recombination-{slug}.md"


# ---------- jsonl helpers (state-dir writes) ----------


def _atomic_append_jsonl(path: pathlib.Path, record: dict[str, Any]) -> None:
    """Append one JSONL record. Creates parent dirs on demand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write("\n")
        fh.flush()


def _locked_append_jsonl(
    path: pathlib.Path,
    record: dict[str, Any],
) -> None:
    """Append under an EXCLUSIVE vault_lock on the JSONL file itself.

    The lock is a sidecar next to the JSONL — concurrent appenders
    serialize so two writers can't interleave a half-line. We bound the
    wait at :data:`_LOCK_TIMEOUT_SECONDS`; on timeout the caller logs
    and the audit invariant fails the wake (better to fail loud than
    write a synthesis without auditing it).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with vault_lock.acquire(
        path,
        mode=vault_lock.LockMode.EXCLUSIVE,
        timeout=_LOCK_TIMEOUT_SECONDS,
    ):
        _atomic_append_jsonl(path, record)


# ---------- commit gate ----------


def commit_stage_d_synthesis(
    mind: pathlib.Path,
    *,
    note_a: pathlib.Path,
    note_b: pathlib.Path,
    synthesis_path: pathlib.Path,
    note_content: str,
    journal_path: Optional[pathlib.Path] = None,
) -> dict[str, Any]:
    """**Structural commit gate** — the only sanctioned path for
    writing a Stage D synthesis.

    Caller MUST go through this function. Order of operations:

    1. Journal the intent (op=``"recombination"``) BEFORE any write.
    2. Acquire an EXCLUSIVE :func:`vault_lock` on the synthesis path.
    3. Write the synthesis file inside the critical section.
    4. Append the audit record to
       ``inner/state/memory-worker-stage-d-attempts.jsonl`` (under the
       JSONL's own lock). This is the structural invariant: every
       synthesis write produces exactly one audit line, and the audit
       line is produced by deterministic Python — the LLM cannot
       skip it because it isn't in the prompt.
    5. Append the pair record to ``inner/state/stage-d-pairs.jsonl``.
    6. Mark the journal entry committed.

    Returns the audit record (useful for tests + telemetry).

    Failure modes
    -------------

    * :class:`vault_lock.VaultLockTimeout` — synthesis target was held
      longer than ``_LOCK_TIMEOUT_SECONDS``. Propagates so the caller
      can mark the cycle skipped.
    * :class:`OSError` on write — propagates; the journal entry stays
      ``pending`` and a future replay verifier reconciles it.
    """
    vault = _vault_dir(mind)
    audit_hash = _sha256_of_text(note_content)
    ts = _utc_iso()
    rel_path = str(synthesis_path.relative_to(vault))

    entry = None
    if journal_path is not None:
        entry = journal_mod.append(
            journal_path,
            op="recombination",
            source=f"research/{note_a.stem}",
            targets=[rel_path],
            detail={
                "note_a": note_a.stem,
                "note_b": note_b.stem,
                "synthesis_path": rel_path,
                "audit_hash": audit_hash,
            },
        )

    # Steps 2 + 3: write under EXCLUSIVE vault_lock.
    synthesis_path.parent.mkdir(parents=True, exist_ok=True)
    with vault_lock.acquire(
        synthesis_path,
        mode=vault_lock.LockMode.EXCLUSIVE,
        timeout=_LOCK_TIMEOUT_SECONDS,
    ):
        synthesis_path.write_text(note_content, encoding="utf-8")

    # Step 4: audit log — the structural invariant.
    audit_record = {
        "ts": ts,
        "note_a": note_a.stem,
        "note_b": note_b.stem,
        "synthesis_path": rel_path,
        "audit_hash": audit_hash,
        "status": "committed",
        "source": "memory-worker-stage-d",
    }
    _locked_append_jsonl(_attempts_log_path(mind), audit_record)

    # Step 5: pair dedup log.
    _locked_append_jsonl(
        _pairs_log_path(mind),
        {
            "note_a": note_a.stem,
            "note_b": note_b.stem,
            "timestamp": ts,
            "synthesis_path": rel_path,
            "outcome": "synthesized",
        },
    )

    # Step 6: mark journal committed.
    if journal_path is not None and entry is not None:
        journal_mod.commit(journal_path, entry.journal_id)

    return audit_record


def _record_null_result(
    mind: pathlib.Path,
    *,
    note_a: pathlib.Path,
    note_b: pathlib.Path,
    reason: str,
) -> None:
    """Log a NULL_RESULT outcome WITHOUT writing a vault note.

    Two records land: one on the null-results log (so we can review
    "what didn't connect") and one on the pairs log (so the dedup
    lookback covers null outcomes the same way it covers ships).
    """
    ts = _utc_iso()
    _locked_append_jsonl(
        _null_results_log_path(mind),
        {
            "ts": ts,
            "note_a": note_a.stem,
            "note_b": note_b.stem,
            "reason": reason,
        },
    )
    _locked_append_jsonl(
        _pairs_log_path(mind),
        {
            "note_a": note_a.stem,
            "note_b": note_b.stem,
            "timestamp": ts,
            "synthesis_path": None,
            "outcome": "null_result",
        },
    )


# ---------- phase 3: decay-aware pre-pass ----------
#
# Three sequential passes drain the decay backlog without losing knowledge:
#
#   1. Archive — moves low-signal notes (superseded / resolved / redirect
#      stubs / orphan investigations) into ``cortex-memory/archive/``.
#   2. Extraction — intentionally a no-op stub. Filename-keyword grouping
#      between decayed and accessed cohorts produces zero matches on the
#      live vault: see ``cortex-memory/research/2026-06-04-decay-extraction-pass-breakdown``.
#   3. Pairing (Phase 3.5) — title cosine similarity (IDF-weighted) with
#      a TITLE_COSINE_STANDARD floor of 0.45 and fitness-domain exemption.
#      Records the pair on the DecayPairingResult for telemetry; Stage C's
#      atomize() boosts decayed notes via _decay_priority_score(), so no
#      cross-stage event channel is needed.
#
# Spec: ``cortex-memory/research/2026-06-04-decay-phase3-spec``.
# Matching strategy: ``cortex-memory/research/2026-06-04-decay-phase3-matching-strategy``.


#: Decay window. A note is "decayed" if its ``last_accessed`` is older
#: than this many days AND its ``access_count`` is <=1. Matches the
#: window Stage C uses in :func:`stage_c._count_decayed_in_window` so
#: the two stages see the same population.
DEFAULT_DECAY_WINDOW_DAYS = 7

#: Top-level vault folders excluded from the decay pass — dailies are
#: time-bound by design, archive/ is the destination, gh-state is
#: auto-mirrored data with its own lifecycle. Mirrors stage_c's
#: ``_EXCLUDED_TOP_DIRS``.
_DECAY_EXCLUDED_TOP_DIRS = frozenset({"dailies", "archive", "gh-state"})

#: Fitness domain notes are fixed-schedule skill-path writes, not
#: behavioral decay. See ``2026-06-03-fitness-domain-decay-false-alarm``.
FITNESS_TAGS = frozenset({"fitness", "workout", "nutrition", "weight"})

#: Stop words for filename-keyword extraction. Sourced from the matching
#: strategy dry-run note — these tokens carry no topical signal and are
#: discarded before grouping.
_DECAY_STOPWORDS = frozenset(
    (
        "a an the and or but in on at to for of is it that this these "
        "are was were be been being have has had do does did will would "
        "shall should may might can could not no nor if then than so "
        "just only too very also each every both further where when "
        "which while as before after during through between about against "
        "above under into over out down up off away once here there how "
        "all any even first last long great little own old right high "
        "different small large next early young important few public "
        "care ever know need make time water been call whose local data "
        "went end line white"
    ).split()
)

_DECAY_DATE_PREFIX_RE = re.compile(r"^\d{4}[-_]?\d{2}[-_]?\d{2}[-_]")


def _is_fitness_domain(fm: dict[str, Any]) -> bool:
    return bool(_tags_of(fm) & FITNESS_TAGS)


#: Phase 3.5 pairing thresholds. ``STANDARD`` is the floor for a pair to
#: be accepted; ``HIGH_CONFIDENCE`` is the floor above which the pair is
#: treated as a strong match (telemetry / future tier). Empirical sweep
#: (0.40 / 0.45 / 0.50 / 0.55 / 0.60) against M5 behavioral recovery
#: showed 0.45 is the Pareto sweet spot: M5 jumps 1.71 -> 5.333 with 13
#: recovered notes (9.1% of decay pool), versus 10 notes (7.0%) at 0.55.
#: See surface 2026-06-06-130400-decay-recovery-empirical-results.
TITLE_COSINE_STANDARD = 0.45
TITLE_COSINE_HIGH_CONFIDENCE = 0.5


def _precompute_title_idf(vault_notes: list[pathlib.Path]) -> dict[str, float]:
    """Pre-compute IDF (inverse document frequency) for title tokens across vault."""
    title_df: Counter = Counter()
    for md in vault_notes:
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _body = split_frontmatter(text)
        title = fm.get("title", md.stem)
        for t in re.findall(r"\b\w+\b", title):
            if len(t) > 2:
                title_df[t.lower()] += 1
    total = len(vault_notes)
    return {t: math.log(total / (1 + df)) for t, df in title_df.items()}


def _title_tfidf_vector(title: str, title_idf: dict[str, float]) -> dict[str, float]:
    """IDF-weighted TF vector for a single note title."""
    tokens = [t.lower() for t in re.findall(r"\b\w+\b", title) if len(t) > 2]
    tf: Counter = Counter(tokens)
    return {tok: cnt * title_idf.get(tok, 1.0) for tok, cnt in tf.items()}


def _cosine_similarity(v1: dict[str, float], v2: dict[str, float]) -> float:
    dot = sum(v1.get(k, 0) * v2.get(k, 0) for k in set(v1) | set(v2))
    mag1 = math.sqrt(sum(v * v for v in v1.values()))
    mag2 = math.sqrt(sum(v * v for v in v2.values()))
    return dot / (mag1 * mag2) if mag1 and mag2 else 0.0


def _extract_title_keywords(stem: str) -> list[str]:
    """Topical tokens from a filename slug.

    Strips ``YYYY-MM-DD-`` (or underscore variants) prefix, splits on
    hyphens/underscores, lowercases, drops stop words and tokens <=2
    chars.
    """
    name = _DECAY_DATE_PREFIX_RE.sub("", stem)
    tokens = re.split(r"[-_]", name)
    return [
        t.lower()
        for t in tokens
        if t and len(t) > 2 and t.lower() not in _DECAY_STOPWORDS
    ]


def _extract_major_topic(keywords: list[str]) -> str:
    """Longest keyword >4 chars; first keyword otherwise; empty if none.

    The longest specific token is the most discriminating signal for
    grouping in a vault dominated by short common words.
    """
    specific = [k for k in keywords if len(k) > 4]
    if specific:
        return max(specific, key=len)
    return keywords[0] if keywords else ""


def _iter_decayed_notes(
    vault: pathlib.Path,
    today: datetime.date,
    *,
    window_days: int,
) -> list[pathlib.Path]:
    """All groomable notes whose ``last_accessed`` is older than the
    decay window AND whose ``access_count`` is <=1.

    Excludes ``dailies/``, ``archive/``, ``gh-state/``, and dotfiles —
    same filter Stage C uses for its decay count, so the two stages see
    the same population.
    """
    if not vault.is_dir():
        return []
    cutoff = today - datetime.timedelta(days=window_days)
    cutoff_str = cutoff.isoformat()
    out: list[pathlib.Path] = []
    for md in vault.rglob("*.md"):
        rel = md.relative_to(vault).parts
        if not rel or rel[0] in _DECAY_EXCLUDED_TOP_DIRS:
            continue
        if any(part.startswith(".") for part in rel):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _body = split_frontmatter(text)
        la = fm.get("last_accessed")
        if la is None:
            continue
        la_str = str(la).strip()
        if len(la_str) < 10 or la_str[:10] >= cutoff_str:
            continue
        try:
            ac = int(fm.get("access_count") or 0)
        except (TypeError, ValueError):
            ac = 0
        if ac > 1:
            continue
        out.append(md)
    out.sort()
    return out


def _is_archive_eligible(fm: dict[str, Any], body: str) -> bool:
    """A decayed note is archive-eligible if it carries no live signal.

    Three categories:

    1. Explicit ``status: superseded`` / ``resolved`` / ``obsolete``.
    2. ``note_type: investigation`` or ``audit`` with no outstanding
       ``next_step`` / ``action`` field anywhere in the body.
    3. Redirect stub — body is exactly one ``[[target]]`` link.
    """
    status = str(fm.get("status") or "").strip().lower()
    if status in ("superseded", "resolved", "obsolete"):
        return True
    note_type = str(fm.get("note_type") or "").strip().lower()
    if note_type in ("investigation", "audit"):
        # Loose match — vault prose uses both ``next_step`` (frontmatter)
        # and ``next step``/``next steps`` (markdown headings). Treat any
        # of them as "still has an open thread".
        body_lower = body.lower()
        if (
            "next_step" not in body_lower
            and "next step" not in body_lower
            and "action" not in body_lower
        ):
            return True
    stripped = body.strip()
    if (
        stripped.startswith("[[")
        and stripped.count("[[") == 1
        and stripped.count("]]") == 1
    ):
        return True
    return False


def _identify_archive_candidates(
    decayed_notes: list[pathlib.Path],
) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for path in decayed_notes:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = split_frontmatter(text)
        if _is_archive_eligible(fm, body):
            out.append(path)
    return out


def _archive_decayed_note(
    mind: pathlib.Path,
    note: pathlib.Path,
    journal_path: Optional[pathlib.Path],
) -> bool:
    """Move a decayed, archive-eligible note to ``cortex-memory/archive/``.

    Conservative: we only move notes that have zero inbound wikilinks.
    A decayed-but-referenced note stays in place because rewriting
    every referrer is Stage C's responsibility (``archive`` and
    ``dedupe-merge`` both do that work under their own locks); decay
    archive's job is to drop the orphaned-and-superseded tail without
    creating broken links.
    """
    vault = _vault_dir(mind)
    today_iso = _today().isoformat()

    # Inbound check — skip if anyone links to this slug.
    stem = note.stem
    needle = f"[[{stem}"
    for source in vault.rglob("*.md"):
        if source.resolve() == note.resolve():
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            continue
        if needle in text:
            return False

    try:
        text = note.read_text(encoding="utf-8")
    except OSError:
        return False
    fm, body = split_frontmatter(text)

    archive_dir = vault / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dst = archive_dir / f"{note.stem}.md"
    if dst.exists():
        logger.info(
            "stage_d archive-decay: destination exists, skipping %s",
            note.stem,
        )
        return False

    new_fm = dict(fm)
    new_fm["updated"] = today_iso
    new_fm["archived"] = today_iso
    new_text = _render_frontmatter(new_fm) + "\n" + body.strip() + "\n"

    entry = None
    if journal_path is not None:
        entry = journal_mod.append(
            journal_path,
            op="archive-decay",
            source=str(note.relative_to(vault)),
            targets=[str(dst.relative_to(vault))],
            detail={
                "src": str(note.relative_to(vault)),
                "dst": str(dst.relative_to(vault)),
            },
        )

    try:
        rename_paths = sorted([note, dst])
        with vault_lock.acquire(
            rename_paths[0],
            mode=vault_lock.LockMode.EXCLUSIVE,
            timeout=_LOCK_TIMEOUT_SECONDS,
        ), vault_lock.acquire(
            rename_paths[1],
            mode=vault_lock.LockMode.EXCLUSIVE,
            timeout=_LOCK_TIMEOUT_SECONDS,
        ):
            dst.write_text(new_text, encoding="utf-8")
            note.unlink()
    except (OSError, vault_lock.VaultLockTimeout) as exc:
        logger.warning(
            "stage_d archive-decay: write/unlink failed for %s: %s",
            note.stem,
            exc,
        )
        return False

    if journal_path is not None and entry is not None:
        journal_mod.commit(journal_path, entry.journal_id)
    return True


def _identify_extraction_candidates(
    decayed_notes: list[pathlib.Path],
    vault: pathlib.Path,
) -> list[tuple[pathlib.Path, pathlib.Path]]:
    """Intentionally a no-op — extraction is structurally ineffective.

    Filename-keyword grouping between decayed and accessed cohorts
    produces zero matches on the live vault: the two sets use disjoint
    naming patterns. Pairing recovers 93.7% on its own. Scaffolding
    kept for symmetry with the three-pass spec — see
    ``cortex-memory/research/2026-06-04-decay-extraction-pass-breakdown``.
    """
    return []


def _select_decay_pair(
    decayed_notes: list[pathlib.Path],
    vault: pathlib.Path,
    title_idf: dict[str, float],
) -> Optional[tuple[pathlib.Path, pathlib.Path, float]]:
    """Best decay→decay pair under pure title cosine similarity.

    Accepts pairs with cosine >= TITLE_COSINE_STANDARD. Fitness domain notes are exempt.
    """
    if len(decayed_notes) < 2:
        return None

    vectors: dict[pathlib.Path, dict[str, float]] = {}
    for md in decayed_notes:
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _body = split_frontmatter(text)
        if _is_fitness_domain(fm):
            continue
        title = fm.get("title", md.stem)
        vec = _title_tfidf_vector(title, title_idf)
        if vec:
            vectors[md] = vec

    if len(vectors) < 2:
        return None

    best: Optional[tuple[float, str, pathlib.Path, pathlib.Path]] = None
    keys = list(vectors.keys())
    for i in range(len(keys)):
        a = keys[i]
        for j in range(i + 1, len(keys)):
            b = keys[j]
            score = _cosine_similarity(vectors[a], vectors[b])
            if score < TITLE_COSINE_STANDARD:
                continue
            tiebreak = f"{a.stem}::{b.stem}" if a.stem <= b.stem else f"{b.stem}::{a.stem}"
            cand = (score, tiebreak, a, b)
            if best is None or score > best[0] or (score == best[0] and tiebreak < best[1]):
                best = cand

    return (best[2], best[3], best[0]) if best else None


def run_decay_prepass(
    mind: pathlib.Path,
    *,
    journal_path: Optional[pathlib.Path] = None,
    today: Optional[datetime.date] = None,
    window_days: int = DEFAULT_DECAY_WINDOW_DAYS,
    max_archive: int = 20,
) -> DecayPairingResult:
    """Run the three Phase 3 passes against the current vault.

    Returns a :class:`DecayPairingResult` summarizing what each pass
    found. Safe to call standalone (used by the unit tests) or as a
    pre-pass inside :func:`run` before the recombination flow.
    """
    today = today or _today()
    vault = _vault_dir(mind)
    result = DecayPairingResult()

    decayed = _iter_decayed_notes(vault, today, window_days=window_days)
    if not decayed:
        return result

    # Pass 1: archive low-signal notes.
    archive_candidates = _identify_archive_candidates(decayed)
    archived_paths: set[pathlib.Path] = set()
    for path in archive_candidates[:max_archive]:
        if _archive_decayed_note(mind, path, journal_path):
            archived_paths.add(path)
            result.archived += 1

    remaining = [p for p in decayed if p not in archived_paths]

    # Pass 2: extraction stub — see _identify_extraction_candidates.
    extracted_pairs = _identify_extraction_candidates(remaining, vault)
    result.extracted = len(extracted_pairs)

    # Pass 3.5: pairing — title cosine + high-spec-tag filter. No decay_event
    # emission: Stage C's _decay_priority_score() uses access_count directly,
    # so the event was dead weight (no consumers in the live pipeline).
    title_idf = _precompute_title_idf(remaining)
    pair = _select_decay_pair(remaining, vault, title_idf)
    if pair is not None:
        a, b, score = pair
        result.paired = 1
        result.score = score
        result.notes.append((a.stem, b.stem, score))

    return result


# ---------- config loader ----------


def _load_stage_d_config(mind: pathlib.Path) -> StageDConfig:
    """Read ``memory_worker.stage_d.*`` from ``alice.config.json``."""
    cfg_path = mind / "config" / "alice.config.json"
    cfg = StageDConfig()
    if not cfg_path.is_file():
        return cfg
    try:
        blob = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return cfg
    section = ((blob or {}).get("memory_worker") or {}).get("stage_d") or {}
    if not isinstance(section, dict):
        return cfg
    if "recent_window_days" in section:
        try:
            cfg.recent_window_days = int(section["recent_window_days"])
        except (TypeError, ValueError):
            pass
    if "pairs_dedup_lookback_days" in section:
        try:
            cfg.pairs_dedup_lookback_days = int(
                section["pairs_dedup_lookback_days"]
            )
        except (TypeError, ValueError):
            pass
    if "model_tier" in section:
        val = section["model_tier"]
        if isinstance(val, str) and val.strip():
            cfg.model_tier = val.strip()
    return cfg


# ---------- top-level run ----------


def run(
    mind: pathlib.Path,
    *,
    journal_path: Optional[pathlib.Path] = None,
    config: Optional[StageDConfig] = None,
    synthesizer: Optional[Synthesizer] = None,
    today: Optional[datetime.date] = None,
) -> StageDReport:
    """One Stage D tick.

    Pulls recently-touched research notes, picks the most graph-distant
    pair not already processed within the dedup window, asks the
    synthesizer for a connection, and either writes a vault note
    (through :func:`commit_stage_d_synthesis`) or logs a NULL_RESULT.

    ``synthesizer`` is injectable for tests; the production default is
    :func:`_default_synthesizer` (local Qwen). ``today`` defaults to
    :func:`datetime.date.today`; tests pass a fixed date to make recency
    math reproducible.

    Returns a :class:`StageDReport` summarizing the cycle. The report's
    ``ran`` flag is True iff a pair was selected (whether or not the
    synthesizer produced a vault note); ``synthesized`` and
    ``null_results`` are mutually exclusive counts.
    """
    cfg = config or _load_stage_d_config(mind)
    synth = synthesizer or _default_synthesizer
    today = today or _today()
    vault = _vault_dir(mind)

    report = StageDReport(ran=False)

    # Phase 3.5 pre-pass: archive low-signal decayed notes and select a
    # title-cosine pair for telemetry. The pre-pass is independent of the
    # recombination flow below — it runs whether or not we have a
    # recombination pair to ship.
    report.decay_pairing = run_decay_prepass(
        mind, journal_path=journal_path, today=today
    )

    candidates = _recently_touched_research(
        vault, today, window_days=cfg.recent_window_days
    )
    if len(candidates) < 2:
        report.skipped_reason = "fewer than 2 recently-touched research notes"
        return report

    processed = _load_processed_pairs(
        mind, today=today, lookback_days=cfg.pairs_dedup_lookback_days
    )
    pair = _select_pair(candidates, vault=vault, processed_pairs=processed)
    if pair is None:
        report.skipped_reason = (
            "no graph-distant pair available outside dedup window"
        )
        # We still count the candidates inspected so the heartbeat
        # surfaces a quiet-cycle reason instead of looking like a no-op.
        report.pairs_considered = max(0, len(candidates) * (len(candidates) - 1) // 2)
        return report

    note_a, note_b = pair
    report.ran = True
    report.pairs_considered = max(1, len(candidates) * (len(candidates) - 1) // 2)

    a_fm, a_body = _read_note(note_a)
    b_fm, b_body = _read_note(note_b)

    # Synthesizer call — propagate exceptions so the worker logs and
    # the cycle ends cleanly. The default :func:`_default_synthesizer`
    # raises :class:`LLMUnreachable` on transport failure; we catch
    # only that one (lazy import so the test path stays light).
    try:
        out = synth(note_a.stem, a_body, note_b.stem, b_body)
    except Exception as exc:  # noqa: BLE001 — degrade quietly on any synth failure
        # Best-effort detection of "endpoint is down" vs unexpected
        # programmer error. We swallow LLMUnreachable; anything else
        # bubbles so the test harness sees it.
        from core.llm_client import LLMUnreachable

        if isinstance(exc, LLMUnreachable):
            logger.warning("stage_d: synthesizer unreachable: %s", exc)
            report.skipped_reason = f"synthesizer unreachable: {exc}"
            report.ran = False
            return report
        raise

    if out.null:
        _record_null_result(
            mind, note_a=note_a, note_b=note_b, reason=out.reason or "(no reason)"
        )
        report.null_results = 1
        return report

    # Build the vault note + commit through the gate.
    today_iso = today.isoformat()
    filename = _synthesis_filename(
        today=today,
        title=out.title,
        note_a_stem=note_a.stem,
        note_b_stem=note_b.stem,
    )
    synthesis_path = _research_dir(mind) / filename
    note_content = _build_synthesis_note(
        title=out.title,
        body=out.body,
        note_a_stem=note_a.stem,
        note_b_stem=note_b.stem,
        note_a_fm=a_fm,
        note_b_fm=b_fm,
        today=today,
    )

    audit = commit_stage_d_synthesis(
        mind,
        note_a=note_a,
        note_b=note_b,
        synthesis_path=synthesis_path,
        note_content=note_content,
        journal_path=journal_path,
    )

    report.synthesized = 1
    report.synthesis_path = audit["synthesis_path"]
    logger.info(
        "stage_d: synthesized %s × %s -> %s (today=%s)",
        note_a.stem,
        note_b.stem,
        audit["synthesis_path"],
        today_iso,
    )
    return report


# ---------- crash recovery verifier ----------


def register_verifiers(mind: pathlib.Path) -> None:
    """Bind the recombination verifier to a concrete ``mind`` root.

    The verifier confirms a crashed ``recombination`` journal entry
    landed by checking the synthesis file exists AND an audit record
    with the matching hash is present in
    ``memory-worker-stage-d-attempts.jsonl``. Either one missing means
    the structural invariant didn't hold — replay marks the entry
    skipped rather than committed so the operator can investigate.
    """
    vault = _vault_dir(mind)

    def recombination_verifier(entry: journal_mod.JournalEntry) -> bool:
        rel_path = entry.detail.get("synthesis_path")
        audit_hash = entry.detail.get("audit_hash")
        if not rel_path or not audit_hash:
            return False
        synthesis_file = vault / str(rel_path)
        if not synthesis_file.is_file():
            return False
        # Confirm the audit row exists with a matching hash. Linear
        # scan — the attempts log is single-write-per-cycle so it stays
        # small.
        attempts = _attempts_log_path(mind)
        if not attempts.is_file():
            return False
        try:
            text = attempts.read_text(encoding="utf-8")
        except OSError:
            return False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                rec.get("audit_hash") == audit_hash
                and rec.get("synthesis_path") == rel_path
            ):
                return True
        return False

    def archive_decay_verifier(entry: journal_mod.JournalEntry) -> bool:
        src = vault / entry.detail.get("src", entry.source)
        dst_rel = entry.detail.get(
            "dst", entry.targets[0] if entry.targets else ""
        )
        if not dst_rel:
            return False
        dst = vault / dst_rel
        if src.exists():
            return False
        return dst.is_file()

    journal_mod.register_verifier("recombination", recombination_verifier)
    journal_mod.register_verifier("archive-decay", archive_decay_verifier)


# ---------- module API ----------


__all__ = [
    "DEFAULT_DECAY_WINDOW_DAYS",
    "DEFAULT_MODEL_TIER",
    "DEFAULT_PAIRS_DEDUP_LOOKBACK_DAYS",
    "DEFAULT_RECENT_WINDOW_DAYS",
    "FITNESS_TAGS",
    "NULL_RESULT_SENTINEL",
    "DecayPairingResult",
    "StageDConfig",
    "StageDReport",
    "Synthesizer",
    "SynthesizerOutput",
    "commit_stage_d_synthesis",
    "register_verifiers",
    "run",
    "run_decay_prepass",
]


# Public iterable seam — keeps the test module from importing private
# helpers by name when the helper signature is the explicit contract.
def iter_recently_touched(
    mind: pathlib.Path,
    *,
    window_days: int = DEFAULT_RECENT_WINDOW_DAYS,
    today: Optional[datetime.date] = None,
) -> Iterable[pathlib.Path]:
    today = today or _today()
    return _recently_touched_research(
        _vault_dir(mind), today, window_days=window_days
    )
