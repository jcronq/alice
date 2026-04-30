"""Personae loader — agent + user identity from ``mind/personae.yml``.

Plan 05 Phase 1 of the runtime refactor. The mind ships a YAML file
that names the agent (today's default: Alice) and the user. Both
hemispheres render that into a system-prompt fragment via the
``meta.system_persona`` template; ``alice_prompts`` exposes the same
``agent`` / ``user`` keys as context defaults so any prompt template
can reference ``{{ agent.name }}`` / ``{{ user.name }}``.

The loader is small + dumb on purpose: read YAML, raise a clear error
on missing required fields, return frozen dataclasses. The caller
decides what to do with a missing file (the speaking + thinking
factories fall back to a placeholder personae that matches today's
behaviour).

Schema lives at ``docs/refactor/05-personae-and-injection.md``. In
short:

    agent:
      name: Alice                 # required
      pronouns: she/her           # optional
      tagline: "..."              # optional
      lineage: "..."              # optional
      voice:
        summary: "..."            # optional
        rules: ["...", "..."]     # optional
    user:
      name: Friend                # required
      pronouns: he/him            # optional
      addressing: "first name"    # optional
      honorific: null             # optional, used when addressing == honorific
      relationship: "friend"      # optional
      about: ["...", "..."]       # optional
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Mapping


__all__ = [
    "AgentPersona",
    "Personae",
    "PersonaeError",
    "UserPersona",
    "from_mapping",
    "load",
    "placeholder",
]


PERSONAE_FILENAME = "personae.yml"


class PersonaeError(ValueError):
    """Raised on missing required fields, YAML parse errors, etc."""


@dataclass(frozen=True)
class AgentPersona:
    name: str
    pronouns: str = ""
    tagline: str = ""
    lineage: str = ""
    voice_summary: str = ""
    voice_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class UserPersona:
    name: str
    pronouns: str = ""
    addressing: str = "first name"
    honorific: str = ""
    relationship: str = ""
    about: tuple[str, ...] = ()


@dataclass(frozen=True)
class Personae:
    agent: AgentPersona
    user: UserPersona

    def as_template_context(self) -> dict[str, Any]:
        """Return the dict shape ``alice_prompts`` consumes as
        ``context_defaults``. Templates reference
        ``{{ agent.name }}`` / ``{{ user.name }}``; the dict mirrors
        the dataclass field names directly."""
        return {
            "agent": {
                "name": self.agent.name,
                "pronouns": self.agent.pronouns,
                "tagline": self.agent.tagline,
                "lineage": self.agent.lineage,
                "voice_summary": self.agent.voice_summary,
                "voice_rules": list(self.agent.voice_rules),
            },
            "user": {
                "name": self.user.name,
                "pronouns": self.user.pronouns,
                "addressing": self.user.addressing,
                "honorific": self.user.honorific,
                "relationship": self.user.relationship,
                "about": list(self.user.about),
            },
        }


def placeholder() -> Personae:
    """Stand-in personae used when ``personae.yml`` is missing.

    Matches today's behaviour: agent is "Alice", user is "the
    operator". Once the operator drops a real ``personae.yml`` into
    the mind, the loader picks it up and these defaults retire.
    """
    return Personae(
        agent=AgentPersona(name="Alice"),
        user=UserPersona(name="the operator"),
    )


def _coerce_str(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise PersonaeError(
        f"{field_name!r} must be a string (got {type(value).__name__})"
    )


def _coerce_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PersonaeError(
            f"{field_name!r} must be a list of strings "
            f"(got {type(value).__name__})"
        )
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise PersonaeError(
                f"{field_name}[{i}] must be a string "
                f"(got {type(item).__name__})"
            )
        out.append(item)
    return tuple(out)


def _agent_from_dict(data: Mapping[str, Any]) -> AgentPersona:
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PersonaeError("agent.name is required (non-empty string)")
    voice = data.get("voice") or {}
    if not isinstance(voice, Mapping):
        raise PersonaeError(
            f"agent.voice must be a mapping (got {type(voice).__name__})"
        )
    return AgentPersona(
        name=name.strip(),
        pronouns=_coerce_str(data.get("pronouns"), "agent.pronouns"),
        tagline=_coerce_str(data.get("tagline"), "agent.tagline"),
        lineage=_coerce_str(data.get("lineage"), "agent.lineage"),
        voice_summary=_coerce_str(voice.get("summary"), "agent.voice.summary"),
        voice_rules=_coerce_string_tuple(voice.get("rules"), "agent.voice.rules"),
    )


def _user_from_dict(data: Mapping[str, Any]) -> UserPersona:
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PersonaeError("user.name is required (non-empty string)")
    addressing = _coerce_str(data.get("addressing"), "user.addressing") or "first name"
    return UserPersona(
        name=name.strip(),
        pronouns=_coerce_str(data.get("pronouns"), "user.pronouns"),
        addressing=addressing,
        honorific=_coerce_str(data.get("honorific"), "user.honorific"),
        relationship=_coerce_str(data.get("relationship"), "user.relationship"),
        about=_coerce_string_tuple(data.get("about"), "user.about"),
    )


def from_mapping(data: Mapping[str, Any]) -> Personae:
    """Parse an in-memory mapping (the YAML body) into a :class:`Personae`.

    Useful for tests that don't want to touch the filesystem.
    """
    agent_data = data.get("agent")
    if not isinstance(agent_data, Mapping):
        raise PersonaeError("personae.yml must have a top-level 'agent' mapping")
    user_data = data.get("user")
    if not isinstance(user_data, Mapping):
        raise PersonaeError("personae.yml must have a top-level 'user' mapping")
    return Personae(
        agent=_agent_from_dict(agent_data),
        user=_user_from_dict(user_data),
    )


def load(mind_path: pathlib.Path) -> Personae:
    """Load ``mind_path/personae.yml`` and return a :class:`Personae`.

    Raises :class:`FileNotFoundError` if the file is absent (caller
    decides whether to fall back to :func:`placeholder`). Raises
    :class:`PersonaeError` on YAML parse errors or missing required
    fields, with a message that names the offending field.
    """
    import yaml  # imported lazily so cold-import of this module stays light

    path = mind_path / PERSONAE_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"personae file missing: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PersonaeError(f"failed to parse {path}: {exc}") from exc
    if raw is None:
        raise PersonaeError(f"{path} is empty")
    if not isinstance(raw, Mapping):
        raise PersonaeError(
            f"{path} must contain a YAML mapping (got {type(raw).__name__})"
        )
    return from_mapping(raw)
