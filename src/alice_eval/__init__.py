"""Speaking-quality eval harness.

Day-1 harness for the speaking-quality eval per
``cortex-memory/research/2026-05-15-speaking-quality-eval-design.md``:

- :mod:`alice_eval.sampling` — stratified sample extraction from
  ``inner/state/speaking-turns.jsonl``.
- :mod:`alice_eval.replay`   — fan-out to candidate models, capturing
  output, latency, and token counts.
- :mod:`alice_eval.rating_ui` — generates the single-file blind-rating
  HTML for Jason.
- :mod:`alice_eval.pii`      — conservative redaction (phone, email,
  ``/home/`` paths) applied before any network call.
- :mod:`alice_eval.prompt`   — composes the speaking system prompt
  using :mod:`alice_prompts`.

The package is intentionally offline of the running speaking daemon —
it reads the log and writes new files but never touches live routing
or ``model.yml``. CLI dispatch lives in :mod:`alice_eval.__main__`.
"""

__all__: list[str] = []
