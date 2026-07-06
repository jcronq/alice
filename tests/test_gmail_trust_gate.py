"""Tests for the Gmail limited-trust PreToolUse gate (Recommendation 2).

The gate methods only read ``self._current_turn_kind``,
``self._current_principal_display_name`` and ``self.address_book``, so we
exercise them against a lightweight stub ``self`` rather than standing up a
whole :class:`SpeakingDaemon`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alice_speaking.daemon import SpeakingDaemon
from alice_speaking.domain.principals import (
    AddressBook,
    PrincipalChannel,
    PrincipalRecord,
)


def _book() -> AddressBook:
    return AddressBook(
        [
            PrincipalRecord(
                id="jason",
                display_name="Jason",
                channels=[
                    PrincipalChannel(
                        transport="signal",
                        address="+15551110000",
                        durable=True,
                        preferred=True,
                    ),
                    PrincipalChannel(
                        transport="gmail", address="jason@example.com", durable=True
                    ),
                ],
            ),
            PrincipalRecord(
                id="katie",
                display_name="Katie",
                channels=[
                    PrincipalChannel(
                        transport="signal",
                        address="+15551110001",
                        durable=True,
                        preferred=True,
                    )
                ],
            ),
        ]
    )


def _stub(kind: str = "gmail") -> SimpleNamespace:
    stub = SimpleNamespace(
        _current_turn_kind=kind,
        _current_principal_display_name="Jason",
        address_book=_book(),
    )
    # ``_gmail_trust_deny`` calls ``self._gmail_send_allowed`` for
    # send_message; bind the real method onto the stub so the collaboration
    # is exercised for real.
    stub._gmail_send_allowed = lambda ti: SpeakingDaemon._gmail_send_allowed(stub, ti)
    return stub


def _deny(stub, tool, tool_input=None):
    return SpeakingDaemon._gmail_trust_deny(stub, tool, tool_input or {})


SENSITIVE = [
    "Bash",
    "Write",
    "Edit",
    "Task",
    "Agent",
    "mcp__alice__write_file",
    "mcp__alice__edit_file",
    "mcp__alice__write_directive",
    "mcp__alice__write_config",
    "mcp__alice__request_worker_reload",
    "mcp__alice__request_cozylobe_reload",
    "mcp__alice__request_host_claude",
]


@pytest.mark.parametrize("tool", SENSITIVE)
def test_sensitive_tools_denied_on_gmail_turn(tool):
    decision = _deny(_stub("gmail"), tool)
    assert decision is not None
    out = decision["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "EMAIL" in out["permissionDecisionReason"]


@pytest.mark.parametrize("tool", SENSITIVE + ["mcp__alice__send_message"])
def test_nothing_denied_on_non_gmail_turn(tool):
    # Signal / CLI turns are full-trust — the gate never fires.
    assert _deny(_stub("signal"), tool) is None
    assert _deny(_stub("cli"), tool) is None


@pytest.mark.parametrize(
    "tool",
    ["Read", "Grep", "Glob", "WebFetch", "mcp__alice__append_note", "mcp__alice__read_memory"],
)
def test_readonly_tools_allowed_on_gmail_turn(tool):
    # Not in the sensitive set → gate returns None (allowed).
    assert _deny(_stub("gmail"), tool) is None


def test_send_message_self_reply_allowed():
    stub = _stub("gmail")
    for recipient in ("self", "reply", "user", "sender"):
        assert _deny(stub, "mcp__alice__send_message", {"recipient": recipient}) is None


def test_send_message_signal_escalation_allowed():
    stub = _stub("gmail")
    assert _deny(stub, "mcp__alice__send_message", {"recipient": "jason"}) is None
    assert _deny(stub, "mcp__alice__send_message", {"recipient": "katie"}) is None


def test_send_message_third_party_denied():
    stub = _stub("gmail")
    # Raw phone number — not a known principal, treated as third party.
    assert _deny(stub, "mcp__alice__send_message", {"recipient": "+19998887777"}) is not None
    # Unknown principal name.
    assert _deny(stub, "mcp__alice__send_message", {"recipient": "bob"}) is not None
    # Empty recipient.
    assert _deny(stub, "mcp__alice__send_message", {"recipient": ""}) is not None
