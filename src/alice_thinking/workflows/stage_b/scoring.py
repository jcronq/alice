"""Deterministic candidate scoring for ``pick_grooming_target``.

Three criteria, +1 each: staleness (``updated:`` >14 days),
low-access (``access_count:`` < 2), recently-inbox-touched (any
consumed-note slug shares a token with the candidate stem).
Tied scores resolve lexicographically. Filesystem-only — no LLM.
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
    path: pathlib.Path
    score: int
    is_stale: bool
    is_low_access: bool
    is_inbox_touched: bool


def _parse_frontmatter(text: str) -> dict[str, str]:
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
    """Tokens from the most-recent ``inner/notes/.consumed/<date>/`` dir."""
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
    slugs: set[str] = set()
    try:
        for entry in date_dirs[0].iterdir():
            if not entry.is_file():
                continue
            stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", entry.stem.lower())
            for tok in re.split(r"[-_\s]+", stem):
                if len(tok) >= 4:
                    slugs.add(tok)
    except OSError:
        return set()
    return slugs


def _candidate_paths(vault_dir: pathlib.Path) -> Iterable[pathlib.Path]:
    """Walk vault subdirs (research/people/projects/reference/feedback/sources).
    Skip dailies + conflicts (own dispatchers) and hidden folders.
    """
    if not vault_dir.is_dir():
        return []
    skip = {"dailies", "conflicts"}
    out: list[pathlib.Path] = []
    try:
        for top in os.scandir(vault_dir):
            if top.name.startswith(".") or not top.is_dir() or top.name in skip:
                continue
            for root, dirs, files in os.walk(top.path):
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
    """Sorted ``Candidate`` list — head is the highest-scoring lex-first
    candidate. Empty when nothing scores >0.
    """
    now = now or _dt.datetime.now()
    today = now.date()
    consumed_slugs: set[str] = (
        _slugs_from_consumed(consumed_root) if consumed_root is not None else set()
    )

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
        path_stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", path.stem.lower())
        is_inbox_touched = bool(set(re.split(r"[-_\s]+", path_stem)) & consumed_slugs)

        score = (1 if is_stale else 0) + (1 if is_low_access else 0) + (
            1 if is_inbox_touched else 0
        )
        if score == 0:
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
