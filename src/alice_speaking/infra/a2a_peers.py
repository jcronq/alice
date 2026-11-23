"""A2A peering config — the symmetric client+server knob set.

Loaded from ``${ALICE_MIND_DIR}/config/a2a-peers.yaml`` by default.
When absent, peering stays off — the A2A transport runs as a
Phase-1 inbound-only server with no outbound token and no
inbound token→principal map. Feature is opt-in at every layer.

YAML shape (minimum)::

    # ${ALICE_MIND_DIR}/config/a2a-peers.yaml
    identity_token_env: ALICE_A2A_IDENTITY_TOKEN    # OUR outbound token

    peers:
      grand-alice:
        display_name: "Grand Alice (grandparents-home)"
        # The token grand-alice presents when SHE pings us. Looked up
        # via env var so the secret stays out of the vault.
        token_env: ALICE_A2A_PEER_TOKEN_GRAND_ALICE

      main-alice:
        display_name: "Main Alice (aimax1)"
        token_env: ALICE_A2A_PEER_TOKEN_MAIN_ALICE

Only ``peers.<id>.token_env`` is required for each entry. The
``identity_token_env`` falls back to ``ALICE_A2A_IDENTITY_TOKEN``
(same default the client-side helper uses) so single-line configs
stay single-line.

The address book (``principals.yaml``) carries the outbound URL
alongside each peer principal. We deliberately keep ``url`` out of
this file so there's one source of truth for "where do we reach
peer X" — the address book — and one source of truth for "how do
we authenticate" — this file.
"""

from __future__ import annotations

import logging
import os
import pathlib
from dataclasses import dataclass, field
from typing import Optional

import yaml


log = logging.getLogger(__name__)

_DEFAULT_IDENTITY_ENV = "ALICE_A2A_IDENTITY_TOKEN"


@dataclass(frozen=True)
class A2APeerEntry:
    """One entry in the peers map.

    ``token`` is already resolved from the env var at load time — the
    daemon never re-reads the environment, which keeps hot-reload
    behaviour predictable (change the env, restart the worker). An
    unresolved entry (env var missing) is loaded with ``token=None``;
    the transport skips registering it and logs a warning.
    """

    principal_id: str
    token: Optional[str]
    display_name: Optional[str] = None
    token_env: Optional[str] = None


@dataclass
class A2APeersConfig:
    """Parsed ``a2a-peers.yaml``.

    ``identity_token`` is the outbound-side secret presented to peers
    we POST to. ``peers`` is the inbound-side map, keyed by the
    principal id of each peer so the transport can resolve an incoming
    token back to an identity.

    Empty on default — no file + no env → no peering, no auth, same as
    Phase 1 behaviour.
    """

    identity_token: Optional[str] = None
    identity_token_env: str = _DEFAULT_IDENTITY_ENV
    peers: list[A2APeerEntry] = field(default_factory=list)

    def token_map(self) -> dict[str, str]:
        """Inbound ``token → principal_id`` map for the server side.

        Entries whose token didn't resolve (env var missing) are
        dropped silently — the operator has already seen the warning
        at load time.
        """
        return {
            entry.token: entry.principal_id
            for entry in self.peers
            if entry.token
        }

    def display_name_map(self) -> dict[str, str]:
        return {
            entry.principal_id: entry.display_name
            for entry in self.peers
            if entry.display_name
        }


def load(
    yaml_path: Optional[pathlib.Path],
    *,
    env: Optional[dict[str, str]] = None,
) -> A2APeersConfig:
    """Parse the peers YAML, resolving env-var-sourced tokens.

    Missing file → empty config (peering off). Malformed YAML raises
    :class:`ValueError` so startup fails loudly rather than booting
    with silent-off peering. ``env`` lets tests inject a controlled
    environment; production callers pass ``None`` and we read
    ``os.environ``.
    """
    environ = env if env is not None else os.environ
    if yaml_path is None or not yaml_path.is_file():
        identity_token = _env_lookup(environ, _DEFAULT_IDENTITY_ENV)
        if identity_token:
            log.info(
                "a2a peers: no YAML found at %s; using %s for outbound identity only",
                yaml_path,
                _DEFAULT_IDENTITY_ENV,
            )
        return A2APeersConfig(
            identity_token=identity_token,
            identity_token_env=_DEFAULT_IDENTITY_ENV,
        )

    try:
        data = yaml.safe_load(yaml_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{yaml_path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{yaml_path}: top-level must be a mapping")

    identity_env = str(data.get("identity_token_env") or _DEFAULT_IDENTITY_ENV)
    identity_token = _env_lookup(environ, identity_env)
    if not identity_token:
        log.warning(
            "a2a peers: identity_token_env=%r is unset; outbound A2A calls "
            "will be anonymous (peer will see unauthenticated catch-all)",
            identity_env,
        )

    peers_raw = data.get("peers") or {}
    if not isinstance(peers_raw, dict):
        raise ValueError(f"{yaml_path}: `peers` must be a mapping of id → entry")
    peers: list[A2APeerEntry] = []
    for pid, body in peers_raw.items():
        if not isinstance(body, dict):
            raise ValueError(f"{yaml_path}: peer {pid!r} must be a mapping")
        token_env = body.get("token_env")
        if not token_env:
            raise ValueError(
                f"{yaml_path}: peer {pid!r} missing required `token_env`"
            )
        token = _env_lookup(environ, str(token_env))
        if not token:
            log.warning(
                "a2a peers: token_env=%r for peer %r is unset; "
                "inbound requests from %r will fall back to the catch-all principal",
                token_env,
                pid,
                pid,
            )
        display = body.get("display_name")
        peers.append(
            A2APeerEntry(
                principal_id=str(pid),
                token=token,
                display_name=str(display) if display else None,
                token_env=str(token_env),
            )
        )

    return A2APeersConfig(
        identity_token=identity_token,
        identity_token_env=identity_env,
        peers=peers,
    )


def _env_lookup(env: dict[str, str], name: str) -> Optional[str]:
    raw = env.get(name)
    if raw is None:
        return None
    trimmed = raw.strip()
    return trimmed or None


__all__ = ["A2APeersConfig", "A2APeerEntry", "load"]
