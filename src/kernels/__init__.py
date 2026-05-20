"""kernels — concrete :class:`core.kernel.Kernel` backends.

Each sub-package implements the backend-agnostic Protocol from
:mod:`core.kernel` for one underlying runtime:

- :mod:`kernels.anthropic` — claude_agent_sdk.
- :mod:`kernels.pi` — pi-coding-agent subprocess.

Agent code never imports these directly. The factory at
:func:`core.kernel.factory.make_kernel` dispatches by backend
name via dynamic import so :mod:`core` stays free of
sibling-package imports (see ``tests/test_core_isolation.py``).
"""
