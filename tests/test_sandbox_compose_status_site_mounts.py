"""Regression test for issue #849.

The DB-backed Overview readers from #814 open ``alice-state.db`` via
``alice_state_db.connect``, which resolves its default path under
``Path.home() / alice-mind / inner / state /``. Inside the
``alice-status`` container (``python:3.12-alpine`` run as a bare UID)
``Path.home()`` returns ``/`` (or similar), and the read-only
create-on-missing branch in ``read_api.connect`` then raced itself
under concurrent requests — surfacing as ``database is locked`` /
``no such table: items``.

Fix (config-only): the ``status-site`` service in
``sandbox/docker-compose.yml`` must

1. mount the mind-side ``inner/state/`` dir into the container at a
   stable path, and
2. set ``ALICE_STATE_DB_PATH`` so ``db_readers._resolve_db_path()`` and
   ``alice_state_db.connect`` both pick up that mount instead of
   falling back to ``Path.home()``.

Style mirror: ``tests/test_run_experiment.py::test_default_subagent_model_never_empty``
is already a regression for a different compose-injected env var — same
shape (load the yaml, assert on the ``status-site`` service), different
key.
"""

from __future__ import annotations

import pathlib

import yaml


# Compose file path, derived from this test file's location so the test
# runs correctly no matter where the repo is checked out.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_COMPOSE_PATH = _REPO_ROOT / "sandbox" / "docker-compose.yml"


def _load_status_site_service() -> dict:
    """Parse the compose file and return the ``status-site`` service block."""
    with _COMPOSE_PATH.open("r", encoding="utf-8") as fh:
        compose = yaml.safe_load(fh)
    assert "services" in compose, "compose file has no top-level `services` key"
    services = compose["services"]
    assert "status-site" in services, "compose file is missing the `status-site` service"
    return services["status-site"]


def _container_target(volume: str) -> str:
    """Pull the container-side target out of a short-form volume string.

    Short-form is ``host:container[:mode]``. The host half may contain
    compose default-value substitutions like ``${ALICE_MIND:-${HOME}/alice-mind}``
    whose ``:-`` splits naively too, so we can't just take ``split(":")[1]``.
    Instead we close every ``${…}`` group before splitting, then (if the
    tail is a mode like ``ro``/``rw``/``z``/``Z``) drop it.
    """
    # Replace each balanced ${…} with a placeholder free of ``:`` so
    # split(":") only hits the real host/container/mode boundaries.
    masked = []
    depth = 0
    for ch in volume:
        if ch == "$":
            masked.append(ch)
            continue
        if masked and masked[-1] == "$" and ch == "{":
            depth += 1
            masked.append(ch)
            continue
        if ch == "}" and depth > 0:
            depth -= 1
            masked.append(ch)
            continue
        if ch == ":" and depth > 0:
            masked.append("\x00")  # placeholder, not ``:``
            continue
        masked.append(ch)
    parts = "".join(masked).split(":")
    # Modes (``ro``, ``rw``, ``z``, ``Z``, ``cached``, ``delegated``,
    # ``consistent``, ``shared``, ``slave``, ``private``, ``rshared``,
    # ``rslave``, ``rprivate``) sit in the final slot and are short
    # non-path tokens. Anything starting with ``/`` is a path.
    if len(parts) >= 3 and not parts[-1].startswith("/"):
        return parts[-2]
    return parts[-1]


def test_status_site_mounts_state_dir_for_alice_state_db() -> None:
    """The ``status-site`` service must bind-mount ``inner/state/`` at
    ``/srv/state`` so ``alice-state.db`` + its WAL sidecars are visible
    to the DB-backed Overview readers (#849)."""
    service = _load_status_site_service()
    volumes = service.get("volumes") or []

    container_targets = [_container_target(str(v)) for v in volumes]
    assert "/srv/state" in container_targets, (
        "status-site service is missing a bind-mount for inner/state -> /srv/state; "
        f"found volume targets: {container_targets}"
    )


def test_status_site_sets_alice_state_db_path_env() -> None:
    """The ``status-site`` service must set ``ALICE_STATE_DB_PATH`` so
    ``db_readers._resolve_db_path()`` points at the mounted DB file
    rather than falling back to ``Path.home()`` (#849)."""
    service = _load_status_site_service()
    environment = service.get("environment") or {}
    assert environment.get("ALICE_STATE_DB_PATH") == "/srv/state/alice-state.db", (
        "status-site service must set ALICE_STATE_DB_PATH=/srv/state/alice-state.db; "
        f"got: {environment.get('ALICE_STATE_DB_PATH')!r}"
    )
