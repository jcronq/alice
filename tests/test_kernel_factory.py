"""Tests for alice_core.kernel.factory.make_kernel.

The factory is the single switch point that turns a BackendSpec
into a concrete Kernel impl. These tests pin: (a) subscription /
api / bedrock all return AnthropicKernel today, (b) "pi" is wired
to lazy-import PiKernel (xfail until Phase D ships PiKernel),
(c) constructor kwargs (correlation_id, silent, short_cap) thread
through.
"""

from __future__ import annotations

import pytest

from alice_core.config.model import BackendSpec
from alice_core.events import CapturingEmitter
from alice_core.kernel import AnthropicKernel, Kernel, make_kernel


@pytest.mark.parametrize("backend_name", ["subscription", "api", "bedrock"])
def test_make_kernel_returns_anthropic_for_anthropic_backends(backend_name) -> None:
    spec = BackendSpec(backend=backend_name)  # type: ignore[arg-type]
    kernel = make_kernel(spec, CapturingEmitter())
    assert isinstance(kernel, AnthropicKernel)
    assert isinstance(kernel, Kernel)  # runtime_checkable Protocol


def test_make_kernel_threads_constructor_kwargs() -> None:
    spec = BackendSpec(backend="subscription")
    emitter = CapturingEmitter()
    kernel = make_kernel(
        spec, emitter, correlation_id="corr-123", silent=True, short_cap=512
    )
    assert isinstance(kernel, AnthropicKernel)
    # AnthropicKernel exposes these on its instance:
    assert kernel.correlation_id == "corr-123"
    assert kernel.silent is True
    assert kernel._cap == 512


def test_make_kernel_default_branch_is_subscription() -> None:
    """Unknown / missing backend.backend falls through to the
    Anthropic path. Defensive: bad config doesn't blow up at
    factory-call time; the auth layer raises later if creds
    don't match."""

    class _StrayBackend:
        pass

    kernel = make_kernel(_StrayBackend(), CapturingEmitter())
    assert isinstance(kernel, AnthropicKernel)


@pytest.mark.xfail(
    reason="PiKernel not implemented until Phase D; factory branch "
    "ImportErrors until alice_pi.kernel exists."
)
def test_make_kernel_returns_pi_for_pi_backend() -> None:
    spec = BackendSpec(backend="pi")  # type: ignore[arg-type]
    # Phase D ships alice_pi.kernel.PiKernel; until then this raises
    # ImportError and the xfail marker keeps the suite green.
    kernel = make_kernel(spec, CapturingEmitter())
    assert kernel.__class__.__name__ == "PiKernel"
