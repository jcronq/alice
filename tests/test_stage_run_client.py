"""Unit tests for ``alice_speaking.stage_run_client``.

The client is the daemon-side write edge to the status-site
``POST /api/stage-run`` handler added in PR #890. Covers the
inference helpers (``infer_stage`` / ``infer_item_slug``), the
positive POST path, the vocabulary validation, and the soft-fail
contract — a dispatch site must NEVER crash because this edge
hiccupped.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from alice_speaking import stage_run_client


# ---------------------------------------------------------------------------
# infer_stage


@pytest.mark.parametrize(
    "description,instructions,expected",
    [
        ("review slice X", "Run a code review on the diff.", "review"),
        ("lensed review of PR", "", "review"),
        ("code-reviewer: adversarial pass", "", "review"),
        ("design doc for the new slice", "", "design"),
        ("designer agent", "please architect the new runtime.", "design"),
        ("implement feature", "normal build prompt", "build"),
        ("", "", "build"),
        # Explicit code-review phrasing always wins — a code-review pass
        # of a design doc is still a review.
        ("code review the design doc", "", "review"),
    ],
)
def test_infer_stage_rules(description, instructions, expected) -> None:
    assert stage_run_client.infer_stage(description, instructions) == expected


# ---------------------------------------------------------------------------
# infer_item_slug


def test_infer_item_slug_prefers_explicit_task_id() -> None:
    slug = stage_run_client.infer_item_slug(
        description="some work",
        instructions="task-9999 appears here",
        task_id="task-0042",
    )
    assert slug == "task-0042"


def test_infer_item_slug_falls_back_to_prompt_mention() -> None:
    slug = stage_run_client.infer_item_slug(
        description="wire task-1234 up",
        instructions="",
    )
    assert slug == "task-1234"


def test_infer_item_slug_returns_none_when_no_context() -> None:
    assert (
        stage_run_client.infer_item_slug(
            description="just a build task", instructions="no task id"
        )
        is None
    )


# ---------------------------------------------------------------------------
# Local HTTP server fixture — exercises urllib end-to-end without a
# real status site.


class _Recorder(BaseHTTPRequestHandler):
    """Tiny request recorder for the POST path."""

    received: list[dict] = []
    response_code: int = 200

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        n = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            body = {"_raw": raw.decode("utf-8", "replace")}
        type(self).received.append(body)
        self.send_response(type(self).response_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *_a, **_k) -> None:  # silence test noise
        return


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def recorder():
    _Recorder.received = []
    _Recorder.response_code = 200
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _Recorder)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}/api/stage-run",
            "received": _Recorder.received,
            "set_code": lambda c: setattr(_Recorder, "response_code", c),
        }
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# post_stage_run — happy path


def test_post_stage_run_posts_expected_shape(recorder) -> None:
    ok = stage_run_client.post_stage_run(
        item_slug="slice-timeline",
        stage="build",
        status="running",
        task_id="bg-abcdef",
        started_at="2026-11-17T09:00:00-05:00",
        summary="worker spinning up",
        url=recorder["url"],
        key="k-test",
    )
    assert ok is True
    assert len(recorder["received"]) == 1
    body = recorder["received"][0]
    assert body["key"] == "k-test"
    assert body["item_slug"] == "slice-timeline"
    assert body["stage"] == "build"
    assert body["status"] == "running"
    assert body["task_id"] == "bg-abcdef"
    assert body["started_at"] == "2026-11-17T09:00:00-05:00"
    assert body["summary"] == "worker spinning up"
    # ended_at absent while running
    assert "ended_at" not in body


def test_post_stage_run_sends_ended_at_on_terminal(recorder) -> None:
    ok = stage_run_client.post_stage_run(
        item_slug="task-0042",
        stage="review",
        status="success",
        task_id="bg-close",
        started_at="2026-11-17T09:00:00-05:00",
        ended_at="2026-11-17T09:12:00-05:00",
        summary="review passed",
        url=recorder["url"],
        key="k-test",
    )
    assert ok is True
    body = recorder["received"][0]
    assert body["status"] == "success"
    assert body["ended_at"] == "2026-11-17T09:12:00-05:00"


# ---------------------------------------------------------------------------
# post_stage_run — soft-fail cases


def test_post_stage_run_rejects_bad_stage(recorder) -> None:
    ok = stage_run_client.post_stage_run(
        item_slug="slice-x",
        stage="wat",
        status="running",
        url=recorder["url"],
        key="k-test",
    )
    assert ok is False
    assert recorder["received"] == []


def test_post_stage_run_rejects_bad_status(recorder) -> None:
    ok = stage_run_client.post_stage_run(
        item_slug="slice-x",
        stage="build",
        status="pending",
        url=recorder["url"],
        key="k-test",
    )
    assert ok is False
    assert recorder["received"] == []


def test_post_stage_run_skips_without_slug(recorder) -> None:
    ok = stage_run_client.post_stage_run(
        item_slug="",
        stage="build",
        status="running",
        url=recorder["url"],
        key="k-test",
    )
    assert ok is False
    assert recorder["received"] == []


def test_post_stage_run_skips_without_key(recorder, monkeypatch) -> None:
    # Make the fallback key file unreadable so the client falls through
    # to "no key, skip POST" instead of using the real key.
    monkeypatch.setenv("ALICE_STATUS_KEY_FILE", "/nonexistent/key-missing")
    ok = stage_run_client.post_stage_run(
        item_slug="slice-x",
        stage="build",
        status="running",
        url=recorder["url"],
        # Deliberately no key argument — exercises the file-lookup
        # branch.
    )
    assert ok is False
    assert recorder["received"] == []


def test_post_stage_run_soft_fails_on_non_200(recorder) -> None:
    recorder["set_code"](500)
    ok = stage_run_client.post_stage_run(
        item_slug="slice-x",
        stage="build",
        status="running",
        url=recorder["url"],
        key="k-test",
    )
    # Dispatch site saw the request but the server rejected it; the
    # client signals failure but does NOT raise.
    assert ok is False


def test_post_stage_run_soft_fails_on_network_error() -> None:
    # Port 1 is reserved — guaranteed connection refused. The client
    # MUST return False without raising, regardless of local DNS.
    ok = stage_run_client.post_stage_run(
        item_slug="slice-x",
        stage="build",
        status="running",
        url="http://127.0.0.1:1/api/stage-run",
        key="k-test",
        timeout=0.25,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# Key loading from env


def test_post_stage_run_loads_key_from_env_file(
    tmp_path, recorder, monkeypatch
) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("from-env-file\n")
    monkeypatch.setenv("ALICE_STATUS_KEY_FILE", str(key_file))
    ok = stage_run_client.post_stage_run(
        item_slug="slice-x",
        stage="build",
        status="running",
        url=recorder["url"],
    )
    assert ok is True
    assert recorder["received"][0]["key"] == "from-env-file"


def test_default_url_uses_env_override(monkeypatch) -> None:
    monkeypatch.setenv(
        "ALICE_STAGE_RUN_URL", "http://example-host:9999/api/stage-run"
    )
    # Internal helper; exercised indirectly to confirm the env override
    # lands without re-importing the module.
    assert (
        stage_run_client._default_url()
        == "http://example-host:9999/api/stage-run"
    )
