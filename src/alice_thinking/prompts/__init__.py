"""Phase-routed prompt fragments for thinking wakes.

Single source of truth for the per-phase wake instructions. The
repo is bind-mounted rw into worker containers, so editing a file
here takes effect on the next wake — no rebuild required, but
commits make the iteration durable.

Layout:

- ``prelude.md`` — shared across all phases (identity, hemispheres,
  constitutional boundary, constraints, Step 1 skeleton, Step 2,
  Step 4, Step 5 skeleton).
- ``active.md`` / ``sleep-b.md`` / ``sleep-c.md`` / ``sleep-d.md`` —
  phase-specific operational steps (Step 0 variant + Step 3 body).

The composition is plain string concatenation (see
:class:`alice_thinking.phase.PromptFragmentLoader`). No Jinja2 here
— the fragments are static markdown.
"""
