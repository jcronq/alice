"""Tests for the outbound A2A client + the token-aware server bridge.

Covers:

- :meth:`A2ATransport.send` routes URL-shaped destinations through
  :class:`A2AClient` instead of the server per-task outbox.
- Unknown destination addresses on the server path still log + drop
  (regression guard — the URL branch must not steal task ids).
- :meth:`A2ATransport.register_peer` and :meth:`_principal_for_token`
  resolve inbound tokens to the matching principal id, falling back
  to the catch-all when the token is missing / unknown.
- :mod:`alice_speaking.infra.a2a_peers` parses the YAML shape and
  resolves env-sourced tokens with sensible defaults when either
  side is missing.

The actual client-to-server round-trip over the wire is covered by
``test_a2a_transport.py::test_live_request_response_smoke`` plus
``scripts/a2a_mutual_ping.py`` — this module stays deterministic.
"""

from __future__ import annotations

import asyncio

import pytest

from alice_speaking.infra import a2a_peers as peers_module
from alice_speaking.transports.a2a import A2ATransport
from alice_speaking.transports.a2a_client import (
    A2AClient,
    A2AReply,
    PEER_TOKEN_HEADER,
    _TERMINAL_STATES,
    resolve_client_timeout,
    resolve_identity_token,
)
from alice_speaking.transports.base import ChannelRef, OutboundMessage


def test_url_address_dispatches_to_client(monkeypatch):
    """A URL destination must hit the outbound client, not the server outbox."""
    t = A2ATransport(port=0)
    calls: list[dict] = []

    async def fake_send_text(*, url, text, timeout_s=None):
        calls.append({"url": url, "text": text, "timeout_s": timeout_s})
        return A2AReply(text="pong", state=None, chunk_count=1)

    monkeypatch.setattr(t._client, "send_text", fake_send_text)

    async def go():
        return await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="a2a",
                    address="http://10.200.0.2:7878",
                    durable=True,
                ),
                text="ping",
            )
        )

    delivered = asyncio.run(go())
    assert delivered == 1
    assert calls == [
        {"url": "http://10.200.0.2:7878", "text": "ping", "timeout_s": None}
    ]


def test_url_error_reports_zero_chunks(monkeypatch):
    t = A2ATransport(port=0)

    async def fake_send_text(*, url, text, timeout_s=None):
        return A2AReply(error="peer refused connection")

    monkeypatch.setattr(t._client, "send_text", fake_send_text)

    async def go():
        return await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="a2a",
                    address="http://unreachable.example",
                    durable=True,
                ),
                text="ping",
            )
        )

    delivered = asyncio.run(go())
    assert delivered == 0


def test_task_id_destination_still_routes_to_outbox(monkeypatch):
    """Regression — non-URL destinations must keep hitting the per-task outbox."""
    t = A2ATransport(port=0)
    outbox = t._open_task_outbox("task-abc")

    client_called = False

    async def fake_send_text(*, url, text, timeout_s=None):
        nonlocal client_called
        client_called = True
        return A2AReply()

    monkeypatch.setattr(t._client, "send_text", fake_send_text)

    async def go():
        return await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="a2a", address="task-abc", durable=False
                ),
                text="hello world",
            )
        )

    delivered = asyncio.run(go())
    assert delivered == 1
    assert not client_called
    ev = outbox.get_nowait()
    assert ev["kind"] == "chunk"


def test_register_peer_updates_token_map():
    t = A2ATransport(port=0)
    t.register_peer(
        principal_id="grand-alice",
        token="secret-GA",
        display_name="Grand Alice",
    )
    assert t._peer_tokens == {"secret-GA": "grand-alice"}
    assert t._peer_display_names == {"grand-alice": "Grand Alice"}


def test_register_peer_rejects_empty_inputs():
    t = A2ATransport(port=0)
    with pytest.raises(ValueError):
        t.register_peer(principal_id="x", token="")
    with pytest.raises(ValueError):
        t.register_peer(principal_id="", token="x")


def test_principal_for_token_resolves_registered_peer():
    t = A2ATransport(port=0, peer_tokens={"tok-1": "grand-alice"})
    pid, display = t._principal_for_token("tok-1")
    assert pid == "grand-alice"
    assert "grand-alice" in display


def test_principal_for_token_uses_display_override():
    t = A2ATransport(
        port=0,
        peer_tokens={"tok-1": "grand-alice"},
        peer_display_names={"grand-alice": "Grand Alice (grandparents)"},
    )
    pid, display = t._principal_for_token("tok-1")
    assert pid == "grand-alice"
    assert display == "Grand Alice (grandparents)"


def test_principal_for_token_falls_back_without_token():
    t = A2ATransport(
        port=0,
        principal_name="a2a",
        principal_display_name="A2A peer agent",
        peer_tokens={"tok-1": "grand-alice"},
    )
    pid, display = t._principal_for_token(None)
    assert pid == "a2a"
    assert display == "A2A peer agent"
    # Unknown token also falls back.
    pid, display = t._principal_for_token("different")
    assert pid == "a2a"


def test_make_principal_tags_peer_by_token():
    t = A2ATransport(
        port=0,
        peer_tokens={"tok-1": "grand-alice"},
        peer_display_names={"grand-alice": "Grand Alice"},
    )
    p = t._make_principal("tok-1")
    assert p.native_id == "grand-alice"
    assert p.display_name == "Grand Alice"
    assert p.transport == "a2a"


# -- peers config loader ------------------------------------------------------


def test_peers_load_missing_yaml_returns_empty_but_honours_identity_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_A2A_IDENTITY_TOKEN", "id-tok")
    cfg = peers_module.load(tmp_path / "does-not-exist.yaml")
    assert cfg.peers == []
    assert cfg.identity_token == "id-tok"
    assert cfg.token_map() == {}
    assert cfg.display_name_map() == {}


def test_peers_load_yaml_resolves_env_tokens(tmp_path):
    yaml_path = tmp_path / "a2a-peers.yaml"
    yaml_path.write_text(
        "identity_token_env: ALICE_A2A_IDENTITY_TOKEN\n"
        "peers:\n"
        "  grand-alice:\n"
        '    display_name: "Grand Alice"\n'
        "    token_env: ALICE_A2A_GRAND\n"
    )
    cfg = peers_module.load(
        yaml_path,
        env={
            "ALICE_A2A_IDENTITY_TOKEN": "ident",
            "ALICE_A2A_GRAND": "grand-secret",
        },
    )
    assert cfg.identity_token == "ident"
    assert cfg.token_map() == {"grand-secret": "grand-alice"}
    assert cfg.display_name_map() == {"grand-alice": "Grand Alice"}


def test_peers_load_requires_token_env_on_each_peer(tmp_path):
    yaml_path = tmp_path / "a2a-peers.yaml"
    yaml_path.write_text(
        "peers:\n"
        "  grand-alice:\n"
        '    display_name: "Grand Alice"\n'
    )
    with pytest.raises(ValueError):
        peers_module.load(yaml_path, env={})


def test_peers_load_drops_unresolved_tokens(tmp_path):
    yaml_path = tmp_path / "a2a-peers.yaml"
    yaml_path.write_text(
        "peers:\n"
        "  grand-alice:\n"
        "    token_env: MISSING_ENV\n"
    )
    cfg = peers_module.load(yaml_path, env={})
    # Entry still parsed (keeps display + env trail for operator
    # visibility) but it shouldn't surface in the live token map.
    assert cfg.peers[0].principal_id == "grand-alice"
    assert cfg.peers[0].token is None
    assert cfg.token_map() == {}


def test_peer_token_header_constant_matches_expected_wire_name():
    # Smoke — the header name is contractually fixed. Changing it
    # breaks the agreement between the two Alices without a
    # coordinated deploy.
    assert PEER_TOKEN_HEADER == "x-alice-peer-token"


def test_resolve_identity_token_reads_env(monkeypatch):
    monkeypatch.delenv("ALICE_A2A_IDENTITY_TOKEN", raising=False)
    assert resolve_identity_token() is None
    monkeypatch.setenv("ALICE_A2A_IDENTITY_TOKEN", "  secret  ")
    assert resolve_identity_token() == "secret"


def test_resolve_client_timeout_env_override(monkeypatch):
    monkeypatch.delenv("ALICE_A2A_CLIENT_TIMEOUT", raising=False)
    assert resolve_client_timeout() == pytest.approx(60.0)
    monkeypatch.setenv("ALICE_A2A_CLIENT_TIMEOUT", "12.5")
    assert resolve_client_timeout() == pytest.approx(12.5)
    monkeypatch.setenv("ALICE_A2A_CLIENT_TIMEOUT", "garbage")
    assert resolve_client_timeout() == pytest.approx(60.0)
    monkeypatch.setenv("ALICE_A2A_CLIENT_TIMEOUT", "-1")
    assert resolve_client_timeout() == pytest.approx(60.0)


# -- regression guards for code-review findings (PR #920) ---------------------


def test_terminal_states_cover_auth_and_input_required():
    """Server closes the SSE stream on AUTH_REQUIRED / INPUT_REQUIRED too,
    so the client must recognise them as terminal — otherwise the stream
    loop hangs waiting for an event that never arrives."""
    from a2a.types import TaskState

    assert TaskState.TASK_STATE_AUTH_REQUIRED in _TERMINAL_STATES
    assert TaskState.TASK_STATE_INPUT_REQUIRED in _TERMINAL_STATES


def test_url_empty_reply_reports_zero_chunks(monkeypatch):
    """F3 — a successful-but-empty peer reply must return a real 0,
    not a fabricated ghost chunk. The OutboxRouter interprets the
    chunk count verbatim."""
    t = A2ATransport(port=0)

    async def fake_send_text(*, url, text, timeout_s=None):
        # Peer answered, no artifacts emitted, no error, no timeout.
        return A2AReply(state=None, chunk_count=0)

    monkeypatch.setattr(t._client, "send_text", fake_send_text)

    async def go():
        return await t.send(
            OutboundMessage(
                destination=ChannelRef(
                    transport="a2a",
                    address="http://peer.example",
                    durable=True,
                ),
                text="ping",
            )
        )

    delivered = asyncio.run(go())
    assert delivered == 0


def test_timeout_invalidates_cached_client(monkeypatch):
    """F4 — an asyncio.TimeoutError leaves the a2a-sdk client's httpx
    stream half-cancelled. The next send_text must rebuild rather
    than reuse the stale cached client."""

    class FakeClient:
        def __init__(self, url):
            self.url = url

        async def send_message(self, request):  # pragma: no cover - never yields
            # Hang-forever iterator; the outer asyncio.timeout fires.
            await asyncio.sleep(10)
            if False:
                yield None

    client = A2AClient(default_timeout_s=0.05)

    async def fake_build(url):
        return FakeClient(url)

    monkeypatch.setattr(client, "_build_client", fake_build)

    async def go():
        reply = await client.send_text(url="http://peer.test", text="hi")
        assert reply.timed_out is True
        # Cache must be empty — the stale client was evicted.
        assert client._client_cache == {}

    asyncio.run(go())


def _build_task_frame(text: str, state):
    """Helper: build a StreamResponse carrying a completed-Task payload
    with a single text artifact. Models the non-streaming branch of
    :meth:`BaseClient.send_message` where the SDK synthesises one
    ``StreamResponse.task`` frame from the plain-RPC Task reply."""
    from a2a.helpers import new_text_artifact
    from a2a.types import StreamResponse, Task, TaskStatus

    response = StreamResponse()
    task = Task(
        id="t1",
        context_id="t1",
        status=TaskStatus(state=state),
        artifacts=[new_text_artifact(name="reply", text=text, artifact_id="a1")],
    )
    response.task.CopyFrom(task)
    return response


def test_task_frame_handler_aggregates_artifacts():
    """F1 fallback — if a peer somehow drives the non-streaming branch,
    ``_ingest_stream_response`` must pull artifact text off the
    ``task`` frame rather than silently dropping it."""
    from a2a.types import TaskState

    client = A2AClient()
    reply = A2AReply()
    response = _build_task_frame("pong echo", TaskState.TASK_STATE_COMPLETED)

    client._ingest_stream_response(response, reply)

    assert reply.text == "pong echo"
    assert reply.chunk_count == 1
    assert reply.state == TaskState.TASK_STATE_COMPLETED


def test_task_frame_handler_does_not_break_on_streaming_initial_task():
    """F1 regression — the streaming path's FIRST SSE frame is always
    the initial Task (state=SUBMITTED, artifacts empty). Setting a
    terminal state on that frame would break the client's outer
    stream loop before the real artifact and status-COMPLETED frames
    arrive. The handler must leave ``reply.state`` unset in that case."""
    from a2a.types import StreamResponse, Task, TaskState, TaskStatus

    client = A2AClient()
    reply = A2AReply()
    task = Task(
        id="t",
        context_id="t",
        status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
    )
    response = StreamResponse()
    response.task.CopyFrom(task)

    client._ingest_stream_response(response, reply)

    assert reply.state is None
    assert reply.text == ""
    assert reply.chunk_count == 0


def test_aclose_is_idempotent_and_closes_shared_httpx_once():
    """F5 — a second aclose() must not double-close the shared httpx
    client the cached SDK clients share, and the broad-except debug
    log path must not fire."""
    client = A2AClient()
    # Install a fake httpx + two fake cached clients that would try
    # to close the same underlying resource if invoked.
    close_count = {"httpx": 0}

    class FakeHttpx:
        async def aclose(self):
            close_count["httpx"] += 1

    client._httpx = FakeHttpx()
    client._client_cache = {"a": object(), "b": object()}

    async def go():
        await client.aclose()
        await client.aclose()  # second call must be a no-op
        assert client._client_cache == {}
        assert client._httpx is None

    asyncio.run(go())
    assert close_count["httpx"] == 1
