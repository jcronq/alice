"""PII redaction unit tests."""

from __future__ import annotations

from alice_eval.pii import redact


def test_redact_e164_phone_number():
    text = "call me at +14357091512 when you can"
    out = redact(text)
    assert "+14357091512" not in out
    assert "[REDACTED_PHONE]" in out


def test_redact_dashed_phone_number():
    text = "ring 435-709-1512 tonight"
    out = redact(text)
    assert "435-709-1512" not in out
    assert "[REDACTED_PHONE]" in out


def test_redact_email():
    out = redact("write to alice@example.com please")
    assert "alice@example.com" not in out
    assert "[REDACTED_EMAIL]" in out


def test_redact_home_path_preserves_shape():
    out = redact("artifact at /home/alice/alice-mind/inner/notes/foo.md")
    assert "/home/alice/" not in out
    assert "/home/[REDACTED_USER]/alice-mind/inner/notes/foo.md" in out


def test_redact_keeps_names_and_unaffected_prose():
    text = "Jason asked Katie about the rower."
    assert redact(text) == text


def test_redact_handles_empty_input():
    assert redact("") == ""


def test_redact_is_idempotent_for_placeholder_text():
    text = "ping [REDACTED_PHONE] and [REDACTED_EMAIL]"
    assert redact(text) == text
