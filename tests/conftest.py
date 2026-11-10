"""Shared pytest fixtures for the alice-speaking test suite."""

from __future__ import annotations

import os
import pathlib

import pytest

# Forge repo slugs have no built-in default (fail-loud); give the suite a
# fixed set *before* modules that resolve them at import time are loaded.
os.environ.setdefault("ALICE_FORGE_REPOS", "jcronq/cozyhem-engine,jcronq/alice")
os.environ.setdefault("ALICE_FORGE_REPO", "jcronq/alice")
os.environ.setdefault("ALICE_BUG_FIX_REPO", "jcronq/modern-mud")

from alice_speaking.infra.config import Config, SPEAKING_DEFAULTS
from alice_speaking.domain.principals import (
    AddressBook,
    PrincipalChannel,
    PrincipalRecord,
)


@pytest.fixture(autouse=True)
def _isolate_status_dir(tmp_path_factory, monkeypatch):
    """Point ``status-reply`` thread writes at a tmp dir for every test.

    Code paths that post to a status-site thread (e.g. the bug-fix
    dispatcher's default poster) would otherwise append to the real
    ``~/alice-mind/inner/status/threads``.
    """
    monkeypatch.setenv("STATUS_DIR", str(tmp_path_factory.mktemp("status-dir-isolation")))
    yield


@pytest.fixture(autouse=True)
def _isolate_gh_rate_limit_backoff(tmp_path_factory, monkeypatch):
    """Redirect the shared gh rate-limit backoff file to a per-test tmp path.

    Issue #786 M4. Without this, a test that triggers a real (or
    fake-with-429-shape) ``gh`` error would write to
    ``/state/worker/gh-rate-limit-backoff.json``, poisoning the
    production state for every subsequent test AND for the live
    speaking daemon running on the same host. The rate-limit module
    resolves the env var lazily on each call so the redirect is
    effective throughout the test's lifetime.
    """
    monkeypatch.setenv(
        "ALICE_GH_RATE_LIMIT_BACKOFF_PATH",
        str(tmp_path_factory.mktemp("gh-rate-limit") / "backoff.json"),
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_pi_models_staging(tmp_path_factory, monkeypatch):
    """Redirect kernels.pi.models_staging at tmp paths for every test.

    The PiKernel.run() integration stages
    ``~/alice-mind/config/pi-models.json`` → ``~/.pi/agent/models.json``
    at the start of each run. Without this fixture, any test that
    actually invokes ``PiKernel.run()`` would touch the real user's
    home directory. Tests that need to exercise staging directly
    pass explicit ``source=``/``dest=`` kwargs to the helper, which
    take priority over these env vars.
    """
    isolation_dir = tmp_path_factory.mktemp("pi-models-isolation")
    # Source path that doesn't exist → staging no-ops cleanly.
    monkeypatch.setenv(
        "ALICE_PI_MODELS_JSON", str(isolation_dir / "absent-source.json")
    )
    # Dest under tmp → even if a test writes a source, output is
    # contained.
    monkeypatch.setenv("PI_AGENT_DIR", str(isolation_dir / "pi-agent"))
    yield


@pytest.fixture(autouse=True)
def _reset_alice_prompts_default_loader():
    """Reset the prompts package-level loader after every test.

    Tests that mutate the singleton (via :func:`prompts.set_default_loader`
    or :func:`alice_thinking.wake._install_prompt_loader`) used to leak
    state into subsequent tests — the ``test_prompts.py`` cases that
    rely on the placeholder personae (``"the operator"``) failed when
    a thinking-wake test ran first and replaced the loader with one
    pointing at a tmp-dir override path.
    """
    import prompts as _prompts

    yield
    _prompts._default_loader = None


@pytest.fixture
def cfg(tmp_path: pathlib.Path) -> Config:
    """Minimal Config for tests. All paths live under tmp_path so tests
    can't stomp on real state."""
    mind_dir = tmp_path / "mind"
    state_dir = tmp_path / "state"
    mind_dir.mkdir()
    state_dir.mkdir()
    return Config(
        signal_api="http://127.0.0.1:8080",
        signal_account="+15550000000",
        oauth_token="dummy",
        work_dir=mind_dir,
        mind_dir=mind_dir,
        state_dir=state_dir,
        signal_log_path=state_dir / "signal.log",
        offset_path=state_dir / "offset",
        seen_path=state_dir / "seen",
        turn_log_path=mind_dir / "inner" / "state" / "speaking-turns.jsonl",
        event_log_path=state_dir / "speaking.log",
        principals_path=mind_dir / "config" / "principals.yaml",
        allowed_senders_fallback={
            "+15555550100": "Owner",
            "+15555550101": "Friend",
        },
        speaking=dict(SPEAKING_DEFAULTS),
    )


@pytest.fixture
def address_book() -> AddressBook:
    """Standard two-principal address book matching the legacy
    ALLOWED_SENDERS fixture: Owner on signal+cli, Friend on signal only."""
    return AddressBook(
        [
            PrincipalRecord(
                id="owner",
                display_name="Owner",
                channels=[
                    PrincipalChannel(
                        transport="signal",
                        address="+15555550100",
                        durable=True,
                        preferred=True,
                    ),
                    PrincipalChannel(transport="cli", address="1000", durable=False),
                ],
            ),
            PrincipalRecord(
                id="friend",
                display_name="Friend",
                channels=[
                    PrincipalChannel(
                        transport="signal",
                        address="+15555550101",
                        durable=True,
                        preferred=True,
                    ),
                ],
            ),
        ]
    )
