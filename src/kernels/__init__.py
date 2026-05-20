"""kernels — concrete :class:`core.kernel.Kernel` backends.

Each sub-package implements the backend-agnostic Protocol from
:mod:`alice_core.kernel` for one underlying runtime:

- :mod:`kernels.anthropic` — claude_agent_sdk.
- :mod:`kernels.pi` — pi-coding-agent subprocess.

Agent code never imports these directly. The factory at
:func:`alice_core.kernel.factory.make_kernel` dispatches by backend
name via dynamic import so :mod:`alice_core` stays free of
sibling-package imports (see ``tests/test_core_isolation.py``).
"""
