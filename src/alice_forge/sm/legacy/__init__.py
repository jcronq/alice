"""Legacy v1 state-machine modules — retained for one-month grace period.

Phase 4 of the SM v3 rollout (issue #301) moved the v1 dispatcher's
``handlers/`` package and the ``DispatcherState`` ledger here from
``alice_forge.dispatcher`` so v3 can own the natural namespace. The
files still import, run, and are exercised by the existing test
suite under ``tests/test_alice_forge_dispatcher.py`` — the move is
strictly a rename, not a behaviour change.

The dispatcher's main loop (``alice_forge.dispatcher.main.run``) no
longer drives transitions through these modules. v3 owns transition
decisions, comment emission, ledger writes, and parse-error replies.
v1's preserved transport modules (``gh.py``, ``git_ops.py``,
``spawn.py``, ``verify.py``, ``report.py``) still carry the
side-effects v3 hasn't ported (spawn dispatch, hello, rebase
machinery, verify gate, post-merge cleanup). Where v3 returns a
``HandlerResult`` other than ``Transition``, the dispatcher falls
through to the matching legacy handler in this package so those
side-effects still fire.

**This package is slated for deletion in the next month.** The
grace period exists so a rollback to v1 stays as cheap as ``git
revert`` for the Phase 4 PR + restoring the legacy import paths in
``alice_forge.dispatcher.__init__``. Once Phase 4 is stable in
production for four weeks with no rollback events, a follow-up PR
deletes this package outright. New code MUST NOT import from
``alice_forge.sm.legacy.*`` — wire against the v3 ``alice_forge.sm``
primitives instead.

Module inventory:

* :mod:`alice_forge.sm.legacy.state` — :class:`DispatcherState`
  (eight retrofitted dedup ledgers), :func:`load_state`,
  :func:`save_state`. Replaced by :mod:`alice_forge.sm.ledger`
  (unified :class:`EmittedRecord` schema). Forward-migrated on first
  load by :func:`alice_forge.sm.ledger.load_or_migrate`.

* :mod:`alice_forge.sm.legacy.handlers` — per-state ``_process_*``
  functions. Replaced by the uniform-shape ``handle`` handlers in
  :mod:`alice_forge.sm.handlers`. The legacy handlers retain the
  shared scaffolding via ``handlers._common``.
"""
