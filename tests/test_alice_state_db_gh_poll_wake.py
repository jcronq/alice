"""Unit tests for :mod:`alice_state_db.gh_poll_wake`.

Issue #853, Fix 1: the new dedicated ``alice-gh-poll`` s6 service
runs these three derives in order so the memory-worker wake's
vault grooming can't starve them any more.

    1. derive_tasks_workers_events  (state_db)
    2. derive_sm_seed               (sm_seed)
    3. derive_gh_poll               (gh_poll)

Tests pin:

* The ordering above.
* The telemetry-only ``except`` wrap: a raise in any one of them
  doesn't stop the others and the wake still returns 0.
* The disabled-in-config short-circuit (``gh_poll.enabled = false``).
* The back-compat fallback to ``memory_worker.gh_poll_enabled``.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_state_db import derive as _derive_mod, gh_poll_wake
from alice_state_db.gh_poll import PollReport
from alice_state_db.sm_seed import SmSeedReport


# ----- test helpers -------------------------------------------------


def _write_config(mind: pathlib.Path, blob: dict) -> None:
    cfg_dir = mind / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "alice.config.json").write_text(json.dumps(blob))


class _OrderRecorder:
    """Monkeypatches each derive to append its name on call.

    Returns a canned Report dataclass so the module's heartbeat emit
    doesn't blow up on attribute access.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def state_db(self, mind: pathlib.Path):  # type: ignore[no-untyped-def]
        self.calls.append("state_db")
        return _derive_mod.DeriveReport()

    def sm_seed(self, mind: pathlib.Path):  # type: ignore[no-untyped-def]
        self.calls.append("sm_seed")
        return SmSeedReport()

    def gh_poll(self, mind, repos=None):  # type: ignore[no-untyped-def]
        self.calls.append("gh_poll")
        return PollReport()


# ----- tests --------------------------------------------------------


def test_gh_poll_wake_calls_derives_in_order(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single wake tick calls state_db → sm_seed → gh_poll in that order."""
    mind = tmp_path / "mind"
    mind.mkdir()
    log = tmp_path / "gh-poll.log"
    _write_config(mind, {})  # defaults (enabled = True)

    rec = _OrderRecorder()
    monkeypatch.setattr(
        gh_poll_wake._derive_mod,
        "derive_tasks_workers_events",
        rec.state_db,
    )
    # Patch BOTH the sm_seed module (via the late-import inside
    # gh_poll_wake.main) and the gh_poll module similarly.
    import alice_state_db.sm_seed as _sm_seed_mod
    import alice_state_db.gh_poll as _gh_poll_mod

    monkeypatch.setattr(_sm_seed_mod, "derive_sm_seed", rec.sm_seed)
    monkeypatch.setattr(_gh_poll_mod, "derive_gh_poll", rec.gh_poll)
    monkeypatch.setattr(
        gh_poll_wake, "GH_POLL_LIVENESS_PATH", tmp_path / "liveness"
    )

    rc = gh_poll_wake.main(["--mind", str(mind), "--log", str(log)])
    assert rc == 0
    assert rec.calls == ["state_db", "sm_seed", "gh_poll"], (
        f"expected canonical order, got {rec.calls}"
    )

    # One event line was written.
    assert log.is_file()
    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected one event line, got {lines!r}"
    record = json.loads(lines[0])
    assert record["event"] == "gh_poll_tick"
    # Spot-check the flattened prefix convention.
    assert "state_db_tasks_upserted" in record
    assert "sm_seed_items_upserted" in record
    assert "gh_poll_status" in record


def test_gh_poll_wake_swallows_derive_exception(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise in the middle derive doesn't kill the other two.

    Pins the telemetry-only ``except`` wrap: ``sm_seed`` raises,
    ``state_db`` and ``gh_poll`` still run, and ``main`` returns 0
    with one event emitted (so operators see the error count without
    the wake going silent).
    """
    mind = tmp_path / "mind"
    mind.mkdir()
    log = tmp_path / "gh-poll.log"
    _write_config(mind, {})

    calls: list[str] = []

    def _state_db(mind):  # type: ignore[no-untyped-def]
        calls.append("state_db")
        return _derive_mod.DeriveReport()

    def _sm_seed_boom(mind):  # type: ignore[no-untyped-def]
        calls.append("sm_seed")
        raise RuntimeError("boom from sm_seed")

    def _gh_poll(mind, repos=None):  # type: ignore[no-untyped-def]
        calls.append("gh_poll")
        return PollReport()

    monkeypatch.setattr(
        gh_poll_wake._derive_mod, "derive_tasks_workers_events", _state_db
    )
    import alice_state_db.sm_seed as _sm_seed_mod
    import alice_state_db.gh_poll as _gh_poll_mod

    monkeypatch.setattr(_sm_seed_mod, "derive_sm_seed", _sm_seed_boom)
    monkeypatch.setattr(_gh_poll_mod, "derive_gh_poll", _gh_poll)
    monkeypatch.setattr(
        gh_poll_wake, "GH_POLL_LIVENESS_PATH", tmp_path / "liveness"
    )

    rc = gh_poll_wake.main(["--mind", str(mind), "--log", str(log)])
    assert rc == 0, "wake must still exit cleanly when a derive raises"
    assert calls == ["state_db", "sm_seed", "gh_poll"], (
        f"all three phases must be invoked even when sm_seed raises; got {calls}"
    )

    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    # The fallback SmSeedReport carries the error in its errors list,
    # and ``to_dict()`` surfaces ``error_count`` with the raised entry.
    assert record["sm_seed_error_count"] >= 1


def test_gh_poll_wake_disabled_short_circuit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``gh_poll.enabled = false`` → exit 0 with no event + no derive calls."""
    mind = tmp_path / "mind"
    mind.mkdir()
    log = tmp_path / "gh-poll.log"
    _write_config(mind, {"gh_poll": {"enabled": False}})

    calls: list[str] = []

    def _boom_state(mind):  # type: ignore[no-untyped-def]
        calls.append("state_db")
        raise AssertionError("state_db must not be called when disabled")

    def _boom_sm(mind):  # type: ignore[no-untyped-def]
        calls.append("sm_seed")
        raise AssertionError("sm_seed must not be called when disabled")

    def _boom_gh(mind, repos=None):  # type: ignore[no-untyped-def]
        calls.append("gh_poll")
        raise AssertionError("gh_poll must not be called when disabled")

    monkeypatch.setattr(
        gh_poll_wake._derive_mod, "derive_tasks_workers_events", _boom_state
    )
    import alice_state_db.sm_seed as _sm_seed_mod
    import alice_state_db.gh_poll as _gh_poll_mod

    monkeypatch.setattr(_sm_seed_mod, "derive_sm_seed", _boom_sm)
    monkeypatch.setattr(_gh_poll_mod, "derive_gh_poll", _boom_gh)
    monkeypatch.setattr(
        gh_poll_wake, "GH_POLL_LIVENESS_PATH", tmp_path / "liveness"
    )

    rc = gh_poll_wake.main(["--mind", str(mind), "--log", str(log)])
    assert rc == 0
    assert calls == [], f"no derive should run when disabled; got {calls}"
    assert not log.is_file(), "no event should be written when disabled"


def test_gh_poll_wake_back_compat_memory_worker_gh_poll_enabled_false(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``memory_worker.gh_poll_enabled = false`` disables, even with
    no ``gh_poll`` block. This is the operator-side back-compat path
    carried over from the pre-#853 wake-coupled code: operators who
    pinned the old gh-poll to a no-op should keep that semantic.
    """
    mind = tmp_path / "mind"
    mind.mkdir()
    log = tmp_path / "gh-poll.log"
    _write_config(mind, {"memory_worker": {"gh_poll_enabled": False}})

    called = []

    def _not_called(*args, **kwargs):  # type: ignore[no-untyped-def]
        called.append(1)
        raise AssertionError("must not be called")

    monkeypatch.setattr(
        gh_poll_wake._derive_mod, "derive_tasks_workers_events", _not_called
    )
    import alice_state_db.sm_seed as _sm_seed_mod
    import alice_state_db.gh_poll as _gh_poll_mod

    monkeypatch.setattr(_sm_seed_mod, "derive_sm_seed", _not_called)
    monkeypatch.setattr(_gh_poll_mod, "derive_gh_poll", _not_called)
    monkeypatch.setattr(
        gh_poll_wake, "GH_POLL_LIVENESS_PATH", tmp_path / "liveness"
    )

    rc = gh_poll_wake.main(["--mind", str(mind), "--log", str(log)])
    assert rc == 0
    assert called == []
    assert not log.is_file()


def test_gh_poll_wake_resolve_repos_honors_both_config_locations(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``gh_poll.repos`` wins; falling back to
    ``memory_worker.gh_poll_repos`` when the former is absent.
    Tests this through :func:`_resolve_repos` directly — the full
    main path asserts the order already.
    """
    cfg_both = {
        "gh_poll": {"repos": ["gh/alpha"]},
        "memory_worker": {"gh_poll_repos": ["gh/legacy"]},
    }
    assert gh_poll_wake._resolve_repos(cfg_both) == ["gh/alpha"]

    cfg_legacy_only = {"memory_worker": {"gh_poll_repos": ["gh/legacy"]}}
    assert gh_poll_wake._resolve_repos(cfg_legacy_only) == ["gh/legacy"]

    cfg_empty: dict = {}
    assert gh_poll_wake._resolve_repos(cfg_empty) is None
