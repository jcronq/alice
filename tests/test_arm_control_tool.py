"""Tests for alice_speaking.tools.arm_control.

The arm-control tool is a thin HTTP client over the arm-daemon on the Pi.
Tests cover:

- ``arm_state`` returns the joint dict shape when given a mocked response.
- ``arm_goto`` accepts both a pose-name string and a joint dict, and
  builds the right POST body in each case.
- ``arm_choreo`` posts the choreo name correctly.
- ``arm_abort`` posts to the abort endpoint.
- Transport errors surface as tool-level errors (isError=True), not as
  raw stack traces.
- HTTP 409 (stalled joints) is treated as a successful call — the body
  is passed through rather than raised — because the daemon uses 409 to
  mean "motion completed but some joints stalled", not "transport
  failure".
- ``ALICE_ARM_DAEMON_URL`` env override is honoured.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from alice_speaking.tools import arm_control


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _handler(tools, name: str):
    """Look up a tool's async handler by name."""
    by_name = {t.name: t for t in tools}  # type: ignore[attr-defined]
    return by_name[name].handler  # type: ignore[attr-defined]


def _extract_text(result: dict) -> str:
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    return result["content"][0]["text"]


class _Capture:
    """Container that records every request seen by the mock handler."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []


@pytest.fixture
def install_mock_transport(monkeypatch: pytest.MonkeyPatch):
    """Install an httpx MockTransport by wrapping the AsyncClient constructor.

    The arm-control module builds a fresh AsyncClient per call (``async with``).
    We patch ``httpx.AsyncClient`` module-side so every one of those
    constructions transparently uses our transport. Returns a factory the
    test calls with a per-test response handler; the return value has a
    ``.calls`` list recording each request the client made.
    """

    def _factory(handler):
        capture = _Capture()

        def wrapped(request: httpx.Request) -> httpx.Response:
            capture.calls.append(request)
            return handler(request)

        transport = httpx.MockTransport(wrapped)
        real_cls = httpx.AsyncClient

        def _client(**kwargs: Any):
            kwargs["transport"] = transport
            return real_cls(**kwargs)

        monkeypatch.setattr(arm_control.httpx, "AsyncClient", _client)
        return capture

    return _factory


# --------------------------------------------------------------------------
# _base_url
# --------------------------------------------------------------------------


def test_base_url_defaults_to_pi_at_170(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ALICE_ARM_DAEMON_URL", raising=False)
    assert arm_control._base_url() == arm_control.DEFAULT_ARM_DAEMON_URL
    assert arm_control._base_url() == "http://10.20.30.170:8091"


def test_base_url_honours_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALICE_ARM_DAEMON_URL", "http://arm.example:9999/")
    # Trailing slash is stripped so ``{base}/state`` doesn't produce ``//state``.
    assert arm_control._base_url() == "http://arm.example:9999"


# --------------------------------------------------------------------------
# build() returns four tools with the expected names.
# --------------------------------------------------------------------------


def test_build_returns_expected_tool_names():
    tools = arm_control.build(cfg=None)
    names = sorted(t.name for t in tools)  # type: ignore[attr-defined]
    assert names == ["arm_abort", "arm_choreo", "arm_goto", "arm_state"]


# --------------------------------------------------------------------------
# arm_state
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm_state_returns_joint_dict(install_mock_transport):
    payload = {
        "joints": {
            "shoulder_pan": 0.12,
            "shoulder_lift": -84.9,
            "elbow_flex": 85.1,
            "wrist_flex": 0.0,
            "wrist_roll": 0.3,
            "gripper": 40.0,
        },
        "is_connected": True,
        "timestamp": "2026-07-09T02:15:00+00:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/state"
        return httpx.Response(200, json=payload)

    capture = install_mock_transport(handler)

    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_state")({})

    assert result.get("isError") is not True
    text = _extract_text(result)
    parsed = json.loads(text)
    assert parsed == payload
    assert len(capture.calls) == 1


@pytest.mark.asyncio
async def test_arm_state_transport_error_becomes_tool_error(install_mock_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        # Return a genuine transport-level failure the client will raise on.
        return httpx.Response(500, json={"ok": False, "error": "boom"})

    install_mock_transport(handler)

    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_state")({})

    assert result.get("isError") is True
    text = _extract_text(result)
    # We want a clean error string, not a raw stacktrace.
    assert text.startswith("error:")
    assert "Traceback" not in text


# --------------------------------------------------------------------------
# arm_goto — both string and dict pose
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm_goto_with_pose_name_posts_string(install_mock_transport):
    response_body = {"ok": True, "final": {"shoulder_pan": 0.0}, "stalled": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/goto_pose"
        body = json.loads(request.content)
        assert body == {"pose": "rest"}
        return httpx.Response(200, json=response_body)

    capture = install_mock_transport(handler)

    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_goto")({"pose": "rest"})

    assert result.get("isError") is not True
    parsed = json.loads(_extract_text(result))
    assert parsed == response_body
    assert len(capture.calls) == 1


@pytest.mark.asyncio
async def test_arm_goto_with_joint_dict_posts_dict(install_mock_transport):
    pose_dict = {
        "shoulder_pan": 0.0,
        "shoulder_lift": -30.0,
        "elbow_flex": 50.0,
        "wrist_flex": 0.0,
        "wrist_roll": 0.0,
        "gripper": 80.0,
    }
    response_body = {"ok": True, "final": pose_dict, "stalled": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/goto_pose"
        body = json.loads(request.content)
        assert body == {"pose": pose_dict}
        return httpx.Response(200, json=response_body)

    install_mock_transport(handler)

    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_goto")({"pose": pose_dict})

    assert result.get("isError") is not True
    parsed = json.loads(_extract_text(result))
    assert parsed == response_body


@pytest.mark.asyncio
async def test_arm_goto_missing_pose_errors(install_mock_transport):
    # No mock needed — should short-circuit before making an HTTP call.
    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_goto")({})
    assert result.get("isError") is True
    assert "missing 'pose'" in _extract_text(result)


@pytest.mark.asyncio
async def test_arm_goto_bad_pose_type_errors():
    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_goto")({"pose": 42})
    assert result.get("isError") is True
    assert "must be a string" in _extract_text(result)


@pytest.mark.asyncio
async def test_arm_goto_stall_409_is_passed_through(install_mock_transport):
    """409 = motion completed but joints stalled. Body carries the detail;
    we surface it as a normal (non-isError) result so the agent can decide
    what to do next."""
    stalled_body = {
        "ok": False,
        "final": {"elbow_flex": 95.0},
        "stalled": ["elbow_flex"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=stalled_body)

    install_mock_transport(handler)

    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_goto")({"pose": "rest"})

    # Not a tool error — the daemon successfully responded.
    assert result.get("isError") is not True
    parsed = json.loads(_extract_text(result))
    assert parsed == stalled_body
    assert "elbow_flex" in parsed["stalled"]


# --------------------------------------------------------------------------
# arm_choreo
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm_choreo_posts_name(install_mock_transport):
    response_body = {
        "ok": True,
        "choreo": "wave",
        "final": {"shoulder_pan": 0.0},
        "stalled": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/run_choreo"
        body = json.loads(request.content)
        assert body == {"name": "wave"}
        return httpx.Response(200, json=response_body)

    install_mock_transport(handler)

    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_choreo")({"name": "wave"})

    assert result.get("isError") is not True
    parsed = json.loads(_extract_text(result))
    assert parsed == response_body


@pytest.mark.asyncio
async def test_arm_choreo_strips_whitespace(install_mock_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"name": "wave"}
        return httpx.Response(200, json={"ok": True})

    install_mock_transport(handler)

    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_choreo")({"name": "  wave  "})
    assert result.get("isError") is not True


@pytest.mark.asyncio
async def test_arm_choreo_missing_name_errors():
    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_choreo")({})
    assert result.get("isError") is True
    assert "'name'" in _extract_text(result)


@pytest.mark.asyncio
async def test_arm_choreo_empty_name_errors():
    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_choreo")({"name": "   "})
    assert result.get("isError") is True


# --------------------------------------------------------------------------
# arm_abort
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm_abort_posts_to_abort(install_mock_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/abort"
        return httpx.Response(200, json={"ok": True, "aborted": True})

    install_mock_transport(handler)

    tools = arm_control.build(cfg=None)
    result = await _handler(tools, "arm_abort")({})

    assert result.get("isError") is not True
    parsed = json.loads(_extract_text(result))
    assert parsed == {"ok": True, "aborted": True}


# --------------------------------------------------------------------------
# Env override propagates through the client
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_override_changes_target_host(
    install_mock_transport, monkeypatch: pytest.MonkeyPatch
):
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"joints": {}, "is_connected": False,
                                          "timestamp": "x"})

    install_mock_transport(handler)
    monkeypatch.setenv("ALICE_ARM_DAEMON_URL", "http://custom.example:1234")

    tools = arm_control.build(cfg=None)
    await _handler(tools, "arm_state")({})
    assert seen_urls == ["http://custom.example:1234/state"]


# --------------------------------------------------------------------------
# Registry integration — arm_control tools show up in the alice server
# --------------------------------------------------------------------------


def test_arm_control_wired_into_tools_init():
    """arm_control must be imported and included in the tool_list built by
    alice_speaking.tools.__init__.build(). Guards against the "tool
    written but not wired" failure mode. We can't easily construct a real
    Config here (30+ required fields), so we inspect the source and the
    imported module directly.
    """
    from alice_speaking import tools as tools_pkg

    # Module import — otherwise the tool_list line below would NameError.
    assert hasattr(tools_pkg, "arm_control")
    assert tools_pkg.arm_control is arm_control

    # And the composition: arm_control.build should appear inside the
    # top-level build() function's source.
    import inspect

    src = inspect.getsource(tools_pkg.build)
    assert "arm_control.build" in src
