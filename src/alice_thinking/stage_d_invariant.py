"""Stage D commit invariant — second-layer check.

The structural fix for "thinking writes a stage-d note without going
through the judge gate" is :func:`alice_thinking.stage_d_pipeline.commit_stage_d_synthesis`.
This module is the belt-and-suspenders complement: scan the vault for
notes that claim ``source: stage-d`` in frontmatter and verify each one
has either an ``stage-d-attempts.jsonl`` entry (judges ran) or a
``stage-d-judge-failures.jsonl`` entry (judges errored, vault write was
the documented fallback).

A note with neither is an end-run around the gate — almost certainly
the legacy direct-write path that motivated the redesign. The wake
prompt should call :func:`find_unaudited_stage_d_notes` at close-clean
and surface anything it returns.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
from typing import Optional

from alice_thinking.stage_d_pipeline import (
    DEFAULT_ATTEMPTS_LOG,
    DEFAULT_JUDGE_FAILURES_LOG,
    DEFAULT_VAULT_ROOT,
)


# Minimal frontmatter parser — only the keys we need. Avoids a YAML
# dependency in thinking's hot path. Stage D frontmatter uses simple
# scalar values for source/note_a/note_b so a regex over the leading
# ``---\n...\n---\n`` block is sufficient and predictable.
_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def _parse_frontmatter_subset(text: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Return ``{key: stripped_value}`` for any of ``keys`` found in the
    leading frontmatter block. Missing keys are simply absent from the
    result. Values are stripped of surrounding quotes/whitespace."""
    m = _FM_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        km = _KEY_RE.match(line)
        if not km:
            continue
        k = km.group(1)
        if k not in keys:
            continue
        v = km.group(2).strip().strip("'").strip('"')
        out[k] = v
    return out


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def find_unaudited_stage_d_notes(
    *,
    date: Optional[_dt.date] = None,
    vault_root: pathlib.Path = DEFAULT_VAULT_ROOT,
    attempts_log_path: pathlib.Path = DEFAULT_ATTEMPTS_LOG,
    judge_failures_log_path: pathlib.Path = DEFAULT_JUDGE_FAILURES_LOG,
) -> list[dict]:
    """Return a list of vault notes that claim Stage D provenance but
    lack either an attempts.jsonl ship record or a judge-failures.jsonl
    fallback record.

    Parameters
    ----------
    date
        Restrict the scan to notes whose ``created`` frontmatter equals
        this ISO date (``YYYY-MM-DD``). Defaults to today in local time.
        ``None`` of the caller's choosing — to scan all dates pass a
        sentinel via ``date=_dt.date.min``; the resulting comparison
        will let everything through (and the function is fine to call
        broadly).
    vault_root
        Root of the cortex-memory vault.
    attempts_log_path
        Firehose log written by ``run_dual_judge``. A note is audited if
        any line has ``shipped_slug == "research/<note-slug>"``.
    judge_failures_log_path
        Fallback log. A note is audited if a line's
        ``(slug_a, slug_b)`` matches the note's ``note_a``/``note_b``
        (in either order).

    Returns
    -------
    list[dict]
        One entry per unaudited note, shape:
        ``{"slug": str, "path": str, "note_a": str|None, "note_b": str|None,
        "created": str|None}``. Empty when everything checks out.
    """
    target_date = date if date is not None else _dt.date.today()
    target_iso = target_date.isoformat()

    attempts = _read_jsonl(attempts_log_path)
    shipped_slugs = {
        rec.get("shipped_slug")
        for rec in attempts
        if rec.get("shipped_slug")
    }

    failures = _read_jsonl(judge_failures_log_path)
    failed_pairs: set[frozenset[str]] = {
        frozenset((rec.get("slug_a") or "", rec.get("slug_b") or ""))
        for rec in failures
        if rec.get("slug_a") and rec.get("slug_b")
    }

    research = vault_root / "research"
    if not research.is_dir():
        return []

    unaudited: list[dict] = []
    for md in sorted(research.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter_subset(
            text, keys=("source", "note_a", "note_b", "created")
        )
        if fm.get("source") != "stage-d":
            continue
        # Honor date filter when set (date.min lets everything through).
        if target_date != _dt.date.min:
            if (fm.get("created") or "").strip() != target_iso:
                continue

        note_slug = md.stem
        expected_shipped = f"research/{note_slug}"
        note_a = fm.get("note_a") or None
        note_b = fm.get("note_b") or None

        audited = False
        if expected_shipped in shipped_slugs:
            audited = True
        if not audited and note_a and note_b:
            if frozenset((note_a, note_b)) in failed_pairs:
                audited = True

        if not audited:
            unaudited.append(
                {
                    "slug": note_slug,
                    "path": str(md),
                    "note_a": note_a,
                    "note_b": note_b,
                    "created": fm.get("created"),
                }
            )

    return unaudited
