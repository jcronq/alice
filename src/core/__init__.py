"""core — the agentic kernel shared by alice_speaking and alice_thinking.

Owns the pieces that don't care which hemisphere is running:

- :mod:`core.kernel` — ``AgentKernel`` drives one SDK ``query()`` to
  completion, dispatches blocks to observers + handlers, handles timeout +
  session-missing paths.
- :mod:`core.events` — JSONL event emitter + shared ``_short`` serializer.
- :mod:`core.auth` — OAuth token loader (env first, then ``alice.env``).
- :mod:`core.config` — env + JSON config primitives with hemisphere-
  scoped sections.
- :mod:`core.session` — ``session.json`` read/write/clear + SDK-session
  JSONL existence check.
- :mod:`core.sdk_compat` — small helpers that paper over SDK quirks
  (missing-session detection, value truncation for log fields).

Neither a daemon nor an entry point; pure library.
"""
