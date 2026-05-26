"""Statistical person-identification classifier for the cozylobe motion-cortex.

This package was relocated from ``~/alice-mind/cozylobe-cortex/classify.py``
into the alice repo on 2026-05-26 (Phase 5 of motion-cortex, see issue #399 +
``cortex-memory/research/2026-05-26-classify-integration-design.md``). The
profile files (``people/*.md``), room graphs, sensor specs, and other vault
content stay in ``~/alice-mind/cozylobe-cortex/`` — that's vault knowledge,
not code. Only the classifier itself moves.

Profile loading is path-configurable via the ``COZYLOBE_CORTEX_PATH``
environment variable; it defaults to ``~/alice-mind/cozylobe-cortex/`` so the
bind-mounted alice container reaches the profile files without further
configuration.

Public API:

* :class:`MotionEvent` — single motion-sensor firing.
* :class:`MotionTrail` — sliding window of recent events.
* :class:`BehavioralProfile` — per-person priors loaded from the vault.
* :class:`ClassificationResult` — classifier output.
* :func:`classify` — score a trail against a set of profiles.
* :func:`classify_from_trail` — convenience wrapper used by the motion
  pipeline; takes a raw event list and loads profiles from the vault if
  not supplied.
* :func:`load_profiles` — read all ``people/*.md`` profiles from the
  configured cozylobe-cortex directory.
"""

from .classify import (
    BehavioralProfile,
    ClassificationResult,
    MotionEvent,
    MotionTrail,
    classify,
    classify_from_trail,
    classify_latest,
    load_profiles,
)


__all__ = [
    "BehavioralProfile",
    "ClassificationResult",
    "MotionEvent",
    "MotionTrail",
    "classify",
    "classify_from_trail",
    "classify_latest",
    "load_profiles",
]
