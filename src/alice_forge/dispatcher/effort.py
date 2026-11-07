"""Per-task effort level (issue #642): low / medium / high / max.

Precedence: latest ``[SM] effort=<level>`` comment verb > ``effort:<level>``
label > default (``medium``).
"""

from __future__ import annotations

import re
from typing import Any

LEVELS = ("low", "medium", "high", "max")
DEFAULT_EFFORT = "medium"

_VERB_RE = re.compile(r"^\s*\[SM\]\s+effort=(low|medium|high|max)\b", re.I | re.M)

EFFORT_RULES: dict[str, str] = {
    "low": (
        "Effort: LOW (ship-and-iterate). Ship the smallest viable slice, "
        "keep pre-verification minimal, open the PR (mark ready at once if "
        "obvious) and fix forward on the next slice."
    ),
    "medium": (
        "Effort: MEDIUM (ship + verify). Build to spec, add tests covering "
        "the spec's test plan, run the targeted suite, open the PR, and "
        "verify the CI delta before self-merging."
    ),
    "high": (
        "Effort: HIGH (thorough). Build, add tests for edge cases not in the "
        "spec, run the full suite plus a code-review pass, and merge only "
        "when everything is green."
    ),
    "max": (
        "Effort: MAX (go deep until done). Iterate until all open questions "
        "are resolved, run a max-effort code review, close every TODO/FIXME "
        "you touch, and keep working through failure modes until stable."
    ),
}


def read_effort(issue: dict[str, Any]) -> str:
    """Resolve the effort level for an issue dict (comment verb > label > default)."""
    comments = issue.get("comments")
    if isinstance(comments, list):
        for c in reversed(comments):
            body = c.get("body") if isinstance(c, dict) else None
            if isinstance(body, str):
                matches = _VERB_RE.findall(body)
                if matches:
                    return matches[-1].lower()
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.lower().startswith("effort:"):
            level = name.split(":", 1)[1].strip().lower()
            if level in LEVELS:
                return level
    return DEFAULT_EFFORT
