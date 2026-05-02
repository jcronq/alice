"""Model + backend config loader — ``mind/config/model.yml``.

Plan 06 Phase 1 of the runtime refactor. The runtime supports four
LLM backends:

- ``subscription`` — Anthropic Max OAuth (today's default).
- ``api`` — Anthropic API key, optionally via a LiteLLM proxy.
- ``bedrock`` — AWS Bedrock via ``CLAUDE_CODE_USE_BEDROCK=1``.
- ``pi`` — pi-coding-agent subprocess; routes to ChatGPT/Codex
  subscription via the codex→pi auth bridge. See
  :mod:`alice_pi.kernel` and ``docs/refactor/10-pi-kernel.md``.

Each hemisphere (speaking, thinking, viewer) picks its own backend +
model. ``mind/config/model.yml`` is the source of truth; if missing,
the loader returns :class:`ModelConfig.subscription_default()` so
existing minds keep working unchanged.

Schema (full form):

    speaking:
      harness: claude-code
      backend: subscription
      model: claude-opus-4-7
    thinking:
      harness: pi-mono               # routes through pi-coding-agent
      backend: pi
      model: gpt-5.3-codex           # PiKernel adds openai-codex/ prefix
    viewer:
      backend: subscription
      model: claude-haiku-4-5-20251001
    backends:
      api:
        base_url: https://litellm.example.com/v1
      bedrock:
        region: us-east-1
        profile: alice-prod

Per-hemisphere fields override the matching ``backends.<name>.*``
defaults. Hemispheres absent from the file fall back to subscription
with no model override (the caller layers its own default model on
top).
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


__all__ = [
    "BackendName",
    "BackendDefaults",
    "BackendSpec",
    "HarnessName",
    "ModelConfig",
    "ModelConfigError",
    "from_mapping",
    "load",
]


MODEL_FILENAME = "model.yml"
HEMISPHERES: tuple[str, ...] = ("speaking", "thinking", "viewer")
BackendName = Literal["subscription", "api", "bedrock", "pi"]
HarnessName = Literal["claude-code", "pi-mono"]
_VALID_BACKENDS: frozenset[str] = frozenset({"subscription", "api", "bedrock", "pi"})
_VALID_HARNESSES: frozenset[str] = frozenset({"claude-code", "pi-mono"})
_HARNESS_ALIASES: dict[str, HarnessName] = {
    "claude-code": "claude-code",
    "claude": "claude-code",
    "anthropic": "claude-code",
    "agent-sdk": "claude-code",
    "pi": "pi-mono",
    "pi-mono": "pi-mono",
    "pi-coding-agent": "pi-mono",
}


class ModelConfigError(ValueError):
    """Raised on YAML parse errors, unknown backend names, or shape
    mismatches."""


@dataclass(frozen=True)
class BackendDefaults:
    """Top-level ``backends:`` block — per-backend defaults inherited
    by hemispheres that don't override the field themselves."""

    base_url: str = ""
    region: str = ""
    profile: str = ""


@dataclass(frozen=True)
class BackendSpec:
    """One hemisphere's resolved backend.

    ``backend`` and ``model`` are the load-bearing fields. The rest
    are mode-specific (region/profile for bedrock, base_url for api)
    and empty when the hemisphere doesn't use them.
    """

    backend: BackendName = "subscription"
    model: str = ""
    harness: HarnessName = "claude-code"
    region: str = ""
    profile: str = ""
    base_url: str = ""


@dataclass(frozen=True)
class ModelConfig:
    speaking: BackendSpec = field(default_factory=BackendSpec)
    thinking: BackendSpec = field(default_factory=BackendSpec)
    viewer: BackendSpec = field(default_factory=BackendSpec)
    backends: Mapping[str, BackendDefaults] = field(default_factory=dict)

    @classmethod
    def subscription_default(cls) -> "ModelConfig":
        """The fallback when ``model.yml`` is absent: every hemisphere
        on subscription with no model override (caller layers its own
        default). Matches today's behaviour."""
        return cls(
            speaking=BackendSpec(backend="subscription", harness="claude-code"),
            thinking=BackendSpec(backend="subscription", harness="claude-code"),
            viewer=BackendSpec(backend="subscription", harness="claude-code"),
            backends={},
        )

    def hemisphere(self, name: str) -> BackendSpec:
        """Return the resolved spec for ``name`` (``"speaking"`` /
        ``"thinking"`` / ``"viewer"``). Raises :class:`KeyError` for
        unknown hemispheres so typos don't silently fall back."""
        if name == "speaking":
            return self.speaking
        if name == "thinking":
            return self.thinking
        if name == "viewer":
            return self.viewer
        raise KeyError(f"unknown hemisphere {name!r}; expected one of {HEMISPHERES}")


def _coerce_str(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise ModelConfigError(
        f"{field_name!r} must be a string (got {type(value).__name__})"
    )


def _coerce_harness(value: Any, field_name: str) -> HarnessName | None:
    raw = _coerce_str(value, field_name)
    if not raw:
        return None
    normalized = _HARNESS_ALIASES.get(raw.strip().lower())
    if normalized is None:
        raise ModelConfigError(
            f"{field_name} = {raw!r}; "
            f"expected one of {sorted(_VALID_HARNESSES)}"
        )
    return normalized


def _backend_defaults_from_dict(
    name: str, data: Mapping[str, Any]
) -> BackendDefaults:
    if name not in _VALID_BACKENDS:
        raise ModelConfigError(
            f"unknown backend {name!r} in 'backends:' block; "
            f"expected one of {sorted(_VALID_BACKENDS)}"
        )
    return BackendDefaults(
        base_url=_coerce_str(data.get("base_url"), f"backends.{name}.base_url"),
        region=_coerce_str(data.get("region"), f"backends.{name}.region"),
        profile=_coerce_str(data.get("profile"), f"backends.{name}.profile"),
    )


def _hemisphere_spec(
    name: str,
    data: Mapping[str, Any] | None,
    backends: Mapping[str, BackendDefaults],
) -> BackendSpec:
    """Build the per-hemisphere spec, layering in the matching
    ``backends.<name>`` defaults for any field the hemisphere didn't
    set itself."""
    if data is None:
        return BackendSpec(backend="subscription", harness="claude-code")
    if not isinstance(data, Mapping):
        raise ModelConfigError(
            f"{name!r} must be a mapping (got {type(data).__name__})"
        )
    harness = _coerce_harness(
        data.get("harness", data.get("agent_harness")),
        f"{name}.harness",
    )
    backend_raw = _coerce_str(data.get("backend"), f"{name}.backend")
    if not backend_raw:
        backend_raw = "pi" if harness == "pi-mono" else "subscription"
    if backend_raw not in _VALID_BACKENDS:
        raise ModelConfigError(
            f"{name}.backend = {backend_raw!r}; "
            f"expected one of {sorted(_VALID_BACKENDS)}"
        )
    backend: BackendName = backend_raw  # type: ignore[assignment]
    if harness is None:
        harness = "pi-mono" if backend == "pi" else "claude-code"
    if harness == "pi-mono" and backend != "pi":
        raise ModelConfigError(
            f"{name}.harness = 'pi-mono' requires {name}.backend = 'pi'"
        )
    if harness == "claude-code" and backend == "pi":
        raise ModelConfigError(
            f"{name}.backend = 'pi' requires {name}.harness = 'pi-mono'"
        )

    defaults = backends.get(backend, BackendDefaults())

    return BackendSpec(
        backend=backend,
        model=_coerce_str(data.get("model"), f"{name}.model"),
        harness=harness,
        region=_coerce_str(data.get("region"), f"{name}.region") or defaults.region,
        profile=_coerce_str(data.get("profile"), f"{name}.profile") or defaults.profile,
        base_url=_coerce_str(data.get("base_url"), f"{name}.base_url")
        or defaults.base_url,
    )


def from_mapping(data: Mapping[str, Any]) -> ModelConfig:
    """Parse the YAML body (a mapping) into a :class:`ModelConfig`."""
    backends_raw = data.get("backends") or {}
    if not isinstance(backends_raw, Mapping):
        raise ModelConfigError(
            f"'backends' must be a mapping (got {type(backends_raw).__name__})"
        )
    backends: dict[str, BackendDefaults] = {}
    for name, value in backends_raw.items():
        if not isinstance(value, Mapping):
            raise ModelConfigError(
                f"backends.{name} must be a mapping "
                f"(got {type(value).__name__})"
            )
        backends[str(name)] = _backend_defaults_from_dict(str(name), value)

    return ModelConfig(
        speaking=_hemisphere_spec("speaking", data.get("speaking"), backends),
        thinking=_hemisphere_spec("thinking", data.get("thinking"), backends),
        viewer=_hemisphere_spec("viewer", data.get("viewer"), backends),
        backends=backends,
    )


def load(mind_path: pathlib.Path) -> ModelConfig:
    """Load ``mind_path/config/model.yml``.

    File missing → :meth:`ModelConfig.subscription_default` (today's
    behaviour preserved). Parse error or schema mismatch →
    :class:`ModelConfigError` with a message that names the offending
    field.
    """
    import yaml  # imported lazily so cold-import stays light

    path = mind_path / "config" / MODEL_FILENAME
    if not path.is_file():
        return ModelConfig.subscription_default()
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ModelConfigError(f"failed to parse {path}: {exc}") from exc
    if raw is None:
        return ModelConfig.subscription_default()
    if not isinstance(raw, Mapping):
        raise ModelConfigError(
            f"{path} must contain a YAML mapping (got {type(raw).__name__})"
        )
    return from_mapping(raw)
