"""Tests for the AddressBook / principal-based ACL."""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from alice_speaking.domain.principals import (
    AddressBook,
    PrincipalChannel,
    PrincipalRecord,
    load,
)
from alice_speaking.transports.base import ChannelRef, InboundMessage, Principal


def _book() -> AddressBook:
    return AddressBook([
        PrincipalRecord(
            id="owner",
            display_name="Owner",
            channels=[
                PrincipalChannel(
                    transport="signal", address="+15555550100",
                    durable=True, preferred=True,
                ),
                PrincipalChannel(
                    transport="cli", address="1000", durable=False
                ),
            ],
        ),
        PrincipalRecord(
            id="friend",
            display_name="Friend",
            channels=[
                PrincipalChannel(
                    transport="signal", address="+15555550101",
                    durable=True, preferred=True,
                ),
            ],
        ),
    ])


def test_lookup_by_native_signal():
    book = _book()
    record = book.lookup_by_native("signal", "+15555550100")
    assert record is not None
    assert record.id == "owner"


def test_lookup_by_native_cli():
    book = _book()
    record = book.lookup_by_native("cli", "1000")
    assert record is not None
    assert record.id == "owner"


def test_lookup_by_id_case_insensitive_on_id_and_display_name():
    # Use distinct id vs display_name so each branch of the lookup is exercised.
    book = AddressBook([PrincipalRecord(
        id="alpha",
        display_name="Owner",
        channels=[PrincipalChannel(transport="signal", address="+15555550100")],
    )])
    assert book.lookup_by_id("alpha").id == "alpha"
    assert book.lookup_by_id("ALPHA").id == "alpha"
    assert book.lookup_by_id("Owner").id == "alpha"
    assert book.lookup_by_id("OWNER").id == "alpha"


def test_is_allowed_respects_flag():
    blocked = PrincipalRecord(
        id="bob",
        display_name="Bob",
        channels=[PrincipalChannel(transport="signal", address="+19999999999")],
        allowed=False,
    )
    book = AddressBook([blocked])
    assert book.is_allowed("signal", "+19999999999") is False


def test_is_allowed_unknown_returns_false():
    assert _book().is_allowed("signal", "+10000000000") is False


def test_preferred_channel_picks_preferred_then_first():
    book = _book()
    assert book.preferred_channel("owner") == ChannelRef(
        transport="signal", address="+15555550100", durable=True
    )
    # Narrowed by transport
    assert book.preferred_channel("owner", "cli") == ChannelRef(
        transport="cli", address="1000", durable=False
    )
    assert book.preferred_channel("owner", "discord") is None


def test_emergency_recipient_picks_first_allowed_signal_durable():
    book = _book()
    ch = book.emergency_recipient()
    assert ch == ChannelRef(transport="signal", address="+15555550100", durable=True)


def test_display_name_falls_back_to_native_id_when_unknown():
    assert _book().display_name_for("signal", "+10000000000") == "+10000000000"


def test_address_book_rejects_duplicate_id():
    with pytest.raises(ValueError):
        AddressBook([
            PrincipalRecord(id="x", display_name="X", channels=[]),
            PrincipalRecord(id="x", display_name="X again", channels=[]),
        ])


def test_address_book_rejects_native_address_collision():
    with pytest.raises(ValueError):
        AddressBook([
            PrincipalRecord(
                id="a", display_name="A",
                channels=[PrincipalChannel(transport="signal", address="+1")],
            ),
            PrincipalRecord(
                id="b", display_name="B",
                channels=[PrincipalChannel(transport="signal", address="+1")],
            ),
        ])


def test_learn_refreshes_display_name():
    book = _book()
    book.learn(InboundMessage(
        principal=Principal(
            transport="signal", native_id="+15555550100",
            display_name="Owner (work phone)",
        ),
        origin=ChannelRef(
            transport="signal", address="+15555550100", durable=True
        ),
        text="hi",
        timestamp=0.0,
    ))
    assert book.lookup_by_id("owner").display_name == "Owner (work phone)"


def test_learn_skips_unknown_principals():
    book = _book()
    book.learn(InboundMessage(
        principal=Principal(
            transport="signal", native_id="+19999999999",
            display_name="Stranger",
        ),
        origin=ChannelRef(
            transport="signal", address="+19999999999", durable=True
        ),
        text="hi",
        timestamp=0.0,
    ))
    # Did not auto-add — ACL still rejects.
    assert book.is_allowed("signal", "+19999999999") is False


# ---------------------------------------------------------------------------
# YAML loader


def test_load_from_yaml(tmp_path: pathlib.Path):
    p = tmp_path / "principals.yaml"
    p.write_text(textwrap.dedent("""\
        principals:
          owner:
            display_name: Owner
            channels:
              - {transport: signal, address: "+15555550100", preferred: true}
              - {transport: cli, address: "1000", durable: false}
              - {transport: discord, address: "284000000000000000"}
          friend:
            display_name: Friend
            channels:
              - {transport: signal, address: "+15555550101"}
            allowed: false
    """))
    book = load(yaml_path=p)
    assert book.lookup_by_id("owner").display_name == "Owner"
    assert book.is_allowed("signal", "+15555550100") is True
    assert book.is_allowed("signal", "+15555550101") is False  # explicitly disallowed
    # Bare discord ids are auto-prefixed with user: at load time (Phase 4c).
    assert book.preferred_channel("owner", "discord") == ChannelRef(
        transport="discord", address="user:284000000000000000", durable=True
    )


def test_load_synth_fallback_when_yaml_missing(tmp_path: pathlib.Path, caplog):
    book = load(
        yaml_path=tmp_path / "absent.yaml",
        fallback_signal_senders={"+15555550100": "Owner"},
        fallback_cli_uid=1000,
    )
    assert book.is_allowed("signal", "+15555550100") is True
    assert book.is_allowed("cli", "1000") is True
    # Synth merges signal + cli into the same principal id.
    assert book.lookup_by_native("signal", "+15555550100").id == "owner"


def test_load_rejects_bad_yaml_shape(tmp_path: pathlib.Path):
    p = tmp_path / "bad.yaml"
    p.write_text("principals: not-a-mapping\n")
    with pytest.raises(ValueError):
        load(yaml_path=p)


def test_load_normalizes_bare_discord_ids_to_user_prefix(tmp_path: pathlib.Path):
    """Phase 4c added user:/channel: prefixes for Discord. Phase 3b
    YAMLs used bare numeric ids (DM-only); the loader auto-prefixes
    them with ``user:`` so back-compat lookups keep working."""
    p = tmp_path / "principals.yaml"
    p.write_text(textwrap.dedent("""\
        principals:
          owner:
            display_name: Owner
            channels:
              - {transport: discord, address: "123"}              # bare → user:123
              - {transport: discord, address: "user:456"}         # already prefixed
              - {transport: discord, address: "channel:789"}      # guild
    """))
    book = load(yaml_path=p)
    addresses = {ch.address for ch in book.lookup_by_id("owner").channels}
    assert addresses == {"user:123", "user:456", "channel:789"}
    # Native-id lookups use the prefixed form.
    assert book.is_allowed("discord", "user:123") is True
    assert book.is_allowed("discord", "channel:789") is True
    assert book.is_allowed("discord", "123") is False  # not bare anymore
