"""Tests for inbound attachment parsing + prompt composition."""

from __future__ import annotations

import json

import pytest

from alice_speaking.infra.signal_rpc import (
    Attachment,
    SignalEnvelope,
    _parse_attachments,
    _parse_envelope,
)


# ---------------------------------------------------------------------------
# Envelope parsing


def _make_log_line(*, body: str | None, attachments: list[dict] | None) -> str:
    """Build a signal-cli log line with the shape we observe in production."""
    payload = {
        "envelope": {
            "source": "+15555550100",
            "timestamp": 1234567890,
            "dataMessage": {
                "message": body,
                "attachments": attachments or [],
            },
        },
    }
    return json.dumps(payload)


def test_parse_envelope_with_text_only() -> None:
    line = _make_log_line(body="hello", attachments=None)
    env = _parse_envelope(line)
    assert env is not None
    assert env.body == "hello"
    assert env.attachments == []


def test_parse_envelope_with_attachment_only() -> None:
    line = _make_log_line(
        body=None,
        attachments=[
            {
                "id": "abc123.jpg",
                "contentType": "image/jpeg",
                "filename": "photo.jpg",
                "size": 12345,
            }
        ],
    )
    env = _parse_envelope(line)
    assert env is not None
    assert env.body == ""
    assert len(env.attachments) == 1
    a = env.attachments[0]
    assert a.id == "abc123.jpg"
    assert a.content_type == "image/jpeg"
    assert a.filename == "photo.jpg"
    assert a.size == 12345
    # Path resolves under the configured attachments dir.
    assert str(a.path).endswith("/abc123.jpg")


def test_parse_envelope_drops_empty_message() -> None:
    """No body and no attachments → not a useful envelope, ignore."""
    line = _make_log_line(body=None, attachments=None)
    assert _parse_envelope(line) is None


def test_parse_envelope_supports_multiple_attachments() -> None:
    line = _make_log_line(
        body="check these out",
        attachments=[
            {"id": "a.jpg", "contentType": "image/jpeg", "filename": "one.jpg"},
            {"id": "b.png", "contentType": "image/png"},
            {"id": "c.pdf", "contentType": "application/pdf", "filename": "doc.pdf"},
        ],
    )
    env = _parse_envelope(line)
    assert env is not None
    assert env.body == "check these out"
    assert len(env.attachments) == 3
    assert [a.id for a in env.attachments] == ["a.jpg", "b.png", "c.pdf"]
    # Default content_type when missing is application/octet-stream;
    # in this case all entries provided one.
    assert env.attachments[1].filename is None  # not provided → None


def test_parse_envelope_handles_malformed_attachments() -> None:
    """An entry without an ``id`` is skipped silently."""
    raw = [{"contentType": "image/jpeg"}, {"id": "ok.jpg"}]
    parsed = _parse_attachments(raw)
    assert len(parsed) == 1
    assert parsed[0].id == "ok.jpg"


def test_parse_envelope_ignores_non_json_lines() -> None:
    assert _parse_envelope("not json") is None
    assert _parse_envelope("") is None
    assert _parse_envelope("# log header\n") is None


# ---------------------------------------------------------------------------
# Prompt composition (daemon.signal_transport.build_prompt)


@pytest.fixture
def daemon(cfg, monkeypatch):
    """Construct a SpeakingDaemon with SignalClient stubbed out."""
    from alice_speaking import daemon as daemon_module

    class _StubSignal:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr(daemon_module, "SignalClient", _StubSignal)
    return daemon_module.SpeakingDaemon(cfg)


def _signal_event(env: SignalEnvelope, name: str = "Owner"):
    """Wrap an envelope in a SignalEvent for the prompt builder."""
    from alice_speaking.daemon import SignalEvent

    return SignalEvent(envelope=env, sender_name=name)


def test_prompt_with_no_attachments(daemon) -> None:
    env = SignalEnvelope(timestamp=1, source="+15555550100", body="hi alice")
    prompt = daemon.signal_transport.build_prompt(
        sender_name="Owner",
        stamp="Friday, April 25, 2026 at 9:00 AM EDT",
        batch=[_signal_event(env)],
    )
    assert "[Signal from Owner | Friday" in prompt
    assert "hi alice" in prompt
    assert "attachment" not in prompt.lower()


def test_prompt_with_attachments_lists_paths(daemon, tmp_path) -> None:
    p1 = tmp_path / "x.jpg"
    p2 = tmp_path / "y.pdf"
    env = SignalEnvelope(
        timestamp=1,
        source="+15555550100",
        body="see attached",
        attachments=[
            Attachment(id="x.jpg", path=p1, content_type="image/jpeg", filename="snap.jpg"),
            Attachment(id="y.pdf", path=p2, content_type="application/pdf"),
        ],
    )
    prompt = daemon.signal_transport.build_prompt(
        sender_name="Owner", stamp="t", batch=[_signal_event(env)]
    )
    assert "see attached" in prompt
    assert "--- 2 attachments ---" in prompt
    assert str(p1) in prompt
    assert "snap.jpg" in prompt
    assert str(p2) in prompt
    assert "image/jpeg" in prompt
    assert "application/pdf" in prompt
    assert "use the Read tool" in prompt


def test_prompt_with_image_only_message(daemon, tmp_path) -> None:
    """Image-only inbound — body is empty; placeholder substitutes."""
    p = tmp_path / "selfie.jpg"
    env = SignalEnvelope(
        timestamp=1,
        source="+15555550100",
        body="",
        attachments=[Attachment(id="selfie.jpg", path=p, content_type="image/jpeg")],
    )
    prompt = daemon.signal_transport.build_prompt(
        sender_name="Owner", stamp="t", batch=[_signal_event(env)]
    )
    assert "(no text" in prompt
    assert str(p) in prompt
    assert "--- 1 attachment ---" in prompt


def test_prompt_with_batched_messages(daemon) -> None:
    envs = [
        SignalEnvelope(
            timestamp=1735131600000, source="+15555550100", body="hi alice"
        ),
        SignalEnvelope(
            timestamp=1735131605000, source="+15555550100", body="quick question"
        ),
        SignalEnvelope(
            timestamp=1735131610000, source="+15555550100", body="what's the time"
        ),
    ]
    prompt = daemon.signal_transport.build_prompt(
        sender_name="Owner",
        stamp="t",
        batch=[_signal_event(e) for e in envs],
    )
    assert "3 messages came in while you were busy" in prompt
    assert "--- message 1 of 3" in prompt
    assert "--- message 2 of 3" in prompt
    assert "--- message 3 of 3" in prompt
    assert "hi alice" in prompt
    assert "quick question" in prompt
    assert "what's the time" in prompt
    # Closing instruction still present
    assert "send_message" in prompt
