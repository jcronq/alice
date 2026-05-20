"""System-prompt composition for the speaking-quality eval.

The replay harness needs the same persona-flavoured system prompt
the live speaking daemon ships, minus the runtime-specific surface
(MCP tool definitions, channel capability cards). The closest thing
in-tree is :func:`prompts.load` with ``meta.system_persona``;
we render that with personae sourced from one of two places:

1. ``templates/mind-scaffold/personae.yml`` — the bundled mind
   scaffold. This is what new minds clone from; in development it's
   the most reliable source of "Alice talking to <user>" context.
2. A hardcoded minimal fallback when the scaffold is missing
   (e.g. running the harness outside the repo).

The harness *intentionally* does not try to render the per-channel
turn template — those templates expect runtime fields like
``capability``/``channel.transport`` and reach into infra we don't
need for an offline replay. The user message is the inbound text,
passed verbatim through PII redaction.

Production note: the speaking daemon constructs its own
PromptLoader with the deployed mind's override path
(``src/alice_speaking/factory.py``). The harness uses the default
package-level loader. Anyone wanting the override path can call
:func:`build_system_prompt` with their own ``loader=`` and
``personae=`` to inject the deployed scaffold's personae file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

import prompts

__all__ = [
    "DEFAULT_PERSONAE_PATH",
    "FALLBACK_PERSONAE",
    "build_system_prompt",
    "load_personae",
]


log = logging.getLogger(__name__)

# Resolve the scaffold relative to the repo root. ``src/eval/``
# sits two levels under the repo root; ``templates/`` is a sibling
# of ``src``. The lookup tolerates the file being missing.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PERSONAE_PATH = _REPO_ROOT / "templates" / "mind-scaffold" / "personae.yml"

# Fallback used when the scaffold isn't on disk. Mirrors Alice's
# CLAUDE.md identity block so the system prompt still sounds right.
FALLBACK_PERSONAE: dict[str, Any] = {
    "agent": {
        "name": "Alice",
        "pronouns": "she/her",
        "tagline": "concise assistant with opinions",
        "lineage": (
            "Named for A.L.I.C.E., the 1995 chatbot that proved "
            "machines could hold a conversation."
        ),
        "voice_summary": (
            "Executive-level assistant with department-head competence. "
            "No fluff, just results."
        ),
        "voice_rules": [
            "Be genuinely helpful, not performatively helpful.",
            "Have opinions.",
            "Be resourceful before asking.",
            "One task, one reply.",
        ],
    },
    "user": {
        "name": "Jason",
        "pronouns": "he/him",
        "relationship": "operator",
        "about": [
            "Software engineer building Alice.",
            "Communicates primarily via Signal.",
        ],
    },
}


def load_personae(path: str | Path | None = None) -> dict[str, Any]:
    """Read a personae YAML and shape it for the system-persona
    template.

    Returns a dict with ``agent`` and ``user`` keys, matching the
    Jinja context the ``meta.system_persona`` template expects. On
    any failure (missing file, parse error, missing keys) falls back
    to :data:`FALLBACK_PERSONAE` with a warning.
    """
    candidate = Path(path or DEFAULT_PERSONAE_PATH).expanduser()
    if not candidate.is_file():
        log.warning(
            "personae.yml not found at %s — using hardcoded fallback",
            candidate,
        )
        return FALLBACK_PERSONAE

    try:
        raw = yaml.safe_load(candidate.read_text())
    except (yaml.YAMLError, OSError) as exc:
        log.warning(
            "failed to load personae.yml at %s (%s) — using fallback",
            candidate,
            exc,
        )
        return FALLBACK_PERSONAE

    if not isinstance(raw, dict) or "agent" not in raw or "user" not in raw:
        log.warning(
            "personae.yml at %s missing agent/user keys — using fallback",
            candidate,
        )
        return FALLBACK_PERSONAE

    agent = dict(raw["agent"])
    user = dict(raw["user"])

    # ``meta.system_persona`` references ``agent.voice_summary`` and
    # ``agent.voice_rules`` at the top level — the scaffold nests
    # them under ``agent.voice``. Flatten so the template renders.
    voice = agent.pop("voice", None) or {}
    if isinstance(voice, dict):
        if voice.get("summary") and "voice_summary" not in agent:
            agent["voice_summary"] = voice["summary"]
        if voice.get("rules") and "voice_rules" not in agent:
            agent["voice_rules"] = list(voice["rules"])

    return {"agent": agent, "user": user}


def build_system_prompt(
    personae: dict[str, Any] | None = None,
    personae_path: str | Path | None = None,
) -> str:
    """Render the ``meta.system_persona`` template into a string.

    ``personae`` wins over ``personae_path``; if both are ``None`` we
    pull from :data:`DEFAULT_PERSONAE_PATH` (falling back to
    :data:`FALLBACK_PERSONAE`).
    """
    if personae is None:
        personae = load_personae(personae_path)

    return prompts.load("meta.system_persona", **personae)
