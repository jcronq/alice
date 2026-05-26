"""End-to-end integration tests for Phase 4 of the cozylobe motion-cortex
pipeline (#381).

Coverage:

* Subgraph pruning — 2-hop neighborhood from the motion trail returns
  the right subset of rooms.
* Subgraph snapshot includes sensors covering pruned rooms.
* Subgraph snapshot stays well under the 4000-token budget.
* Indexer's ``--cozylobe`` flag produces a separate DB at the
  cozylobe path.
* Default cue runner does NOT return cozylobe-cortex hits — only
  reads from the main cortex-index.db.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from alice_cozylobe.cortex import load_vault
from alice_cozylobe.motion import (
    SUBGRAPH_TOKEN_CEILING,
    MotionEvent,
    build_motion_prompt,
    build_subgraph_snapshot,
)
from alice_speaking.retrieval.cue_runner import (
    COZYLOBE_DB_PATH,
    COZYLOBE_VAULT_ROOT,
    VAULT_COZYLOBE,
    VAULT_DEFAULT,
    build_cue_packet,
    resolve_vault_paths,
)
from indexer.build_index import (
    build,
    main as indexer_main,
)


# ---------------------------------------------------------------------------
# Vault fixtures


def _write_room(
    vault: Path,
    title: str,
    *,
    adjacent: list[str] | None = None,
    sensors: list[str] | None = None,
) -> None:
    rooms_dir = vault / "rooms"
    rooms_dir.mkdir(parents=True, exist_ok=True)
    adj_line = ""
    if adjacent:
        adj_line = "adjacent: " + ", ".join(f"[[rooms/{a}]]" for a in adjacent) + "\n"
    sensor_line = ""
    if sensors:
        sensor_line = "sensors: " + ", ".join(f"[[sensors/{s}]]" for s in sensors) + "\n"
    body = (
        "---\n"
        f"title: {title}\n"
        "tags: [room, cozylobe-cortex]\n"
        "created: 2026-05-26\n"
        "updated: 2026-05-26\n"
        + adj_line
        + sensor_line
        + "---\n\n"
        f"# {title}\n\nTest room.\n"
    )
    (rooms_dir / f"{title}.md").write_text(body, encoding="utf-8")


def _write_sensor(vault: Path, entity_id: str, room: str) -> None:
    sdir = vault / "sensors"
    sdir.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        f"title: {entity_id}\n"
        "tags: [sensor, motion, cozylobe-cortex]\n"
        "created: 2026-05-26\n"
        "updated: 2026-05-26\n"
        f"room: \"[[rooms/{room}]]\"\n"
        "---\n\n"
        f"# {entity_id}\n\nTest sensor.\n"
    )
    (sdir / f"{entity_id}.md").write_text(body, encoding="utf-8")


def _write_person(vault: Path, name: str, time_patterns: list[str]) -> None:
    pdir = vault / "people"
    pdir.mkdir(parents=True, exist_ok=True)
    tp_line = (
        "time_patterns: "
        + ", ".join(f"[[destinations/{t}]]" for t in time_patterns)
        + "\n"
        if time_patterns
        else ""
    )
    body = (
        "---\n"
        f"title: {name}\n"
        "tags: [person, resident, cozylobe-cortex]\n"
        "created: 2026-05-26\n"
        "updated: 2026-05-26\n"
        + tp_line
        + "---\n\n"
        f"# {name}\n"
    )
    (pdir / f"{name}.md").write_text(body, encoding="utf-8")


def _write_destination(
    vault: Path, title: str, person: str, room: str, time_window: str
) -> None:
    ddir = vault / "destinations"
    ddir.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        f"title: {title}\n"
        "tags: [destination, cozylobe-cortex]\n"
        "created: 2026-05-26\n"
        "updated: 2026-05-26\n"
        f"person: \"[[people/{person}]]\"\n"
        f"room: \"[[rooms/{room}]]\"\n"
        f"time_window: {time_window}\n"
        "---\n\n"
        f"# {title}\n"
    )
    (ddir / f"{title}.md").write_text(body, encoding="utf-8")


@pytest.fixture
def vault_path(tmp_path: Path) -> Path:
    """House layout:

        Bedroom — Hallway — Kitchen — Dining Room
                              |
                            Office
        Gym  (isolated)
    """
    _write_room(tmp_path, "Bedroom", adjacent=["Hallway"])
    _write_room(
        tmp_path,
        "Hallway",
        adjacent=["Bedroom", "Kitchen"],
        sensors=["hue_hallway_motion"],
    )
    _write_room(
        tmp_path,
        "Kitchen",
        adjacent=["Hallway", "Dining Room", "Office"],
        sensors=["hue_kitchen_motion"],
    )
    _write_room(
        tmp_path, "Dining Room", adjacent=["Kitchen"], sensors=["hue_dining_motion"]
    )
    _write_room(tmp_path, "Office", adjacent=["Kitchen"], sensors=["hue_office_motion"])
    _write_room(tmp_path, "Gym", sensors=["hue_gym_motion"])

    _write_sensor(tmp_path, "hue_hallway_motion", "Hallway")
    _write_sensor(tmp_path, "hue_kitchen_motion", "Kitchen")
    _write_sensor(tmp_path, "hue_dining_motion", "Dining Room")
    _write_sensor(tmp_path, "hue_office_motion", "Office")
    _write_sensor(tmp_path, "hue_gym_motion", "Gym")

    _write_person(tmp_path, "Jason", ["Kitchen-at-07-00"])
    _write_destination(tmp_path, "Kitchen-at-07-00", "Jason", "Kitchen", "06:30-08:00")

    return tmp_path


def _motion(room: str, ts: float = 1000.0, entity: str | None = None) -> MotionEvent:
    return MotionEvent(
        timestamp=ts,
        entity_id=entity or f"hue_{room.lower().replace(' ', '_')}_motion",
        state="on",
        room_id=room,
    )


# ---------------------------------------------------------------------------
# Subgraph pruning


def test_subgraph_seed_only_returns_one_hop_neighbors(vault_path: Path):
    """With hops=1 and a single Kitchen event, the snapshot should
    include Kitchen + its direct neighbors (Hallway, Dining Room,
    Office) and NOT include Bedroom (2 hops away) or Gym (isolated)."""
    vault = load_vault(vault_path)
    trail = [_motion("Kitchen")]
    snapshot = build_subgraph_snapshot(vault, [], trail, hops=1)
    rooms = set(snapshot["rooms"])
    assert "Kitchen" in rooms
    assert "Hallway" in rooms
    assert "Dining Room" in rooms
    assert "Office" in rooms
    assert "Bedroom" not in rooms
    assert "Gym" not in rooms


def test_subgraph_two_hop_reaches_bedroom(vault_path: Path):
    """With hops=2 and a Kitchen seed, the snapshot includes Bedroom
    (Kitchen → Hallway → Bedroom) but still excludes the isolated Gym."""
    vault = load_vault(vault_path)
    trail = [_motion("Kitchen")]
    snapshot = build_subgraph_snapshot(vault, [], trail, hops=2)
    rooms = set(snapshot["rooms"])
    assert "Bedroom" in rooms
    assert "Gym" not in rooms


def test_subgraph_sensors_pruned_to_neighborhood(vault_path: Path):
    """Sensors covering rooms outside the 2-hop subgraph must be
    dropped from the snapshot."""
    vault = load_vault(vault_path)
    trail = [_motion("Kitchen")]
    snapshot = build_subgraph_snapshot(vault, [], trail, hops=2)
    sensor_ids = {s["entity_id"] for s in snapshot["sensors"]}
    # Gym is isolated — its sensor should be excluded.
    assert "hue_gym_motion" not in sensor_ids
    # Kitchen + its neighbors should be included.
    assert "hue_kitchen_motion" in sensor_ids
    assert "hue_hallway_motion" in sensor_ids
    assert "hue_office_motion" in sensor_ids


def test_subgraph_empty_trail_falls_back_to_minimal(vault_path: Path):
    """With no seed rooms, the snapshot is a minimal whole-vault dump —
    rooms + sensors + adjacency. Keeps the classifier grounded on the
    very first event before the trail has anything to seed from."""
    vault = load_vault(vault_path)
    snapshot = build_subgraph_snapshot(vault, [], [])
    assert "Kitchen" in snapshot["rooms"]
    assert "Gym" in snapshot["rooms"]
    sensor_ids = {s["entity_id"] for s in snapshot["sensors"]}
    assert "hue_kitchen_motion" in sensor_ids
    assert "hue_gym_motion" in sensor_ids


def test_subgraph_people_priors_filter_by_hour(vault_path: Path):
    """Jason's Kitchen-at-07:00 destination should surface only when
    now_hour is inside the 06:30-08:00 window."""
    vault = load_vault(vault_path)
    trail = [_motion("Kitchen")]
    in_window = build_subgraph_snapshot(vault, [], trail, hops=1, now_hour=7)
    out_of_window = build_subgraph_snapshot(vault, [], trail, hops=1, now_hour=22)
    assert any(
        p["person"] == "Jason" for p in in_window["people_priors"]
    )
    assert out_of_window["people_priors"] == []


def test_subgraph_handles_missing_vault():
    """vault=None must produce an empty snapshot, not crash."""
    snapshot = build_subgraph_snapshot(None, [], [_motion("Kitchen")])
    assert snapshot == {
        "rooms": [],
        "sensors": [],
        "adjacency": {},
        "people_priors": [],
    }


def test_subgraph_stays_under_token_budget(vault_path: Path):
    """The snapshot for a normal house layout must fit well under the
    4000-token soft ceiling. We approximate len(json) / 4 as the
    token estimate."""
    vault = load_vault(vault_path)
    trail = [_motion("Kitchen")]
    snapshot = build_subgraph_snapshot(vault, [], trail, hops=2, now_hour=7)
    payload = json.dumps(snapshot, separators=(",", ":"))
    estimated_tokens = len(payload) // 4
    assert estimated_tokens < SUBGRAPH_TOKEN_CEILING, (
        f"snapshot too large: {estimated_tokens} tokens "
        f"(ceiling={SUBGRAPH_TOKEN_CEILING})"
    )


def test_build_motion_prompt_includes_subgraph(vault_path: Path):
    """The full classify prompt must carry a CORTEX_STATE section
    derived from the pruned subgraph (not the whole vault)."""
    vault = load_vault(vault_path)
    trail = [_motion("Kitchen")]
    prompt = build_motion_prompt([], trail, vault, subgraph_hops=1)
    assert "CORTEX_STATE:" in prompt
    # The Gym is isolated and 1-hop from Kitchen does not reach it, so
    # the Gym must NOT appear in the rooms list. (Bedroom appears as an
    # adjacency *target* via Hallway → Bedroom edges; that's expected.)
    assert '"Gym"' not in prompt


# ---------------------------------------------------------------------------
# Indexer integration


def test_indexer_cozylobe_flag_routes_to_cozylobe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``--cozylobe`` with no overrides should default to the
    cozylobe-cortex vault + DB paths (via the env var when set)."""
    vault = tmp_path / "cozylobe-vault"
    vault.mkdir()
    (vault / "alpha.md").write_text(
        "---\ntitle: Alpha\ntags: [test]\n---\n\nKitchen prose.\n"
    )
    db_dir = tmp_path / "state"
    db_dir.mkdir()
    db_path = db_dir / "cozylobe-cortex-index.db"

    monkeypatch.setenv("COZYLOBE_CORTEX_ROOT", str(vault))
    # Override the default cozylobe DB path so we don't clobber a real
    # state file outside the test sandbox.
    monkeypatch.setattr(
        "indexer.build_index.DEFAULT_COZYLOBE_DB", db_path
    )
    rc = indexer_main(["--cozylobe", "--quiet"])
    assert rc == 0
    assert db_path.is_file()

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT slug FROM notes").fetchall()
    finally:
        conn.close()
    assert ("alpha",) in rows


def test_indexer_default_run_does_not_touch_cozylobe_db(tmp_path: Path):
    """A regular indexer build of the main vault must not write the
    cozylobe DB path — they're separate files."""
    main_vault = tmp_path / "vault"
    main_vault.mkdir()
    (main_vault / "alpha.md").write_text(
        "---\ntitle: Alpha\n---\n\nHello.\n"
    )
    main_db = tmp_path / "cortex-index.db"
    cozylobe_db = tmp_path / "cozylobe-cortex-index.db"
    build(main_vault, main_db)
    assert main_db.is_file()
    assert not cozylobe_db.exists()


def test_indexer_two_indices_dont_share_state(tmp_path: Path):
    """Build both indices into separate DB files; each must contain
    only its own vault's notes."""
    main_vault = tmp_path / "vault"
    main_vault.mkdir()
    (main_vault / "main-note.md").write_text(
        "---\ntitle: Main Note\n---\n\nFoo.\n"
    )
    cozylobe_vault = tmp_path / "cozylobe-vault"
    cozylobe_vault.mkdir()
    (cozylobe_vault / "lobe-note.md").write_text(
        "---\ntitle: Lobe Note\n---\n\nBar.\n"
    )

    main_db = tmp_path / "main.db"
    cozylobe_db = tmp_path / "cozylobe.db"
    build(main_vault, main_db)
    build(cozylobe_vault, cozylobe_db)

    def slugs(db_path: Path) -> set[str]:
        conn = sqlite3.connect(str(db_path))
        try:
            return {r[0] for r in conn.execute("SELECT slug FROM notes")}
        finally:
            conn.close()

    assert slugs(main_db) == {"main-note"}
    assert slugs(cozylobe_db) == {"lobe-note"}


# ---------------------------------------------------------------------------
# Cue runner privacy isolation


def test_resolve_vault_paths_default():
    db, vault = resolve_vault_paths(VAULT_DEFAULT)
    assert "cortex-memory" in str(vault)
    assert "cozylobe-cortex" not in str(vault)
    assert "cozylobe-cortex" not in str(db)


def test_resolve_vault_paths_cozylobe():
    db, vault = resolve_vault_paths(VAULT_COZYLOBE)
    assert vault == COZYLOBE_VAULT_ROOT
    assert db == COZYLOBE_DB_PATH


def test_resolve_vault_paths_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_vault_paths("not-a-vault")


def _build_seeded_index(vault: Path, db: Path, notes: dict[str, str]) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    for name, body in notes.items():
        (vault / f"{name}.md").write_text(
            f"---\ntitle: {name}\n---\n\n{body}\n", encoding="utf-8"
        )
    build(vault, db)


def test_default_cue_runner_does_not_return_cozylobe_hits(tmp_path: Path):
    """Seed two separate indices. Calling build_cue_packet with the
    DEFAULT (main) DB must return hits only from the main vault, even
    when the query word also matches cozylobe-cortex content."""
    main_vault = tmp_path / "main-vault"
    main_db = tmp_path / "main.db"
    _build_seeded_index(
        main_vault,
        main_db,
        {
            "kitchen-recipe": "How to roast vegetables in the kitchen.",
            "fitness-log": "Bench press tracking notes.",
        },
    )

    cozylobe_vault = tmp_path / "cozylobe-vault"
    cozylobe_db = tmp_path / "cozylobe.db"
    _build_seeded_index(
        cozylobe_vault,
        cozylobe_db,
        {
            "kitchen-motion": "Motion sensor in kitchen.",
            "office-motion": "Motion sensor in office.",
        },
    )

    cfg = {"enabled": True}

    async def run() -> str:
        return await build_cue_packet(
            "kitchen",
            cfg,
            db_path=main_db,
            vault_root=main_vault,
        )

    packet = asyncio.run(run())
    assert packet, "expected a non-empty packet for the 'kitchen' query"
    # Main-vault note should be the match.
    assert "kitchen-recipe" in packet
    # cozylobe-cortex notes MUST NOT appear in the default-path packet.
    assert "kitchen-motion" not in packet
    assert "office-motion" not in packet


def test_explicit_cozylobe_path_does_return_cozylobe_hits(tmp_path: Path):
    """When a caller explicitly points the cue runner at the cozylobe
    DB + vault, they get cozylobe hits — opt-in. This pairs with the
    privacy test above: the default path stays clean BECAUSE we make
    callers ask for the cozylobe-cortex index by name."""
    cozylobe_vault = tmp_path / "cozylobe-vault"
    cozylobe_db = tmp_path / "cozylobe.db"
    _build_seeded_index(
        cozylobe_vault,
        cozylobe_db,
        {
            "kitchen-motion": "Motion sensor in kitchen.",
        },
    )

    cfg = {"enabled": True}

    async def run() -> str:
        return await build_cue_packet(
            "kitchen",
            cfg,
            db_path=cozylobe_db,
            vault_root=cozylobe_vault,
        )

    packet = asyncio.run(run())
    assert packet
    assert "kitchen-motion" in packet
