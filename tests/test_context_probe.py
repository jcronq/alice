"""Unit tests for alice_speaking.diagnostics.context_probe.

The probe is constructed with callable accessors so the real daemon
can update its state independently. Tests pass small lambdas to
exercise the snapshot shape without standing up a daemon.
"""

from __future__ import annotations

from alice_speaking.diagnostics import ContextProbe


def _build(**overrides):
    """Return a probe with sensible defaults for every accessor.

    Tests override individual accessors via kwargs to check that
    snapshot() reflects the live state, not a value at construction
    time.
    """
    defaults = {
        "get_system_prompt": lambda: "you are alice.",
        "get_builtin_tools": lambda: ["Bash", "Read"],
        "get_custom_tool_names": lambda: [
            "mcp__alice__send_message",
            "mcp__alice__resolve_surface",
            "mcp__memory__search",
        ],
        "get_mcp_servers": lambda: {
            "alice": {"type": "stdio"},
            "memory": {"type": "http"},
        },
        "get_session_id": lambda: "sess-abc",
        "get_pending_preamble": lambda: None,
        "get_current_turn_kind": lambda: None,
        "get_model": lambda: "claude-sonnet-5",
        "get_backend": lambda: "subscription",
        "get_mind_dir": lambda: "/home/alice/alice-mind",
        "get_skills_cwd": lambda: "/state/alice-skills/speaking",
    }
    defaults.update(overrides)
    return ContextProbe(**defaults)


def test_snapshot_includes_text_by_default():
    probe = _build()
    snap = probe.snapshot()
    assert snap.system_prompt["chars"] == len("you are alice.")
    assert snap.system_prompt["text"] == "you are alice."


def test_snapshot_can_omit_text():
    probe = _build()
    snap = probe.snapshot(include_text=False)
    assert snap.system_prompt["chars"] == len("you are alice.")
    assert snap.system_prompt["text"] is None


def test_tools_count_combines_builtin_and_custom():
    probe = _build()
    snap = probe.snapshot()
    assert snap.tools["builtin"] == ["Bash", "Read"]
    assert "mcp__alice__send_message" in snap.tools["custom"]
    assert snap.tools["count"] == 2 + 3


def test_mcp_servers_groups_tools_by_prefix():
    probe = _build()
    snap = probe.snapshot()
    assert set(snap.mcp_servers.keys()) == {"alice", "memory"}
    alice = snap.mcp_servers["alice"]
    assert alice["type"] == "stdio"
    assert alice["tool_count"] == 2
    assert sorted(alice["tool_names"]) == ["resolve_surface", "send_message"]
    # Without get_mcp_tool_defs wired, every server gets an empty
    # ``tools`` list — the snapshot consumer can fall back to names.
    assert alice["tools"] == []
    memory = snap.mcp_servers["memory"]
    assert memory["type"] == "http"
    assert memory["tool_count"] == 1
    assert memory["tool_names"] == ["search"]
    assert memory["tools"] == []


def test_mcp_servers_carries_full_tool_defs_when_wired():
    """When the daemon supplies tool_defs, each server's entry exposes
    name + description + input_schema for every tool — that's what backs
    the viewer's per-tool inspector."""
    defs = {
        "alice": [
            {
                "name": "send_message",
                "description": "Send a message to a recipient.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "recipient": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["recipient", "text"],
                },
            },
        ],
    }
    probe = _build(get_mcp_tool_defs=lambda: defs)
    snap = probe.snapshot()
    alice = snap.mcp_servers["alice"]
    assert alice["tools"][0]["name"] == "send_message"
    assert "Send a message" in alice["tools"][0]["description"]
    assert alice["tools"][0]["input_schema"]["properties"]["recipient"]["type"] == "string"


def test_mcp_servers_shows_unconfigured_servers_from_tool_prefixes():
    """If a tool is registered under a server not in mcp_servers
    (config-vs-runtime drift), surface the server with type=unknown so
    the operator can see the discrepancy."""
    probe = _build(
        get_custom_tool_names=lambda: ["mcp__ghost__do_something"],
        get_mcp_servers=lambda: {},
    )
    snap = probe.snapshot()
    assert "ghost" in snap.mcp_servers
    assert snap.mcp_servers["ghost"]["type"] == "unknown"
    assert snap.mcp_servers["ghost"]["tool_names"] == ["do_something"]


def test_pending_preamble_is_none_when_empty():
    probe = _build(get_pending_preamble=lambda: None)
    snap = probe.snapshot()
    assert snap.pending_preamble is None


def test_pending_preamble_includes_text_and_chars():
    probe = _build(get_pending_preamble=lambda: "previous turns: ...")
    snap = probe.snapshot()
    assert snap.pending_preamble == {
        "chars": len("previous turns: ..."),
        "text": "previous turns: ...",
    }


def test_in_flight_set_when_turn_kind_present():
    probe = _build(get_current_turn_kind=lambda: "cli")
    snap = probe.snapshot()
    assert snap.in_flight == {"turn_kind": "cli"}


def test_in_flight_none_when_idle():
    probe = _build(get_current_turn_kind=lambda: None)
    snap = probe.snapshot()
    assert snap.in_flight is None


def test_accessors_evaluated_at_snapshot_time_not_construction():
    """Mutating state after construction must show up in the next
    snapshot — that's the entire point of using callables."""
    state = {"session_id": "first"}
    probe = _build(get_session_id=lambda: state["session_id"])
    assert probe.snapshot().session_id == "first"
    state["session_id"] = "second"
    assert probe.snapshot().session_id == "second"


def test_to_dict_roundtrips_through_dataclass_asdict():
    probe = _build()
    snap = probe.snapshot()
    d = snap.to_dict()
    assert d["session_id"] == "sess-abc"
    assert d["model"] == "claude-sonnet-5"
    assert d["tools"]["count"] == 5
