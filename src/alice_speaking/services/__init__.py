"""Long-running speaking-side services that don't belong in the daemon.

The daemon owns Signal turns and surface handling. Anything that runs
on its own cadence — face-caption push, future heartbeat / metric
drivers — lives here and gets its own s6 longrun under
``sandbox/s6/``.
"""
