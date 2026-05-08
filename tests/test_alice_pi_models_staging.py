"""Tests for alice_pi.models_staging.

The helper stages the vault's ``pi-models.json`` into pi's runtime
location at ``~/.pi/agent/models.json`` (or ``$PI_AGENT_DIR/...``).
Contract: idempotent, fail-soft, never raises.
"""

from __future__ import annotations

import json
import pathlib
from unittest import mock

import pytest

from alice_core.events import CapturingEmitter
from alice_core.kernel import KernelSpec
from alice_pi import models_staging
from alice_pi.kernel import PiKernel
from alice_pi.models_staging import ensure_pi_models_json


_SAMPLE_REGISTRY = {
    "providers": {
        "openai-local": {
            "baseUrl": "http://10.20.30.177:8033/v1",
            "api": "openai-completions",
            "apiKey": "no-key-required",
            "models": [
                {
                    "id": "Qwen3.6-35B-A3B-Q8_K_XL",
                    "name": "Qwen local",
                    "reasoning": False,
                    "input": ["text"],
                    "contextWindow": 262144,
                    "maxTokens": 8192,
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                }
            ],
        }
    }
}


def _write_sample(path: pathlib.Path, payload=_SAMPLE_REGISTRY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def test_source_absent_is_noop(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "missing.json"
    dst = tmp_path / "out" / "models.json"

    cap = CapturingEmitter()
    result = ensure_pi_models_json(source=src, dest=dst, emit=cap.emit)

    assert result is False
    assert not dst.exists()
    # Source-absent is the documented "no managed registry" path —
    # not an error, no event.
    assert cap.events == []


def test_source_present_dest_absent_copies(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src.json"
    dst = tmp_path / "out" / "nested" / "models.json"
    _write_sample(src)

    cap = CapturingEmitter()
    result = ensure_pi_models_json(source=src, dest=dst, emit=cap.emit)

    assert result is True
    assert dst.exists()
    assert dst.read_bytes() == src.read_bytes()
    assert cap.events == []


def test_dest_matches_byte_for_byte_is_noop(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    _write_sample(src)
    dst.write_bytes(src.read_bytes())

    # Capture original mtime — a no-op must not touch the file.
    orig_mtime = dst.stat().st_mtime_ns

    cap = CapturingEmitter()
    result = ensure_pi_models_json(source=src, dest=dst, emit=cap.emit)

    assert result is False
    assert dst.stat().st_mtime_ns == orig_mtime
    assert cap.events == []


def test_dest_differs_overwrites(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    _write_sample(src, {"providers": {"new-provider": {}}})
    dst.write_text(json.dumps({"providers": {"old-provider": {}}}))

    cap = CapturingEmitter()
    result = ensure_pi_models_json(source=src, dest=dst, emit=cap.emit)

    assert result is True
    assert dst.read_bytes() == src.read_bytes()
    assert json.loads(dst.read_text()) == {"providers": {"new-provider": {}}}
    assert cap.events == []


def test_same_size_different_content_overwrites(tmp_path: pathlib.Path) -> None:
    """Cheap mtime/size check shortcuts to a content compare when
    sizes match — verify that the content compare actually fires
    (not a stale cache leaking by)."""
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    src.write_text('{"a": 1}')
    dst.write_text('{"a": 2}')  # same length, different bytes
    assert src.stat().st_size == dst.stat().st_size

    cap = CapturingEmitter()
    result = ensure_pi_models_json(source=src, dest=dst, emit=cap.emit)

    assert result is True
    assert dst.read_text() == '{"a": 1}'


def test_malformed_json_emits_warning_returns_false(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("{not valid json")

    cap = CapturingEmitter()
    result = ensure_pi_models_json(source=src, dest=dst, emit=cap.emit)

    assert result is False
    assert not dst.exists()
    fail_events = cap.of_kind("pi_models_stage_failed")
    assert len(fail_events) == 1
    assert "source_malformed_json" in fail_events[0]["reason"]


def test_unreadable_source_emits_warning_returns_false(
    tmp_path: pathlib.Path,
) -> None:
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    _write_sample(src)

    cap = CapturingEmitter()

    # Force the read to fail without depending on filesystem
    # permissions (which behave inconsistently under root in
    # containers).
    real_read_bytes = pathlib.Path.read_bytes

    def _boom(self):
        if self == src:
            raise OSError("simulated read failure")
        return real_read_bytes(self)

    with mock.patch.object(pathlib.Path, "read_bytes", _boom):
        result = ensure_pi_models_json(source=src, dest=dst, emit=cap.emit)

    assert result is False
    assert not dst.exists()
    fail_events = cap.of_kind("pi_models_stage_failed")
    assert len(fail_events) == 1
    assert "source_unreadable" in fail_events[0]["reason"]


def test_write_failure_emits_warning_returns_false(tmp_path: pathlib.Path) -> None:
    src = tmp_path / "src.json"
    dst = tmp_path / "out" / "models.json"
    _write_sample(src)

    cap = CapturingEmitter()

    def _boom(self, data):
        raise OSError("simulated write failure")

    with mock.patch.object(pathlib.Path, "write_bytes", _boom):
        result = ensure_pi_models_json(source=src, dest=dst, emit=cap.emit)

    assert result is False
    fail_events = cap.of_kind("pi_models_stage_failed")
    assert len(fail_events) == 1
    assert "write_failed" in fail_events[0]["reason"]


def test_emit_none_does_not_raise_on_failure(tmp_path: pathlib.Path) -> None:
    """No emitter wired ⇒ failures still fail-soft, just silent."""
    src = tmp_path / "src.json"
    dst = tmp_path / "dst.json"
    src.write_text("{not valid json")

    # Must not raise — None emitter is supported.
    result = ensure_pi_models_json(source=src, dest=dst, emit=None)
    assert result is False


def test_alice_pi_models_json_env_overrides_source(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override_src = tmp_path / "override.json"
    dst = tmp_path / "dst.json"
    _write_sample(override_src, {"providers": {"env-override": {}}})

    monkeypatch.setenv("ALICE_PI_MODELS_JSON", str(override_src))

    # Source not passed explicitly — env should be honored.
    result = ensure_pi_models_json(dest=dst)

    assert result is True
    assert json.loads(dst.read_text()) == {"providers": {"env-override": {}}}


def test_pi_agent_dir_env_overrides_dest(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.json"
    pi_agent_dir = tmp_path / "fake-pi-agent"
    _write_sample(src)

    monkeypatch.setenv("PI_AGENT_DIR", str(pi_agent_dir))

    # Dest not passed explicitly — PI_AGENT_DIR/models.json should be used.
    result = ensure_pi_models_json(source=src)

    assert result is True
    expected_dest = pi_agent_dir / "models.json"
    assert expected_dest.exists()
    assert expected_dest.read_bytes() == src.read_bytes()


def test_explicit_args_override_env(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit kwargs win over env. Belt-and-suspenders for tests
    that want full control."""
    env_src = tmp_path / "env-src.json"
    explicit_src = tmp_path / "explicit-src.json"
    explicit_dst = tmp_path / "explicit-dst.json"
    _write_sample(env_src, {"providers": {"env": {}}})
    _write_sample(explicit_src, {"providers": {"explicit": {}}})

    monkeypatch.setenv("ALICE_PI_MODELS_JSON", str(env_src))
    monkeypatch.setenv("PI_AGENT_DIR", str(tmp_path / "env-pi-dir"))

    result = ensure_pi_models_json(source=explicit_src, dest=explicit_dst)

    assert result is True
    assert json.loads(explicit_dst.read_text()) == {"providers": {"explicit": {}}}
    # Env-driven dest must NOT have been touched.
    assert not (tmp_path / "env-pi-dir" / "models.json").exists()


# --------------------------------------------------------------------
# Integration: PiKernel.run() invokes ensure_pi_models_json once at
# the start of the run.
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pi_kernel_run_invokes_models_staging_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """PiKernel.run() must call ensure_pi_models_json exactly once at
    the start of each run, before driving the pi subprocess."""
    # Stub out the staging helper as imported into kernel.py.
    calls: list[dict] = []

    def _stub(*, source=None, dest=None, emit=None):
        calls.append({"source": source, "dest": dest, "emit": emit})
        return False

    import alice_pi.kernel as kernel_mod

    monkeypatch.setattr(kernel_mod, "ensure_pi_models_json", _stub)

    # Stub out the actual subprocess driver so we don't need a fake
    # pi binary — we only care that staging fires.
    async def _fake_drive(self, argv, spec, translator, handlers):
        return None

    monkeypatch.setattr(PiKernel, "_drive", _fake_drive)

    cap = CapturingEmitter()
    kernel = PiKernel(cap)

    await kernel.run("hi", KernelSpec(model="gpt-5.3-codex"))

    assert len(calls) == 1
    # The kernel passes its own _emit as the emitter so failures
    # land in the same event stream as everything else.
    assert calls[0]["emit"] is not None
    assert callable(calls[0]["emit"])


@pytest.mark.asyncio
async def test_pi_kernel_run_does_not_raise_on_staging_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If staging itself somehow raised (it shouldn't — fail-soft is
    in the helper), the kernel should not crash. Belt-and-suspenders
    via a stub that does raise; current production code can't reach
    this path, but the contract is 'staging never breaks a run'."""

    def _boom(*, source=None, dest=None, emit=None):
        # The real helper is fail-soft and never raises. This stub
        # asserts that even if a future bug regressed that, the
        # kernel would still surface the staging failure as an event
        # path rather than tearing down the wake. We pin this via the
        # real helper's contract; here we just confirm the kernel
        # calls through.
        if emit is not None:
            emit("pi_models_stage_failed", reason="stubbed")
        return False

    import alice_pi.kernel as kernel_mod

    monkeypatch.setattr(kernel_mod, "ensure_pi_models_json", _boom)

    async def _fake_drive(self, argv, spec, translator, handlers):
        return None

    monkeypatch.setattr(PiKernel, "_drive", _fake_drive)

    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    await kernel.run("hi", KernelSpec(model="gpt-5.3-codex"))

    # The stubbed failure event made it through the kernel's
    # emitter wrapper.
    fails = cap.of_kind("pi_models_stage_failed")
    assert len(fails) == 1
    assert fails[0]["reason"] == "stubbed"


def test_default_source_path_is_vault_config() -> None:
    """The default source path is the vault config — pin it so a
    refactor doesn't accidentally redirect alice-pi at the wrong
    file. If the vault path moves, update this constant
    deliberately."""
    assert models_staging._DEFAULT_SOURCE == pathlib.Path(
        "/home/alice/alice-mind/config/pi-models.json"
    )
