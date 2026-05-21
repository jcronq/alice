"""Per-state v3 handlers.

Each module under this package implements the v3 handler for one
state, exporting a ``handle`` function with the shared signature
``(issue, services) -> HandlerResult``. The dispatcher dispatches
based on the issue's current label.

Phase 2 ports states one at a time in this order:
  1. sm:draft (this PR)
  2. sm:compacting
  3. sm:building
  4. sm:needs_study
  5. sm:designing + sm:design_review + sm:designed (together)
  6. sm:selected
  7. sm:reviewing
"""

from __future__ import annotations

from alice_forge.sm.handlers.building import handle as handle_building
from alice_forge.sm.handlers.compacting import handle as handle_compacting
from alice_forge.sm.handlers.designed import handle as handle_designed
from alice_forge.sm.handlers.design_review import handle as handle_design_review
from alice_forge.sm.handlers.designing import handle as handle_designing
from alice_forge.sm.handlers.draft import handle as handle_draft
from alice_forge.sm.handlers.needs_study import handle as handle_needs_study
from alice_forge.sm.handlers.selected import handle as handle_selected

__all__ = [
    "handle_draft",
    "handle_compacting",
    "handle_building",
    "handle_needs_study",
    "handle_designing",
    "handle_design_review",
    "handle_designed",
    "handle_selected",
]
