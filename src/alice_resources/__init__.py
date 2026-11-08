"""alice_resources — CPU / RSS / Claude-token sampling for the status-site Resources page.

The sampler loop runs under s6 as ``alice-resource-sampler``. It writes
two append-only tables:

* ``resource_samples`` — per-process CPU% and RSS, matched back to a
  worker ``bg_id`` where possible.
* ``token_usage`` — per-assistant-turn token counts from Claude Code
  transcripts under ``~/.claude/projects/**/<session>.jsonl``.

See :mod:`alice_resources.sampler` for the loop entry point and the
pure parsing helpers (tested in ``tests/test_alice_resources_*.py``).
"""

from __future__ import annotations

from .sampler import (
    PsRow,
    TokenRow,
    main,
    parse_ps_output,
    parse_transcript_turns,
    tag_for_cmd,
)

__all__ = [
    "PsRow",
    "TokenRow",
    "main",
    "parse_ps_output",
    "parse_transcript_turns",
    "tag_for_cmd",
]
