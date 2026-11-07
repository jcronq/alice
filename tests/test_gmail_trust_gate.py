"""Tests for the Gmail limited-trust PreToolUse gate (issue #682).

The gate methods only read ``self._current_turn_kind``,
``self._current_principal_display_name``, ``self.address_book``, and the
instance-level sensitivity knobs. We exercise them against a lightweight
stub ``self`` rather than booting a full :class:`SpeakingDaemon`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alice_speaking.daemon import (
    GMAIL_SENSITIVE_TOOLS_DEFAULT,
    SpeakingDaemon,
)
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
                        transport="gmail",
                        address="jason@example.com",
                        durable=True,
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


def _stub(
    kind: str = "gmail",
    *,
    limited_trust: bool = True,
    sensitive: frozenset[str] = GMAIL_SENSITIVE_TOOLS_DEFAULT,
) -> SimpleNamespace:
    """Build a stub daemon the gate methods can run against."""
    stub = SimpleNamespace(
        _current_turn_kind=kind,
        _current_principal_display_name="Jason",
        address_book=_book(),
        _gmail_limited_trust=limited_trust,
        _gmail_sensitive_tools=sensitive,
    )
    # ``_gmail_trust_deny`` calls ``self._gmail_send_allowed`` for
    # send_message; bind the real method onto the stub so the
    # collaboration is exercised for real.
    stub._gmail_send_allowed = lambda ti: SpeakingDaemon._gmail_send_allowed(
        stub, ti
    )
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
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    # Model-facing message must flag the channel + instruct escalation.
    assert "EMAIL" in out["permissionDecisionReason"]
    assert "Signal" in out["permissionDecisionReason"]


@pytest.mark.parametrize("tool", SENSITIVE + ["mcp__alice__send_message"])
def test_nothing_denied_on_non_gmail_turn(tool):
    # Signal / CLI turns are full-trust — the gate never fires.
    assert _deny(_stub("signal"), tool) is None
    assert _deny(_stub("cli"), tool) is None
    assert _deny(_stub("surface"), tool) is None


@pytest.mark.parametrize(
    "tool",
    [
        "Read",
        "Grep",
        "Glob",
        "WebFetch",
        "mcp__alice__append_note",
        "mcp__alice__read_memory",
    ],
)
def test_readonly_tools_allowed_on_gmail_turn(tool):
    # Not in the sensitive set → gate returns None (allowed).
    assert _deny(_stub("gmail"), tool) is None


def test_send_message_self_reply_allowed():
    stub = _stub("gmail")
    for recipient in ("self", "reply", "user", "sender"):
        assert (
            _deny(stub, "mcp__alice__send_message", {"recipient": recipient})
            is None
        )


def test_send_message_signal_escalation_allowed():
    stub = _stub("gmail")
    # Known principals with a signal channel: allowed.
    assert (
        _deny(stub, "mcp__alice__send_message", {"recipient": "jason"}) is None
    )
    assert (
        _deny(stub, "mcp__alice__send_message", {"recipient": "katie"}) is None
    )


def test_send_message_third_party_denied():
    stub = _stub("gmail")
    # Raw phone number — not a known principal, treated as third party.
    assert (
        _deny(
            stub, "mcp__alice__send_message", {"recipient": "+19998887777"}
        )
        is not None
    )
    # Unknown principal name.
    assert (
        _deny(stub, "mcp__alice__send_message", {"recipient": "bob"})
        is not None
    )
    # Empty recipient.
    assert (
        _deny(stub, "mcp__alice__send_message", {"recipient": ""})
        is not None
    )


# ---------------------------------------------------------------------------
# Env-override coverage (net-new in #682 — PR #535 hardcoded the set)


def test_gate_disabled_passes_through_everything():
    """GMAIL_LIMITED_TRUST=0 → gate is a no-op even on a gmail turn."""
    stub = _stub("gmail", limited_trust=False)
    for tool in SENSITIVE + ["mcp__alice__send_message"]:
        assert _deny(stub, tool) is None


def test_allowed_tools_override_subtracts_from_sensitive_set():
    """GMAIL_LIMITED_TRUST_ALLOWED_TOOLS=Bash → Bash passes through."""
    custom = GMAIL_SENSITIVE_TOOLS_DEFAULT - {"Bash"}
    stub = _stub("gmail", sensitive=custom)
    # Bash now allowed.
    assert _deny(stub, "Bash") is None
    # Other sensitive tools still denied.
    assert _deny(stub, "Write") is not None
    assert _deny(stub, "mcp__alice__request_worker_reload") is not None
