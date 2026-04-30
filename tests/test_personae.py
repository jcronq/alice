"""Phase A of the personae plan: ``alice_core.config.personae``.

Covers the loader's parsing surface end-to-end:

- minimal load (only required fields → defaults fill in)
- full load (every field present + parsed)
- missing-required-field raises a clear error
- malformed YAML raises a clear error
- ``placeholder()`` matches today's defaults

The downstream rendering (``meta.system_persona`` template) is
exercised separately in :mod:`tests.test_personae_template`.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from alice_core.config.personae import (
    AgentPersona,
    Personae,
    PersonaeError,
    UserPersona,
    from_mapping,
    load,
    placeholder,
)


def _write(mind: pathlib.Path, body: str) -> pathlib.Path:
    mind.mkdir(parents=True, exist_ok=True)
    path = mind / "personae.yml"
    path.write_text(body)
    return path


def test_load_minimal_personae(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        """
        agent:
          name: Eve
        user:
          name: Jordan
        """,
    )
    p = load(tmp_path)
    assert p.agent == AgentPersona(name="Eve")
    assert p.user == UserPersona(name="Jordan", addressing="first name")


def test_load_full_personae(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        """
        agent:
          name: Alice
          pronouns: she/her
          tagline: concise assistant
          lineage: Named for A.L.I.C.E., the 1995 chatbot.
          voice:
            summary: Executive-level assistant.
            rules:
              - Be helpful.
              - Have opinions.
        user:
          name: Jeremy
          pronouns: he/him
          addressing: first name
          relationship: friend
          about:
            - Software engineer.
            - EDT timezone.
        """,
    )
    p = load(tmp_path)
    assert p.agent.name == "Alice"
    assert p.agent.pronouns == "she/her"
    assert p.agent.tagline == "concise assistant"
    assert p.agent.lineage.startswith("Named for A.L.I.C.E.")
    assert p.agent.voice_summary == "Executive-level assistant."
    assert p.agent.voice_rules == ("Be helpful.", "Have opinions.")
    assert p.user.name == "Jeremy"
    assert p.user.pronouns == "he/him"
    assert p.user.relationship == "friend"
    assert p.user.about == ("Software engineer.", "EDT timezone.")


def test_load_missing_required_agent_name(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        """
        agent: {}
        user:
          name: Jordan
        """,
    )
    with pytest.raises(PersonaeError, match="agent.name"):
        load(tmp_path)


def test_load_missing_required_user_name(tmp_path: pathlib.Path) -> None:
    _write(
        tmp_path,
        """
        agent:
          name: Alice
        user: {}
        """,
    )
    with pytest.raises(PersonaeError, match="user.name"):
        load(tmp_path)


def test_load_missing_file_raises_filenotfound(tmp_path: pathlib.Path) -> None:
    with pytest.raises(FileNotFoundError):
        load(tmp_path)


def test_load_yaml_parse_error(tmp_path: pathlib.Path) -> None:
    _write(tmp_path, "agent: {name: : :")
    with pytest.raises(PersonaeError, match="failed to parse"):
        load(tmp_path)


def test_placeholder_matches_default_defaults() -> None:
    p = placeholder()
    assert p.agent.name == "Alice"
    assert p.user.name == "the operator"


def test_from_mapping_voice_rules_must_be_list() -> None:
    with pytest.raises(PersonaeError, match="agent.voice.rules"):
        from_mapping(
            {
                "agent": {"name": "Alice", "voice": {"rules": "not a list"}},
                "user": {"name": "Jordan"},
            }
        )


def test_as_template_context_shape() -> None:
    p = Personae(
        agent=AgentPersona(name="Eve", pronouns="she/her", voice_rules=("a", "b")),
        user=UserPersona(name="Jordan", about=("note",)),
    )
    ctx = p.as_template_context()
    assert ctx["agent"]["name"] == "Eve"
    assert ctx["agent"]["pronouns"] == "she/her"
    assert ctx["agent"]["voice_rules"] == ["a", "b"]
    assert ctx["user"]["name"] == "Jordan"
    assert ctx["user"]["about"] == ["note"]


def test_system_persona_template_renders_with_personae() -> None:
    """The meta.system_persona template renders cleanly given a
    full Personae's context. Smoke-checks the template + loader
    integration end-to-end."""
    from alice_prompts import load as load_prompt

    p = Personae(
        agent=AgentPersona(
            name="Eve",
            pronouns="she/her",
            tagline="terse assistant",
            voice_rules=("Be brief.", "No fluff."),
        ),
        user=UserPersona(name="Jordan", relationship="friend"),
    )
    out = load_prompt("meta.system_persona", **p.as_template_context())
    assert "Eve" in out
    assert "terse assistant" in out
    assert "Be brief." in out
    assert "Jordan" in out
    # Default Alice/operator placeholders must NOT leak through:
    assert "Alice" not in out
    assert "the operator" not in out


def test_safe_load_via_yaml_module_directly_smoke(tmp_path: pathlib.Path) -> None:
    """Sanity: yaml.safe_load on a string round-trips. Catches the
    case where pyyaml is unavailable in the test env (the recurring
    worker-Dockerfile trap from earlier plans)."""
    body = "agent:\n  name: Alice\nuser:\n  name: Friend\n"
    parsed = yaml.safe_load(body)
    assert parsed == {"agent": {"name": "Alice"}, "user": {"name": "Friend"}}
