"""kernels.hermes — Hermes kernel backend.

Implements the :class:`core.kernel.Kernel` Protocol for Hermes
models (NousResearch) via OpenAI-compatible ``/v1/chat/completions``
endpoints. Sister package to :mod:`kernels.pi` and
:mod:`kernels.anthropic`; loaded dynamically through
:func:`core.kernel.factory.make_kernel` — agent code never imports
this module directly.

Auth: ``HERMES_API_KEY`` env var → Bearer token on the outbound
request. ``base_url`` from ``mind/config/model.yml``
(``backends.hermes.base_url``) — Nous-hosted by default; local vLLM
deployment overrides.
"""

from .kernel import HermesKernel


__all__ = ["HermesKernel"]
