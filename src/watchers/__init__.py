"""Background watchers that feed Alice's inner/notes/ inbox.

Each watcher is a one-shot poller invoked on a cadence by the worker's
s6 supervisor. They read external state (GitHub, calendar, etc.), diff
against persisted "already seen" markers under ``/state/worker/``, and
emit fresh markdown notes for thinking to drain on her next wake.

No long-lived process — each invocation does one pass and exits.
"""
