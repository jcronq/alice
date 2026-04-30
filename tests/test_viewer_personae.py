"""Phase J of plan 05: viewer chrome substitutes the personae name.

When ``personae.agent.name = "Eve"``, every template that previously
hardcoded "Alice" must render with "Eve" — the title, the header
brand, narrative copy, the empty-memory placeholder. The fixture
mind ships a real ``personae.yml`` (no placeholder fallback) so
the assertion is unambiguous.
"""

from __future__ import annotations

import pathlib

from fastapi.testclient import TestClient

from alice_viewer.main import create_app
from alice_viewer.settings import Paths


def _make_paths(tmp_path: pathlib.Path) -> Paths:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "thinking.log").write_text("")
    (state / "speaking.log").write_text("")
    inner_state = tmp_path / "inner" / "state"
    inner_state.mkdir(parents=True, exist_ok=True)
    (inner_state / "speaking-turns.jsonl").write_text("")
    return Paths(
        thinking_log=state / "thinking.log",
        speaking_log=state / "speaking.log",
        turn_log=inner_state / "speaking-turns.jsonl",
        mind_dir=tmp_path,
        state_dir=state,
    )


def test_index_title_uses_personae_agent_name(tmp_path: pathlib.Path) -> None:
    (tmp_path / "personae.yml").write_text(
        "agent:\n  name: Eve\nuser:\n  name: Jordan\n"
    )
    app = create_app(_make_paths(tmp_path))
    assert app.title == "Eve Viewer"
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # Header brand reads the personae.
    assert "Eve Viewer" in body
    # And the literal "Alice" must not appear in the rendered chrome —
    # if a future template hardcodes the name, this catches it.
    assert "Alice Viewer" not in body


def test_personae_missing_falls_back_to_placeholder(tmp_path: pathlib.Path) -> None:
    """No personae.yml on disk → placeholder personae (Alice). The
    viewer still renders, just with the legacy default."""
    app = create_app(_make_paths(tmp_path))
    assert app.title == "Alice Viewer"
