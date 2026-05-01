"""Model + backend config loader — ``mind/config/model.yml``.

Plan 06 Phase 1 of the runtime refactor. The runtime supports three
LLM backends through the Claude Agent SDK:

- ``subscription`` — Anthropic Max OAuth (today's default).
- ``api`` — Anthropic API key, optionally via a LiteLLM proxy.
- ``bedrock`` — AWS Bedrock via ``CLAUDE_CODE_USE_BEDROCK=1``.

Each hemisphere (speaking, thinking, viewer) picks its own backend +
model. ``mind/config/model.yml`` is the source of truth; if missing,
the loader returns :class:`ModelConfig.subscription_default()` so
existing minds keep working unchanged.

Schema (full form):

    speaking:
      backend: subscription
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
    "ModelConfig",
    "ModelConfigError",
    "from_mapping",
    "load",
]


MODEL_FILENAME = "model.yml"
HEMISPHERES: tuple[str, ...] = ("speaking", "thinking", "viewer")
BackendName = Literal["subscription", "api", "bedrock", "pi"]
_VALID_BACKENDS: frozenset[str] = frozenset({"subscription", "api", "bedrock", "pi"})


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
            speaking=BackendSpec(backend="subscription"),
            thinking=BackendSpec(backend="subscription"),
            viewer=BackendSpec(backend="subscription"),
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
        return BackendSpec(backend="subscription")
    if not isinstance(data, Mapping):
        raise ModelConfigError(
            f"{name!r} must be a mapping (got {type(data).__name__})"
        )
    backend_raw = _coerce_str(data.get("backend"), f"{name}.backend") or "subscription"
    if backend_raw not in _VALID_BACKENDS:
        raise ModelConfigError(
            f"{name}.backend = {backend_raw!r}; "
            f"expected one of {sorted(_VALID_BACKENDS)}"
        )
    backend: BackendName = backend_raw  # type: ignore[assignment]

    defaults = backends.get(backend, BackendDefaults())

    return BackendSpec(
        backend=backend,
        model=_coerce_str(data.get("model"), f"{name}.model"),
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
