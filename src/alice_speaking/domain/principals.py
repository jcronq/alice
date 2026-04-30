"""Address book + principal-based ACL.

Three concepts kept independent (mirrors transports.base):

- :class:`PrincipalRecord` — *who*. Stable, transport-independent identity.
- :class:`PrincipalChannel` — *where (one entry)*. A single addressable
  endpoint for a principal on one transport.
- :class:`AddressBook` — the lookup index. Replaces the dual world of
  ``cfg.allowed_senders`` (signal) + a hard-coded uid check (cli) with a
  single principals.yaml.

Storage: ``${ALICE_MIND_DIR}/config/principals.yaml`` by default. Override
with ``ALICE_PRINCIPALS_FILE``.

Migration: when the YAML is missing, :func:`load` synthesizes an in-memory
book from the ``ALLOWED_SENDERS`` env var + a default CLI principal mapped
to the daemon's own uid. This keeps existing deploys running without a
config-flag day; deploys can author the YAML at their leisure.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Optional

import yaml

from ..transports.base import ChannelRef, InboundMessage

if TYPE_CHECKING:
    from alice_core.config.personae import Personae


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrincipalChannel:
    """One addressable endpoint for a principal on one transport.

    ``preferred=True`` marks this as the channel to use when no explicit
    transport hint is given. ``durable=False`` means the address only
    exists during an active session (e.g. a CLI socket connection's uid)
    — it is never used as a target for proactive sends.
    """

    transport: str
    address: str
    durable: bool = True
    preferred: bool = False


@dataclass
class PrincipalRecord:
    """A single human / agent identity. Mutable so :meth:`AddressBook.learn`
    can refresh display names without rebuilding the index."""

    id: str
    display_name: str
    channels: list[PrincipalChannel] = field(default_factory=list)
    allowed: bool = True

    def channel_for(self, transport: str) -> Optional[PrincipalChannel]:
        """Return the preferred channel on ``transport``, falling back to
        the first entry if no preference is recorded."""
        first: Optional[PrincipalChannel] = None
        for ch in self.channels:
            if ch.transport != transport:
                continue
            if ch.preferred:
                return ch
            if first is None:
                first = ch
        return first


class AddressBook:
    """Lookup index over a set of :class:`PrincipalRecord` objects.

    All lookups are case-insensitive on principal id / display name.
    Native-id lookups are exact (phone numbers, uids — these are
    transport-private and the transport already normalized them).
    """

    def __init__(self, principals: Iterable[PrincipalRecord]) -> None:
        self._by_id: dict[str, PrincipalRecord] = {}
        self._native_index: dict[tuple[str, str], str] = {}
        for record in principals:
            self._add(record)

    def _add(self, record: PrincipalRecord) -> None:
        existing = self._by_id.get(record.id)
        if existing is not None:
            raise ValueError(f"duplicate principal id: {record.id!r}")
        self._by_id[record.id] = record
        for ch in record.channels:
            key = (ch.transport, ch.address)
            prior = self._native_index.get(key)
            if prior is not None and prior != record.id:
                raise ValueError(
                    f"native address {ch.transport}:{ch.address} bound to "
                    f"both {prior!r} and {record.id!r}"
                )
            self._native_index[key] = record.id

    # ------------------------------------------------------------------
    # Lookup

    def all_principals(self) -> list[PrincipalRecord]:
        return list(self._by_id.values())

    def lookup_by_id(self, principal_id: str) -> Optional[PrincipalRecord]:
        if not principal_id:
            return None
        if principal_id in self._by_id:
            return self._by_id[principal_id]
        lowered = principal_id.lower()
        for record in self._by_id.values():
            if record.id.lower() == lowered or record.display_name.lower() == lowered:
                return record
        return None

    def lookup_by_native(
        self, transport: str, native_id: str
    ) -> Optional[PrincipalRecord]:
        pid = self._native_index.get((transport, native_id))
        return self._by_id[pid] if pid is not None else None

    def is_allowed(self, transport: str, native_id: str) -> bool:
        record = self.lookup_by_native(transport, native_id)
        return record is not None and record.allowed

    def display_name_for(self, transport: str, native_id: str) -> str:
        """Display name lookup with a sane fallback (returns ``native_id``
        when no principal claims this address)."""
        record = self.lookup_by_native(transport, native_id)
        return record.display_name if record is not None else native_id

    def preferred_channel(
        self, principal_id: str, transport: Optional[str] = None
    ) -> Optional[ChannelRef]:
        """Resolve a principal id (or display name) to a :class:`ChannelRef`.

        ``transport=None`` picks the explicitly-preferred channel across
        all transports, falling back to the first listed. ``transport=X``
        narrows to that transport.
        """
        record = self.lookup_by_id(principal_id)
        if record is None or not record.channels:
            return None
        if transport is not None:
            ch = record.channel_for(transport)
            if ch is None:
                return None
            return ChannelRef(transport=ch.transport, address=ch.address, durable=ch.durable)
        for ch in record.channels:
            if ch.preferred:
                return ChannelRef(transport=ch.transport, address=ch.address, durable=ch.durable)
        ch = record.channels[0]
        return ChannelRef(transport=ch.transport, address=ch.address, durable=ch.durable)

    def emergency_recipient(self) -> Optional[ChannelRef]:
        """The principal to ping when an emergency surfaces and no inbound
        channel scoped the turn. Picks the first allowed principal with a
        durable signal channel — matches the legacy
        ``next(iter(cfg.allowed_senders))`` behavior."""
        for record in self._by_id.values():
            if not record.allowed:
                continue
            for ch in record.channels:
                if ch.transport == "signal" and ch.durable:
                    return ChannelRef(
                        transport=ch.transport,
                        address=ch.address,
                        durable=ch.durable,
                    )
        return None

    # ------------------------------------------------------------------
    # Inbound handling

    def learn(self, message: InboundMessage) -> None:
        """Refresh metadata for the principal that sent ``message``.

        Skeleton for now: only updates ``display_name`` for already-known
        principals when the inbound carries a richer one. Auto-registering
        unknown senders would defeat the ACL — handle that explicitly via
        the YAML (or a future tool) instead.
        """
        principal = message.principal
        record = self.lookup_by_native(principal.transport, principal.native_id)
        if record is None:
            return
        if principal.display_name and principal.display_name != record.display_name:
            log.debug(
                "address book: refreshing display_name for %s: %r -> %r",
                record.id,
                record.display_name,
                principal.display_name,
            )
            record.display_name = principal.display_name


# ---------------------------------------------------------------------------
# Loading


def load(
    *,
    yaml_path: Optional[pathlib.Path] = None,
    fallback_signal_senders: Optional[dict[str, str]] = None,
    fallback_cli_uid: Optional[int] = None,
    fallback_cli_principal_id: str = "owner",
    fallback_cli_display_name: str = "Owner (local CLI)",
    personae: Optional["Personae"] = None,
) -> AddressBook:
    """Load an :class:`AddressBook`.

    Order of precedence:

    1. If ``yaml_path`` exists, parse it as the authoritative source.
    2. Otherwise synthesize from the fallback inputs (one-shot WARN logged
       so deploys notice they're running on the migration shim).

    The fallback exists so existing ``ALLOWED_SENDERS`` deploys keep
    working without a coordinated config-file rollout. Once a deploy
    authors ``principals.yaml`` the env-var inputs become irrelevant.

    Plan 05 Phase 8: when a ``personae`` is supplied, the fallback CLI
    principal id + display default to the operator's configured user
    name (e.g. ``Friend`` → id ``friend``, display ``Friend (CLI)``)
    instead of the legacy ``owner`` / ``Owner (local CLI)``. The
    explicit ``fallback_cli_*`` kwargs still win when both are set.
    """
    if personae is not None:
        # Caller didn't pin custom defaults — derive from personae.
        if fallback_cli_principal_id == "owner":
            fallback_cli_principal_id = personae.user.name.lower().replace(" ", "_")
        if fallback_cli_display_name == "Owner (local CLI)":
            fallback_cli_display_name = f"{personae.user.name} (CLI)"

    if yaml_path is not None and yaml_path.is_file():
        try:
            data = yaml.safe_load(yaml_path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"{yaml_path}: invalid YAML: {exc}") from exc
        return _from_dict(data, source=str(yaml_path))

    log.warning(
        "principals.yaml not found at %s; synthesizing from ALLOWED_SENDERS "
        "+ default CLI principal (uid=%s). Author principals.yaml to drop "
        "this fallback.",
        yaml_path,
        fallback_cli_uid,
    )
    return _synth(
        fallback_signal_senders or {},
        fallback_cli_uid,
        fallback_cli_principal_id,
        fallback_cli_display_name,
    )


def _from_dict(data: dict, *, source: str) -> AddressBook:
    if not isinstance(data, dict):
        raise ValueError(f"{source}: top-level must be a mapping")
    raw = data.get("principals")
    if raw is None:
        raise ValueError(f"{source}: missing top-level `principals` key")
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: `principals` must be a mapping of id → record")
    records: list[PrincipalRecord] = []
    for pid, body in raw.items():
        if not isinstance(body, dict):
            raise ValueError(f"{source}: principal {pid!r} must be a mapping")
        display = body.get("display_name") or pid
        allowed = bool(body.get("allowed", True))
        ch_raw = body.get("channels") or []
        if not isinstance(ch_raw, list):
            raise ValueError(f"{source}: {pid}.channels must be a list")
        channels: list[PrincipalChannel] = []
        for ch in ch_raw:
            if not isinstance(ch, dict):
                raise ValueError(f"{source}: {pid}.channels entries must be mappings")
            transport = ch.get("transport")
            address = ch.get("address")
            if not transport or not address:
                raise ValueError(
                    f"{source}: {pid} channel missing transport/address: {ch!r}"
                )
            channels.append(
                PrincipalChannel(
                    transport=str(transport),
                    address=_normalize_address(str(transport), str(address)),
                    durable=bool(ch.get("durable", True)),
                    preferred=bool(ch.get("preferred", False)),
                )
            )
        records.append(
            PrincipalRecord(
                id=str(pid),
                display_name=str(display),
                channels=channels,
                allowed=allowed,
            )
        )
    return AddressBook(records)


def _normalize_address(transport: str, address: str) -> str:
    """Apply transport-specific address normalization at load time so
    later lookups have a stable form.

    Discord (Phase 4c): bare numeric ids are auto-prefixed with
    ``user:`` for back-compat with Phase 3b YAMLs (which used the bare
    discord user-id directly).
    """
    if transport == "discord" and ":" not in address:
        return f"user:{address}"
    return address


def _synth(
    signal_senders: dict[str, str],
    cli_uid: Optional[int],
    cli_principal_id: str,
    cli_display_name: str,
) -> AddressBook:
    records: dict[str, PrincipalRecord] = {}

    for number, name in signal_senders.items():
        pid = name.lower().replace(" ", "_")
        if pid not in records:
            records[pid] = PrincipalRecord(id=pid, display_name=name, channels=[])
        records[pid].channels.append(
            PrincipalChannel(
                transport="signal", address=number, durable=True, preferred=True
            )
        )

    if cli_uid is not None:
        cli_channel = PrincipalChannel(
            transport="cli", address=str(cli_uid), durable=False
        )
        if cli_principal_id in records:
            records[cli_principal_id].channels.append(cli_channel)
        else:
            records[cli_principal_id] = PrincipalRecord(
                id=cli_principal_id,
                display_name=cli_display_name,
                channels=[cli_channel],
            )

    return AddressBook(list(records.values()))


__all__ = [
    "PrincipalChannel",
    "PrincipalRecord",
    "AddressBook",
    "load",
]
