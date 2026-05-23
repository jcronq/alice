"""Keyword-based ``art:*`` classifier for SM dispatcher draft entry (EC-2).

Issue #294 — when a draft issue arrives without an ``art:*`` label, the
dispatcher's trust filter (:mod:`alice_forge.dispatcher.trust`) rejects it
and the triage surface never fires; the issue sits silently at
``sm:draft`` until a human notices. This module ships a lightweight
in-process keyword classifier that fills in a best-guess ``art:*`` label
at draft entry. If no keyword matches the issue title/body, we fall back
to the sentinel ``art:pending`` (whitelisted in
:data:`alice_forge.dispatcher.constants.ART_LABEL_WHITELIST` so the
triage surface still fires and Speaking/Thinking can re-label).

Design note: ``cortex-memory/designs/2026-05-22-issue294-art-classifier.md``.
"""

from __future__ import annotations


# Keyword tables — ordering matters: ties in :func:`auto_label` fall back to
# dict insertion order (bug → enhancement → research_note → design). The
# lists are deliberately small/curated; long-term these belong in
# ``alice.config.json`` under ``dispatcher.art_classifier.keywords`` (see
# Open Question #2 in the design note) but a code-resident table is the
# minimum viable cut.
_KEYWORDS: dict[str, list[str]] = {
    "art:bug": [
        "bug",
        "broken",
        "fail",
        "error",
        "crash",
        "race",
        "stall",
        "timeout",
        "unexpected",
        "incorrect",
        "missing",
        "stale",
        "silent",
        "regression",
        "stuck",
        "fix",
    ],
    "art:enhancement": [
        "feature",
        "improve",
        "add",
        "support",
        "better",
        "speed",
        "performance",
        "ergonomics",
        "optimize",
    ],
    "art:research_note": [
        "research",
        "investigate",
        "explore",
        "survey",
        "analysis",
        "study",
        "audit",
        "review",
    ],
    "art:design": [
        "design",
        "architecture",
        "spec",
        "pattern",
        "protocol",
        "contract",
        "interface",
        "model",
        "state machine",
    ],
}


def auto_label(
    title: str, body: str, existing_labels: list[str]
) -> str | None:
    """Classify an unlabelled draft issue.

    Parameters
    ----------
    title:
        The GitHub issue title. Matches in the title count 2× because the
        title is a distilled signal of intent.
    body:
        The GitHub issue body. May be empty.
    existing_labels:
        Current labels on the issue (bare names, not ``{"name": ...}``
        dicts). If any ``art:*`` label is already present, this function
        is a no-op and returns ``None`` — never override a human/agent
        decision.

    Returns
    -------
    ``None``
        Iff any ``art:*`` label is already on the issue.
    ``"art:bug" | "art:enhancement" | "art:research_note" | "art:design"``
        Best-scoring label by keyword count. Ties break in dict
        insertion order (bug → enhancement → research_note → design).
    ``"art:pending"``
        Conservative fallback when no keyword in any category matches —
        forces the issue onto the triage surface for Speaking/Thinking
        to classify, instead of silently picking a wrong label.
    """
    if any(lab.startswith("art:") for lab in existing_labels):
        return None

    text = f"{title} {body}".lower()
    title_text = title.lower()

    scores: dict[str, int] = {}
    for art_type, words in _KEYWORDS.items():
        # Body+title score, with title matches counted a second time so
        # the title (distilled signal) is effectively weighted 2×.
        body_score = sum(1 for w in words if w in text)
        title_bonus = sum(1 for w in words if w in title_text)
        scores[art_type] = body_score + title_bonus

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "art:pending"
    return best
