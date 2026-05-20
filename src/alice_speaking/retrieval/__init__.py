"""Vault retrieval helpers for Speaking Alice.

This subpackage owns the cue runner — the retrieval mechanism that
queries ``cortex-index.db`` before each turn and prepends a small
reference packet of vault matches to Speaking's prompt.

Public entry point:

- :func:`alice_speaking.retrieval.cue_runner.build_cue_packet` — fire
  before composing the turn prompt; returns a packet string or ``""``
  on any failure (cue runner failure must never break a turn).
"""

from .cue_runner import CueContext, build_cue_context, build_cue_packet


__all__ = ["CueContext", "build_cue_context", "build_cue_packet"]
