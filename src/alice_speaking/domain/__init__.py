"""Domain — the model of what a speaking turn IS.

These modules are the nouns of the conversation, not the
machinery around it:

- :mod:`principals` — address book, principal records, channel refs.
- :mod:`render` — capability-aware rendering of Alice's outbound
  text into the chunks each transport needs.
- :mod:`session_state` — Layer-1 session-id persistence (read /
  write / sdk-presence check).
- :mod:`turn_log` — append-only log of every turn for Layer-2
  bootstrap and the viewer's narrative dump.

Plan 02 of the speaking refactor moved these from the flat
``alice_speaking/`` top level into this subpackage.
"""
