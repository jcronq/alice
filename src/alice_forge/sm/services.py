"""HandlerServices — the IO bundle handed to each v3 handler.

A single dataclass carries every external dependency a handler
needs: comment IO, label IO, ledger access, comment list, current
time. Handlers never reach into module-level state; everything
flows through this bundle. Tests substitute a stub bundle.

Why a bundle instead of individual kwargs: v1's handlers had 10-15
positional/keyword arguments each, with different subsets per
handler. The bundle gives every handler the same call surface —
the dispatcher's main loop never has to remember which handler
needs which subset. It also makes it trivial to add a new service
in one place rather than threading through every handler signature.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Callable

from alice_forge.sm.ledger import EmittedLedger


@dataclass
class HandlerServices:
    """Bundle of every external dependency a v3 handler may use.

    ``ledger`` — the unified emit ledger. Handlers call
    ``ledger.is_emitted_active`` / ``ledger.mark_emitted`` /
    ``ledger.clear_emitted`` directly; the dispatcher persists at
    cadence end.

    ``post_comment`` / ``list_comments`` / ``edit_labels`` /
    ``close_issue`` / ``find_linked_pr`` / ``pr_merge_status`` /
    ``master_ci_status`` — same callable shapes as v1's
    ``alice_forge.dispatcher.gh`` functions. Reused unchanged
    (they're transport, not protocol).

    ``trusted_authors`` — the trust set passed to the parser. Reused
    from v1's ``TRUSTED_AUTHORS`` to keep the cutover behavior
    identical.

    ``now`` — current UTC datetime. Tests inject a fixed value;
    production uses ``lambda: datetime.now(tz=UTC)``.

    ``log`` — line-based logger (stderr in production).

    ``repo`` — owner/repo slug for the issue being processed.

    The dispatcher constructs one HandlerServices per cadence and
    passes the same instance to every handler call in that cadence.
    Reusing it across cadences is safe — the instance is immutable
    in practice (callables are bound at construction).
    """

    ledger: EmittedLedger
    repo: str
    post_comment: Callable[[str, int, str], None]
    list_comments: Callable[[str, int], list[dict[str, Any]]]
    edit_labels: Callable[..., None]
    close_issue: Callable[[str, int], None]
    find_linked_pr: Callable[[str, int], dict[str, Any] | None]
    pr_merge_status: Callable[..., Any]
    master_ci_status: Callable[..., Any]
    trusted_authors: frozenset[str]
    now: Callable[[], _dt.datetime]
    log: Callable[[str], None]
