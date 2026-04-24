"""alice_thinking — the quiet hemisphere.

One-shot wake semantics, driven by :mod:`alice_core.kernel`. Fired
periodically by the ``alice-thinker`` s6 service via
``/usr/local/bin/alice-think``.

See :mod:`alice_thinking.wake` for the entry point. The hemisphere
boundary — thinking may read anything, write only inside ``alice-mind``,
no outside-world side effects — is a norm enforced in the bootstrap
prompt + skills, not by this package.
"""
