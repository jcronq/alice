"""Prompt assembly — merge a base system prompt with behavioral rules.

A standalone function rather than an :class:`AgentSpec` method so the
SM dispatcher (and other future callers that don't yet have an
:class:`AgentSpec` registered) can produce the same merged prompt
without going through the registry. Same logic, same result.

The function is pure: takes inputs, returns a string. No I/O, no
templating engine, no persona-YAML lookup (Phase 1 keeps persona
metadata on the :class:`AgentSpec` itself). Phase 2 will add a
sibling that loads persona YAML and threads it in here.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .types import BehavioralRule


__all__ = ["merge"]


def merge(
    base: Optional[str],
    rules: Iterable[BehavioralRule],
    extra: str = "",
) -> Optional[str]:
    """Combine ``base``, every rule's rendered injection, and
    ``extra`` into a single ``append_system_prompt`` string.

    Empty / ``None`` inputs are dropped; sections are joined with a
    blank line. Returns ``None`` when the result is empty so callers
    can pass it straight into :class:`core.kernel.KernelSpec` (which
    treats ``None`` as "no override" and ``""`` as "override with
    empty string").
    """
    base_text = (base or "").rstrip()
    rule_blocks = [rule.render() for rule in rules]
    extra_text = extra.rstrip()
    parts = [
        section
        for section in (base_text, *rule_blocks, extra_text)
        if section
    ]
    return "\n\n".join(parts) if parts else None
