"""Smoke tests for :mod:`alice_speaking.infra.face_caption`.

Covers the deterministic helpers — the caption truncator, the YAML
frontmatter strip, and the wake-file picker — plus a routing assertion
that ``_summarize_via_qwen`` posts to the LiteLLM proxy with the
right model and Authorization header. The real LCD POST is a network
path and stays untested here per the worker spec (skip non-trivial
integration tests).
"""

from __future__ import annotations

import json
import pathlib

from alice_speaking.infra.face_caption import (
    DEFAULT_LITELLM_API_KEY,
    DEFAULT_LITELLM_BASE_URL,
    DEFAULT_LITELLM_MODEL,
    MAX_CAPTION_CHARS,
    _latest_wake_path,
    _load_face_caption_config,
    _strip_frontmatter,
    _summarize_via_qwen,
    _truncate_caption,
)


def test_truncate_clamps_long_input() -> None:
    long_input = "x" * 200
    out = _truncate_caption(long_input)
    assert len(out) <= MAX_CAPTION_CHARS
    assert out == "x" * MAX_CAPTION_CHARS


def test_truncate_strips_trailing_punctuation_and_whitespace() -> None:
    assert _truncate_caption("  reviewing cozyhem bugs.   ") == "reviewing cozyhem bugs"
    assert _truncate_caption("idle; queue blocked on Jason!") == (
        "idle; queue blocked on Jason"
    )


def test_truncate_collapses_internal_whitespace() -> None:
    assert _truncate_caption("foo   bar\n\n   baz") == "foo bar baz"


def test_strip_frontmatter_removes_yaml_block() -> None:
    text = "---\nmode: active\ndid_work: true\n---\n\nbody here\nmore body\n"
    assert _strip_frontmatter(text) == "body here\nmore body\n"


def test_strip_frontmatter_passes_through_when_absent() -> None:
    text = "no frontmatter at all\nsecond line\n"
    assert _strip_frontmatter(text) == text


def test_latest_wake_path_returns_most_recent_today(tmp_path: pathlib.Path) -> None:
    import datetime as dt

    now = dt.datetime(2026, 6, 4, 12, 0, 0)
    day_dir = tmp_path / now.date().isoformat()
    day_dir.mkdir()
    older = day_dir / "100000-wake.md"
    newer = day_dir / "110000-wake.md"
    older.write_text("old")
    newer.write_text("new")
    import os

    os.utime(older, (1, 1))
    os.utime(newer, (1000, 1000))
    assert _latest_wake_path(tmp_path, now=now) == newer


def test_latest_wake_path_falls_back_to_yesterday(tmp_path: pathlib.Path) -> None:
    import datetime as dt

    now = dt.datetime(2026, 6, 4, 12, 0, 0)
    # Today directory exists but empty.
    (tmp_path / now.date().isoformat()).mkdir()
    yesterday_dir = tmp_path / (now.date() - dt.timedelta(days=1)).isoformat()
    yesterday_dir.mkdir()
    f = yesterday_dir / "230000-wake.md"
    f.write_text("from yesterday")
    assert _latest_wake_path(tmp_path, now=now) == f


def test_latest_wake_path_returns_none_when_empty(tmp_path: pathlib.Path) -> None:
    import datetime as dt

    now = dt.datetime(2026, 6, 4, 12, 0, 0)
    assert _latest_wake_path(tmp_path, now=now) is None


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def test_load_face_caption_config_returns_defaults_when_file_absent(
    tmp_path: pathlib.Path,
) -> None:
    cfg = _load_face_caption_config(tmp_path / "missing.json")
    assert cfg["base_url"] == DEFAULT_LITELLM_BASE_URL
    assert cfg["model"] == DEFAULT_LITELLM_MODEL
    assert cfg["api_key"] == DEFAULT_LITELLM_API_KEY


def test_load_face_caption_config_uses_defaults_when_section_missing(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "alice.config.json"
    path.write_text(json.dumps({"speaking": {"model": "claude-opus-4-7"}}))
    cfg = _load_face_caption_config(path)
    assert cfg["base_url"] == DEFAULT_LITELLM_BASE_URL
    assert cfg["model"] == DEFAULT_LITELLM_MODEL
    assert cfg["api_key"] == DEFAULT_LITELLM_API_KEY


def test_load_face_caption_config_applies_overrides(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "alice.config.json"
    path.write_text(
        json.dumps(
            {
                "face_caption": {
                    "base_url": "http://example:9999/v1",
                    "model": "qwen-desktop",
                    "api_key": "sk-test",
                }
            }
        )
    )
    cfg = _load_face_caption_config(path)
    assert cfg["base_url"] == "http://example:9999/v1"
    assert cfg["model"] == "qwen-desktop"
    assert cfg["api_key"] == "sk-test"


def test_load_face_caption_config_partial_override_keeps_other_defaults(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "alice.config.json"
    path.write_text(json.dumps({"face_caption": {"model": "qwen-desktop"}}))
    cfg = _load_face_caption_config(path)
    assert cfg["base_url"] == DEFAULT_LITELLM_BASE_URL
    assert cfg["model"] == "qwen-desktop"
    assert cfg["api_key"] == DEFAULT_LITELLM_API_KEY


# ---------------------------------------------------------------------------
# LiteLLM routing
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Minimal :class:`httpx.Client` stand-in.

    Records the URL / json body / headers of every POST and returns a
    fixed canned response. Matches the surface ``_summarize_via_qwen``
    actually touches (``post``); ``close`` is a no-op because the
    function only closes clients it constructs itself.
    """

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, *, json, headers=None):  # noqa: A002 — mirror httpx
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        return self.response

    def close(self) -> None:
        return None


def test_summarize_via_qwen_posts_to_litellm_with_model_and_auth() -> None:
    response = _FakeResponse(
        200,
        {"choices": [{"message": {"content": "reviewing cozyhem"}}]},
    )
    client = _FakeClient(response)
    out = _summarize_via_qwen(
        "thinking about cozyhem timer drift", client=client
    )
    assert out == "reviewing cozyhem"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "http://alice-litellm:4000/v1/chat/completions"
    assert call["json"]["model"] == "qwen-local"
    assert call["json"]["messages"][0]["role"] == "system"
    assert call["json"]["messages"][1]["role"] == "user"
    assert call["json"]["messages"][1]["content"] == (
        "thinking about cozyhem timer drift"
    )
    assert call["headers"].get("authorization") == "Bearer sk-alice-local"
    # No Anthropic-shape headers should leak through.
    assert "anthropic-version" not in {k.lower() for k in call["headers"]}
    assert "anthropic-beta" not in {k.lower() for k in call["headers"]}


def test_summarize_via_qwen_honors_overrides() -> None:
    response = _FakeResponse(
        200, {"choices": [{"message": {"content": "idle; queue blocked"}}]}
    )
    client = _FakeClient(response)
    out = _summarize_via_qwen(
        "queue is empty",
        base_url="http://10.20.30.177:4000/v1",
        model="qwen-desktop",
        api_key="sk-override",
        client=client,
    )
    assert out == "idle; queue blocked"
    call = client.calls[0]
    assert call["url"] == "http://10.20.30.177:4000/v1/chat/completions"
    assert call["json"]["model"] == "qwen-desktop"
    assert call["headers"]["authorization"] == "Bearer sk-override"


def test_summarize_via_qwen_returns_none_on_http_error() -> None:
    response = _FakeResponse(503, {"error": "backend down"})
    client = _FakeClient(response)
    assert _summarize_via_qwen("anything", client=client) is None


def test_summarize_via_qwen_returns_none_on_empty_content() -> None:
    response = _FakeResponse(
        200, {"choices": [{"message": {"content": "   "}}]}
    )
    client = _FakeClient(response)
    assert _summarize_via_qwen("anything", client=client) is None


def test_summarize_via_qwen_truncates_long_output() -> None:
    response = _FakeResponse(
        200,
        {
            "choices": [
                {"message": {"content": "this is a very long summary that exceeds 32 chars"}}
            ]
        },
    )
    client = _FakeClient(response)
    out = _summarize_via_qwen("anything", client=client)
    assert out is not None
    assert len(out) <= MAX_CAPTION_CHARS
