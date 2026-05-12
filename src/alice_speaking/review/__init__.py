"""Code-quality reviewer agents for ``art:code`` PRs at sm:reviewing.

Speaking-side counterpart to ``alice_thinking.design_pipeline``'s
design-doc reviewer. The design reviewer judges *intent vs. spec* on
research/design drafts; this package's reviewers judge *implementation
quality* on PR diffs.

See ``cortex-memory/research/2026-05-12-swe-practices-in-agents-review.md``
§R3 for the motivation.
"""

from alice_speaking.review.code_reviewer import (
    CODE_REVIEW_CATEGORIES,
    CODE_REVIEWER_SYSTEM_PROMPT,
    CodeReviewResult,
)

__all__ = [
    "CODE_REVIEW_CATEGORIES",
    "CODE_REVIEWER_SYSTEM_PROMPT",
    "CodeReviewResult",
]
