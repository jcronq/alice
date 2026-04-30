"""Phase I of plan 05: tool descriptions interpolate the agent's name.

The substitution surface today is plain ``f"..."`` formatting at
``tools.<sub>.build(cfg, personae=...)`` time — not full Jinja
templates. The invariant the test pins: when ``personae.agent.name``
is something other than ``Alice``, the rendered tool descriptions
must reflect that, and must NOT contain the literal "Alice".
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from alice_core.config.personae import (
    AgentPersona,
    Personae,
    UserPersona,
)
from alice_speaking.tools import (
    config_tools as config_tools_module,
    inner as inner_module,
    memory as memory_module,
)


@dataclass
class _FakeConfig:
    mind_dir: pathlib.Path
    work_dir: pathlib.Path


def _personae(agent: str = "Eve", user: str = "Jordan") -> Personae:
    return Personae(
        agent=AgentPersona(name=agent),
        user=UserPersona(name=user),
    )


def test_inner_tool_description_uses_agent_name(tmp_path: pathlib.Path) -> None:
    cfg = _FakeConfig(mind_dir=tmp_path, work_dir=tmp_path)
    tools = inner_module.build(cfg, personae=_personae(agent="Eve"))
    write_directive = next(t for t in tools if t.name == "write_directive")
    assert "Eve" in write_directive.description
    assert "Alice" not in write_directive.description
    read_thoughts = next(t for t in tools if t.name == "read_thoughts")
    assert "Eve" in read_thoughts.description
    assert "Alice" not in read_thoughts.description


def test_memory_tool_description_uses_agent_name(tmp_path: pathlib.Path) -> None:
    cfg = _FakeConfig(mind_dir=tmp_path, work_dir=tmp_path)
    tools = memory_module.build(cfg, personae=_personae(agent="Eve"))
    read_memory = next(t for t in tools if t.name == "read_memory")
    assert "Eve" in read_memory.description
    assert "Alice" not in read_memory.description


def test_config_tool_description_uses_agent_name(tmp_path: pathlib.Path) -> None:
    cfg = _FakeConfig(mind_dir=tmp_path, work_dir=tmp_path)
    tools = config_tools_module.build(cfg, personae=_personae(agent="Eve"))
    read_cfg = next(t for t in tools if t.name == "read_config")
    write_cfg = next(t for t in tools if t.name == "write_config")
    assert "Eve" in read_cfg.description
    assert "Eve" in write_cfg.description
    assert "Alice" not in read_cfg.description
    assert "Alice" not in write_cfg.description


def test_default_personae_falls_back_to_alice(tmp_path: pathlib.Path) -> None:
    """No personae → placeholder personae (Alice / the operator).
    Existing call sites that don't load a Personae keep working."""
    cfg = _FakeConfig(mind_dir=tmp_path, work_dir=tmp_path)
    tools = memory_module.build(cfg)
    read_memory = next(t for t in tools if t.name == "read_memory")
    assert "Alice" in read_memory.description
