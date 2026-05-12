"""Tests for the request_host_claude MCP tool.

Coverage targets (per task-0012 spec):
- inbox file shape: frontmatter keys + body, written via atomic rename
- wait=False returns immediately with request_id + inbox_path only
- wait=True with a pre-seeded outbox returns parsed stdout/stderr/status
- wait=True timing out returns status="timeout" with empty payloads

The CLI wrapper and slug helper are also covered as part of the same
suite — they share the same core (``request_host_claude_from_args``).
"""

from __future__ import annotations

import io
import json
import pathlib
import threading
import time

import pytest

from alice_speaking.tools import host_claude


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_outbox(
    root: pathlib.Path,
    request_id: str,
    *,
    status: str = "success",
    stdout: str = "ok\n",
    stderr: str = "",
    exit_code: int = 0,
) -> pathlib.Path:
    """Write a synthetic outbox file matching the daemon's shape."""
    outbox_dir = root / "outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    path = outbox_dir / f"{request_id}.md"
    path.write_text(
        "---\n"
        f"id: {request_id}\n"
        f"status: {status}\n"
        "started_at: 2026-05-12T00:00:00Z\n"
        "finished_at: 2026-05-12T00:00:05Z\n"
        f"exit_code: {exit_code}\n"
        "---\n"
        "# Stdout\n"
        "\n"
        f"{stdout}\n"
        "# Stderr\n"
        "\n"
        f"{stderr}\n"
    )
    return path


# ---------------------------------------------------------------------------
# Inbox shape
# ---------------------------------------------------------------------------


def test_inbox_file_has_frontmatter_and_body(tmp_path: pathlib.Path) -> None:
    result = host_claude.request_host_claude_from_args(
        {
            "prompt": "summarize the build log",
            "urgency": "normal",
            "timeout_seconds": 120,
            "allow_destructive": False,
            "wait": False,
        },
        root=tmp_path,
    )
    inbox_path = pathlib.Path(result["inbox_path"])
    assert inbox_path.is_file()
    text = inbox_path.read_text()

    # Frontmatter block: opens with '---', closes with '---', contains
    # every documented key.
    assert text.startswith("---\n")
    fm_end = text.index("\n---\n", 4)
    fm = text[4:fm_end]
    assert f"id: {result['request_id']}" in fm
    assert "requested_by: speaking" in fm
    assert "created_at: " in fm
    assert "urgency: normal" in fm
    assert "timeout_seconds: 120" in fm
    assert "allow_destructive: false" in fm

    # Body has the # Task header and the literal prompt text.
    body = text[fm_end + 5 :]
    assert body.lstrip().startswith("# Task")
    assert "summarize the build log" in body


def test_inbox_written_atomically_no_temp_leftover(
    tmp_path: pathlib.Path,
) -> None:
    """tempfile + rename: no .tmp staging file should be visible after."""
    host_claude.request_host_claude_from_args(
        {"prompt": "hello world", "wait": False}, root=tmp_path
    )
    leftovers = list((tmp_path / "inbox").glob(".*.tmp"))
    assert leftovers == [], f"temp files leaked: {leftovers}"


def test_allow_destructive_serializes_as_lowercase_bool(
    tmp_path: pathlib.Path,
) -> None:
    result = host_claude.request_host_claude_from_args(
        {"prompt": "rm -rf foo", "allow_destructive": True, "wait": False},
        root=tmp_path,
    )
    text = pathlib.Path(result["inbox_path"]).read_text()
    assert "allow_destructive: true" in text


def test_request_id_includes_slug_from_first_words(
    tmp_path: pathlib.Path,
) -> None:
    result = host_claude.request_host_claude_from_args(
        {"prompt": "Run the integration test suite please", "wait": False},
        root=tmp_path,
    )
    rid = result["request_id"]
    # slug is the first six words lowercased + hyphenated. Don't assert
    # exact bytes because the leading UTC timestamp varies; just check
    # the slug pieces show up.
    assert "run-the-integration-test-suite-please" in rid


# ---------------------------------------------------------------------------
# wait=False
# ---------------------------------------------------------------------------


def test_wait_false_returns_immediately(tmp_path: pathlib.Path) -> None:
    start = time.monotonic()
    result = host_claude.request_host_claude_from_args(
        {"prompt": "what time is it", "wait": False}, root=tmp_path
    )
    elapsed = time.monotonic() - start
    # Should never take more than a fraction of a second — no polling.
    assert elapsed < 1.0
    assert result["request_id"]
    assert result["inbox_path"]
    # No outbox-derived keys leak when we didn't wait.
    assert "status" not in result
    assert "stdout" not in result


# ---------------------------------------------------------------------------
# wait=True (synchronous, outbox pre-seeded)
# ---------------------------------------------------------------------------


def test_wait_true_returns_parsed_outbox(tmp_path: pathlib.Path) -> None:
    """Seed the outbox before the call so the first poll lands a hit."""
    # We need to know the request_id ahead of time. The easiest path is
    # to monkey-call request_host_claude_from_args twice: once with
    # wait=False to mint the id, then drop a matching outbox file, then
    # poll separately. But the cleaner test exercises the timing path: a
    # background thread writes the outbox a beat after the call starts.
    captured: dict[str, str] = {}

    def writer() -> None:
        # Spin until the inbox shows up; then mirror that id into outbox.
        for _ in range(50):
            files = list((tmp_path / "inbox").glob("*.md"))
            if files:
                request_id = files[0].stem
                captured["id"] = request_id
                _make_outbox(
                    tmp_path,
                    request_id,
                    stdout="hello from claude\n",
                    stderr="warning: nothing\n",
                    exit_code=0,
                )
                return
            time.sleep(0.01)

    t = threading.Thread(target=writer)
    t.start()
    try:
        result = host_claude.request_host_claude_from_args(
            {
                "prompt": "say hello",
                "timeout_seconds": 5,
                "wait": True,
            },
            root=tmp_path,
            poll_interval=0.05,
        )
    finally:
        t.join(timeout=2)

    assert captured.get("id") == result["request_id"]
    assert result["status"] == "success"
    assert result["exit_code"] == 0
    assert "hello from claude" in result["stdout"]
    assert "warning: nothing" in result["stderr"]
    assert result["started_at"] == "2026-05-12T00:00:00Z"
    assert result["finished_at"] == "2026-05-12T00:00:05Z"
    assert result["outbox_path"].endswith(f"{result['request_id']}.md")


# ---------------------------------------------------------------------------
# Timeout-while-waiting
# ---------------------------------------------------------------------------


def test_wait_true_times_out_when_no_outbox_appears(
    tmp_path: pathlib.Path,
) -> None:
    # Inject a fake monotonic clock that races past the deadline after
    # two polls. That keeps the test fast without making the function
    # signature production-uglier.
    fake_now = [0.0]

    def now() -> float:
        # Advance the clock 100s per call — easily blows past the
        # timeout_seconds (1) + WAIT_SLACK_SECONDS (60) budget.
        fake_now[0] += 100.0
        return fake_now[0]

    result = host_claude.request_host_claude_from_args(
        {
            "prompt": "stalled task",
            "timeout_seconds": 1,
            "wait": True,
        },
        root=tmp_path,
        poll_interval=0.0,
        now=now,
    )
    assert result["status"] == "timeout"
    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert result["exit_code"] is None
    # Inbox file should still exist — the daemon may still be working.
    assert pathlib.Path(result["inbox_path"]).is_file()


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_empty_prompt_raises_value_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="prompt"):
        host_claude.request_host_claude_from_args(
            {"prompt": "   ", "wait": False}, root=tmp_path
        )


def test_invalid_urgency_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="urgency"):
        host_claude.request_host_claude_from_args(
            {"prompt": "ok", "urgency": "extreme", "wait": False}, root=tmp_path
        )


def test_invalid_timeout_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        host_claude.request_host_claude_from_args(
            {"prompt": "ok", "timeout_seconds": "soon", "wait": False},
            root=tmp_path,
        )
    with pytest.raises(ValueError, match="timeout_seconds"):
        host_claude.request_host_claude_from_args(
            {"prompt": "ok", "timeout_seconds": 0, "wait": False},
            root=tmp_path,
        )


# ---------------------------------------------------------------------------
# MCP tool wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_tool_returns_json_content_block(
    cfg, tmp_path: pathlib.Path
) -> None:
    tools = host_claude.build(cfg, root=tmp_path)
    assert len(tools) == 1
    request_tool = tools[0]
    assert request_tool.name == "request_host_claude"

    out = await request_tool.handler(
        {"prompt": "ping", "wait": False}
    )
    payload = json.loads(out["content"][0]["text"])
    assert payload["request_id"]
    assert pathlib.Path(payload["inbox_path"]).is_file()


@pytest.mark.asyncio
async def test_mcp_tool_surfaces_validation_error(
    cfg, tmp_path: pathlib.Path
) -> None:
    tools = host_claude.build(cfg, root=tmp_path)
    out = await tools[0].handler({"prompt": "", "wait": False})
    assert out.get("isError") is True
    assert "prompt" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------


def test_cli_writes_inbox_and_prints_json(
    tmp_path: pathlib.Path, capsys
) -> None:
    rc = host_claude.main(
        ["a quick ad-hoc task", "--root", str(tmp_path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert pathlib.Path(payload["inbox_path"]).is_file()


def test_cli_reads_prompt_from_stdin(
    tmp_path: pathlib.Path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("piped prompt body"))
    rc = host_claude.main(["-", "--root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    inbox = pathlib.Path(payload["inbox_path"]).read_text()
    assert "piped prompt body" in inbox
