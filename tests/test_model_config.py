"""Phase B of the personae+backend plan: model config loader.

Covers the loader's parsing surface end-to-end:

- minimal load (just the hemispheres)
- full load (all three backends + per-hemisphere overrides)
- missing-file fallback (returns subscription-default)
- invalid backend name raises a clear error
- per-hemisphere fields override top-level ``backends.<name>.*``
"""

from __future__ import annotations

import pathlib

import pytest

from core.config.model import (
    BackendSpec,
    ModelConfig,
    ModelConfigError,
    from_mapping,
    load,
)


def _write(mind: pathlib.Path, body: str) -> pathlib.Path:
    cfg_dir = mind / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "model.yml"
    path.write_text(body)
    return path


def test_load_minimal_config(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        """
        speaking:
          backend: subscription
          model: claude-opus-4-7
        thinking:
          backend: subscription
          model: claude-sonnet-4-6
        """,
    )
    cfg = load(tmp_path)
    assert cfg.speaking == BackendSpec(backend="subscription", model="claude-opus-4-7")
    assert cfg.thinking == BackendSpec(
        backend="subscription", model="claude-sonnet-4-6"
    )
    # Viewer absent → defaults to subscription with no model override.
    assert cfg.viewer.backend == "subscription"
    assert cfg.viewer.model == ""


def test_harness_pi_mono_selects_pi_backend() -> None:
    cfg = from_mapping(
        {
            "speaking": {
                "harness": "pi-mono",
                "model": "gpt-5.3-codex",
            },
            "thinking": {
                "agent_harness": "pi",
                "model": "gpt-5.3-codex",
            },
        }
    )
    assert cfg.speaking.harness == "pi-mono"
    assert cfg.speaking.backend == "pi"
    assert cfg.thinking.harness == "pi-mono"
    assert cfg.thinking.backend == "pi"


def test_backend_pi_defaults_to_pi_mono_harness() -> None:
    cfg = from_mapping({"thinking": {"backend": "pi", "model": "gpt-5.3-codex"}})
    assert cfg.thinking.harness == "pi-mono"
    assert cfg.thinking.backend == "pi"


def test_harness_backend_mismatch_raises() -> None:
    with pytest.raises(ModelConfigError, match="requires .*backend = 'pi'"):
        from_mapping(
            {
                "speaking": {
                    "harness": "pi-mono",
                    "backend": "subscription",
                    "model": "gpt-5.3-codex",
                }
            }
        )


def test_load_full_config(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        """
        speaking:
          backend: api
          model: claude-opus-4-7
        thinking:
          backend: bedrock
          model: anthropic.claude-sonnet-4-5-20250929-v1:0
          region: us-east-1
        viewer:
          backend: subscription
          model: claude-haiku-4-5-20251001
        backends:
          api:
            base_url: https://litellm.example.com/v1
          bedrock:
            region: us-west-2
            profile: alice-prod
        """,
    )
    cfg = load(tmp_path)
    assert cfg.speaking.backend == "api"
    assert cfg.speaking.base_url == "https://litellm.example.com/v1"  # inherited
    assert cfg.thinking.backend == "bedrock"
    assert cfg.thinking.region == "us-east-1"  # per-hemisphere override
    assert cfg.thinking.profile == "alice-prod"  # inherited from backends
    assert cfg.viewer.backend == "subscription"
    assert cfg.viewer.model == "claude-haiku-4-5-20251001"


def test_load_missing_file_returns_subscription_default(tmp_path: pathlib.Path) -> None:
    cfg = load(tmp_path)
    assert cfg == ModelConfig.subscription_default()
    assert cfg.speaking.backend == "subscription"
    assert cfg.thinking.backend == "subscription"
    assert cfg.viewer.backend == "subscription"


def test_load_empty_file_returns_subscription_default(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "")
    cfg = load(tmp_path)
    assert cfg == ModelConfig.subscription_default()


def test_invalid_backend_raises(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        """
        speaking:
          backend: vertex
          model: claude-opus-4-7
        """,
    )
    with pytest.raises(ModelConfigError, match="speaking.backend"):
        load(tmp_path)


def test_invalid_top_level_backend_name_raises() -> None:
    with pytest.raises(ModelConfigError, match="unknown backend 'vertex'"):
        from_mapping({"backends": {"vertex": {"region": "us-east-1"}}})


def test_per_hemisphere_inherits_from_top_level_backends() -> None:
    """Per-hemisphere fields are absent → backends.<name>.* fills them."""
    cfg = from_mapping(
        {
            "thinking": {"backend": "bedrock", "model": "anthropic.claude-x"},
            "backends": {"bedrock": {"region": "eu-west-1", "profile": "p"}},
        }
    )
    assert cfg.thinking.region == "eu-west-1"
    assert cfg.thinking.profile == "p"


def test_per_hemisphere_override_wins_over_backends_block() -> None:
    cfg = from_mapping(
        {
            "thinking": {
                "backend": "bedrock",
                "model": "anthropic.claude-x",
                "region": "us-east-1",
            },
            "backends": {"bedrock": {"region": "eu-west-1"}},
        }
    )
    assert cfg.thinking.region == "us-east-1"


def test_yaml_parse_error(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "speaking: {backend: : :")
    with pytest.raises(ModelConfigError, match="failed to parse"):
        load(tmp_path)


def test_hemisphere_lookup_by_name() -> None:
    cfg = ModelConfig.subscription_default()
    assert cfg.hemisphere("speaking") is cfg.speaking
    assert cfg.hemisphere("thinking") is cfg.thinking
    assert cfg.hemisphere("viewer") is cfg.viewer
    with pytest.raises(KeyError):
        cfg.hemisphere("voicing")


# Strix Halo Phase 2: per-stage backend overrides.


def test_stage_spec_returns_none_when_stages_block_absent() -> None:
    cfg = from_mapping(
        {
            "thinking": {
                "harness": "pi-mono",
                "backend": "pi",
                "model": "openai-local/Qwen3.6-35B",
            }
        }
    )
    assert cfg.stage_spec("thinking", "sleep_d") is None
    assert cfg.stage_spec("thinking", "active") is None


def test_stage_spec_unknown_hemisphere_raises() -> None:
    cfg = ModelConfig.subscription_default()
    with pytest.raises(KeyError):
        cfg.stage_spec("voicing", "active")


def test_stage_block_inherits_from_hemisphere_base() -> None:
    """A stage entry that omits ``harness``/``backend`` inherits them
    from the resolved hemisphere spec, not from ``backends.*``."""
    cfg = from_mapping(
        {
            "thinking": {
                "harness": "pi-mono",
                "backend": "pi",
                "model": "openai-local/Qwen3.6-35B",
                "stages": {
                    "active": {"model": "openai-local/some-other-model"},
                },
            }
        }
    )
    spec = cfg.stage_spec("thinking", "active")
    assert spec is not None
    assert spec.backend == "pi"
    assert spec.harness == "pi-mono"
    assert spec.model == "openai-local/some-other-model"


def test_stage_block_overrides_backend_harness_and_model() -> None:
    cfg = from_mapping(
        {
            "thinking": {
                "harness": "pi-mono",
                "backend": "pi",
                "model": "openai-local/Qwen3.6-35B",
                "stages": {
                    "sleep_d": {
                        "backend": "subscription",
                        "model": "claude-sonnet-4-6",
                        "harness": "claude-code",
                    },
                },
            }
        }
    )
    spec = cfg.stage_spec("thinking", "sleep_d")
    assert spec is not None
    assert spec.backend == "subscription"
    assert spec.harness == "claude-code"
    assert spec.model == "claude-sonnet-4-6"


def test_stage_block_unknown_stage_key_raises() -> None:
    with pytest.raises(ModelConfigError, match="unknown stage 'sleep_e'"):
        from_mapping(
            {
                "thinking": {
                    "backend": "subscription",
                    "stages": {"sleep_e": {"model": "x"}},
                }
            }
        )


def test_stage_block_invalid_harness_backend_pairing_raises() -> None:
    with pytest.raises(ModelConfigError, match="requires .*backend = 'pi'"):
        from_mapping(
            {
                "thinking": {
                    "backend": "subscription",
                    "stages": {
                        "sleep_d": {
                            "harness": "pi-mono",
                            "backend": "subscription",
                        }
                    },
                }
            }
        )


def test_stage_block_supports_all_four_stage_keys() -> None:
    cfg = from_mapping(
        {
            "thinking": {
                "harness": "pi-mono",
                "backend": "pi",
                "model": "openai-local/base",
                "stages": {
                    "active": {"model": "openai-local/active-model"},
                    "sleep_b": {"model": "openai-local/sleep-b-model"},
                    "sleep_c": {"model": "openai-local/sleep-c-model"},
                    "sleep_d": {"model": "openai-local/sleep-d-model"},
                },
            }
        }
    )
    for stage in ("active", "sleep_b", "sleep_c", "sleep_d"):
        spec = cfg.stage_spec("thinking", stage)
        assert spec is not None
        assert spec.model == f"openai-local/{stage.replace('_', '-')}-model"


def test_stage_block_must_be_mapping() -> None:
    with pytest.raises(ModelConfigError, match="thinking.stages"):
        from_mapping(
            {
                "thinking": {
                    "backend": "subscription",
                    "stages": ["sleep_d"],
                }
            }
        )
