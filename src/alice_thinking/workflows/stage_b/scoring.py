"""Deterministic candidate scoring for ``pick_grooming_target`` (Step 3).

Per the design sketch, scoring is deterministic so that — given the
same vault state — the workflow always picks the same target. The
three criteria are:

- staleness: ``updated:`` frontmatter > 14 days ago (+1 per criterion met)
- low access: ``access_count:`` < 2 (+1 per criterion met)
- recently inbox-touched: any *consumed* note name shares a slug-ish
  token with the candidate (+1)

Tied highest-score candidates resolve lexicographically.

This module reads the filesystem only — no LLM calls.
"""

from __future__ import annotations

import datetime as _dt
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Iterable, Optional


__all__ = ["score_candidates", "Candidate", "STALENESS_DAYS", "LOW_ACCESS_THRESHOLD"]


STALENESS_DAYS = 14
LOW_ACCESS_THRESHOLD = 2


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class Candidate:
    """One scored grooming candidate."""

    path: pathlib.Path
    score: int
    is_stale: bool
    is_low_access: bool
    is_inbox_touched: bool


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Cheap YAML-ish frontmatter parser — same shape as
    :func:`alice_thinking.phase._parse_frontmatter`.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        out[key.strip()] = raw.strip().strip('"').strip("'")
    return out


def _parse_date(value: str) -> Optional[_dt.date]:
    if not value:
        return None
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    try:
        return _dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _slugs_from_consumed(consumed_root: pathlib.Path) -> set[str]:
    """Pull token-ish slugs from the *most recent* consumed-note dir.

    "Recently inbox-touched" maps to: the consumed-notes dir whose
    name is the most recent date — those are the notes from the most
    recent drain.
    """
    if not consumed_root.is_dir():
        return set()
    try:
        date_dirs = sorted(
            (p for p in consumed_root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        return set()
    if not date_dirs:
        return set()
    latest = date_dirs[0]
    slugs: set[str] = set()
    try:
        for entry in latest.iterdir():
            if not entry.is_file():
                continue
            stem = entry.stem.lower()
            # Strip a leading date prefix if present.
            stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem)
            for tok in re.split(r"[-_\s]+", stem):
                if len(tok) >= 4:
                    slugs.add(tok)
    except OSError:
        return set()
    return slugs


def _candidate_paths(vault_dir: pathlib.Path) -> Iterable[pathlib.Path]:
    """Yield candidate vault notes — research, people, projects,
    reference, feedback, sources. Skip dailies (never atomize/groom),
    conflicts (own dispatcher), and hidden folders.
    """
    if not vault_dir.is_dir():
        return []
    skip_dirs = {"dailies", "conflicts"}
    out: list[pathlib.Path] = []
    try:
        for top in os.scandir(vault_dir):
            if top.name.startswith(".") or not top.is_dir():
                continue
            if top.name in skip_dirs:
                continue
            for root, dirs, files in os.walk(top.path):
                # Don't walk hidden dirs.
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fn in files:
                    if fn.endswith((".md", ".markdown")):
                        out.append(pathlib.Path(root) / fn)
    except OSError:
        return []
    return out


def score_candidates(
    *,
    vault_dir: pathlib.Path,
    consumed_root: Optional[pathlib.Path] = None,
    now: Optional[_dt.datetime] = None,
    staleness_days: int = STALENESS_DAYS,
    low_access_threshold: int = LOW_ACCESS_THRESHOLD,
) -> list[Candidate]:
    """Score every candidate vault note.

    Returns ``Candidate`` records sorted by ``(-score, path-lex-asc)``
    so the head is the highest-scoring lexicographically-first
    candidate. Empty list when the vault dir doesn't exist or has no
    candidate notes.
    """
    now = now or _dt.datetime.now()
    today = now.date()
    consumed_slugs: set[str] = set()
    if consumed_root is not None:
        consumed_slugs = _slugs_from_consumed(consumed_root)

    out: list[Candidate] = []
    for path in _candidate_paths(vault_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        updated = _parse_date(fm.get("updated", ""))
        try:
            access_count = int(fm.get("access_count", "0") or 0)
        except ValueError:
            access_count = 0

        is_stale = updated is not None and (today - updated).days >= staleness_days
        is_low_access = access_count < low_access_threshold

        # Inbox-touched: any consumed-note slug appears in the path stem.
        path_stem = path.stem.lower()
        path_stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path_stem)
        path_tokens = set(re.split(r"[-_\s]+", path_stem))
        is_inbox_touched = bool(path_tokens & consumed_slugs)

        score = sum(
            (
                1 if is_stale else 0,
                1 if is_low_access else 0,
                1 if is_inbox_touched else 0,
            )
        )
        if score == 0:
            # Don't return notes that match no criteria — there's
            # nothing to groom.
            continue
        out.append(
            Candidate(
                path=path,
                score=score,
                is_stale=is_stale,
                is_low_access=is_low_access,
                is_inbox_touched=is_inbox_touched,
            )
        )

    out.sort(key=lambda c: (-c.score, str(c.path)))
    return out
