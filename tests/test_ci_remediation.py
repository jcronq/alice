"""Tests for ``watchers.ci_remediation`` — the master-CI red watcher.

Exercised end-to-end with fakes in place of ``gh``, the worker
spawner, and ``os.kill``. The watcher's load-bearing behaviors:

  * Disabled / empty-repos config is a no-op (no gh calls, no spawn).
  * ``gh run list`` output parses into the expected poll payload.
  * ``conclusion: failure`` + no in-flight worker + no open PR + no
    escalation → spawn one worker, write a spawn surface.
  * ``conclusion: failure`` + an in-flight worker on the same SHA
    whose PID is still alive → dedup, no new spawn.
  * ``conclusion: failure`` + an open remediation PR already exists →
    dedup, no new spawn.
  * ``conclusion: success`` with a tracked worker → clear state,
    write a success surface.
  * Two consecutive worker failures on the same SHA → escalation
    surface with ``deliver-to-jason: true``, NO new spawn.
  * Concurrency cap pins the number of in-flight workers across repos.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from watchers import ci_remediation as cr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mind_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """alice-mind scaffold: config dir + inner/surface dir."""
    d = tmp_path / "mind"
    (d / "config").mkdir(parents=True)
    (d / "inner" / "surface").mkdir(parents=True)
    return d


@pytest.fixture
def state_path(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "state"
    d.mkdir()
    return d / "ci-remediation-state.json"


@pytest.fixture
def scratch_root(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "scratch"
    d.mkdir()
    return d


def _write_config(mind_dir: pathlib.Path, **block: object) -> None:
    (mind_dir / "config" / "alice.config.json").write_text(
        json.dumps({"ci_remediation": {"enabled": True, **block}})
    )


class FakeSpawner:
    """Replaces the subprocess spawner. Records invocations, returns
    a canned PID. Can be told to fail (return None) for spawn-failure
    tests."""

    def __init__(self, pid: int = 42000, should_fail: bool = False) -> None:
        self.pid = pid
        self.should_fail = should_fail
        self.calls: list[tuple[pathlib.Path, str]] = []

    def __call__(self, scratch_dir: pathlib.Path, prompt: str) -> int | None:
        self.calls.append((scratch_dir, prompt))
        if self.should_fail:
            return None
        return self.pid


class StubGHRuns:
    """Returns per-repo canned ``gh run list`` payloads."""

    def __init__(self, payloads: dict[str, dict | None]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def __call__(self, repo: str) -> dict | None:
        self.calls.append(repo)
        return self.payloads.get(repo)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_disabled_config_is_noop(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    (mind_dir / "config" / "alice.config.json").write_text(
        json.dumps({"ci_remediation": {"enabled": False, "repos": ["acme/widgets"]}})
    )
    rc = cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        master_run_fn=lambda repo: pytest.fail("master_run_fn should not be called"),
        open_pr_fn=lambda repo, short: pytest.fail("open_pr_fn should not be called"),
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=lambda d, p: pytest.fail("spawn_fn should not be called"),
    )
    assert rc == 0
    assert not state_path.exists()


def test_empty_repos_is_noop(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    _write_config(mind_dir, repos=[])
    rc = cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        master_run_fn=lambda repo: pytest.fail("should not poll"),
        open_pr_fn=lambda repo, short: None,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=lambda d, p: pytest.fail("should not spawn"),
    )
    assert rc == 0


def test_load_config_defaults(mind_dir: pathlib.Path) -> None:
    (mind_dir / "config" / "alice.config.json").write_text(
        json.dumps(
            {
                "ci_remediation": {
                    "enabled": True,
                    "repos": ["acme/widgets"],
                }
            }
        )
    )
    cfg = cr.load_config(mind_dir)
    assert cfg.enabled is True
    assert cfg.repos == ["acme/widgets"]
    assert cfg.max_concurrent_workers == cr.DEFAULT_MAX_CONCURRENT_WORKERS
    assert cfg.wall_clock_cap_seconds == cr.DEFAULT_WALL_CLOCK_CAP
    assert cfg.escalation_threshold == cr.DEFAULT_ESCALATION_THRESHOLD


# ---------------------------------------------------------------------------
# Red + no in-flight → spawn
# ---------------------------------------------------------------------------


def test_red_with_no_worker_spawns_and_records_state(
    mind_dir: pathlib.Path, state_path: pathlib.Path, scratch_root: pathlib.Path
) -> None:
    _write_config(mind_dir, repos=["acme/widgets"])
    runs = StubGHRuns(
        {
            "acme/widgets": {
                "conclusion": "failure",
                "status": "completed",
                "headSha": "abc1234deadbeef",
                "url": "https://github.com/acme/widgets/actions/runs/1",
            }
        }
    )
    spawner = FakeSpawner(pid=99001)
    rc = cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        scratch_root=scratch_root,
        master_run_fn=runs,
        open_pr_fn=lambda repo, short: None,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=spawner,
    )
    assert rc == 0
    # Exactly one spawn.
    assert len(spawner.calls) == 1
    scratch_dir, prompt = spawner.calls[0]
    assert "abc1234" in str(scratch_dir)
    assert "acme/widgets" in prompt
    assert "abc1234deadbeef" in prompt
    # Prompt enforces --draft.
    assert "--draft" in prompt
    assert "Never pass `--no-draft`" in prompt
    # State records the in-flight worker.
    state = json.loads(state_path.read_text())
    inflight = state["repos"]["acme/widgets"]["current_remediation"]
    assert inflight["head_sha"] == "abc1234deadbeef"
    assert inflight["pid"] == 99001
    assert inflight["branch"].startswith("ci-remediation/abc1234-")
    # Surface emitted.
    surfaces = list((mind_dir / "inner" / "surface").glob("*.md"))
    assert len(surfaces) == 1
    body = surfaces[0].read_text()
    assert "priority: action" in body
    assert "type: ci-remediation-spawn" in body
    assert "ci-remediation-watcher" in body


def test_red_in_progress_run_is_silent(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """``status: in_progress`` (no conclusion yet) must not spawn."""
    _write_config(mind_dir, repos=["acme/widgets"])
    runs = StubGHRuns(
        {
            "acme/widgets": {
                "conclusion": None,
                "status": "in_progress",
                "headSha": "cafef00d",
                "url": "https://github.com/acme/widgets/actions/runs/2",
            }
        }
    )
    spawner = FakeSpawner()
    cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        master_run_fn=runs,
        open_pr_fn=lambda repo, short: None,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=spawner,
    )
    assert spawner.calls == []


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_red_with_live_worker_on_same_sha_is_deduped(
    mind_dir: pathlib.Path,
    state_path: pathlib.Path,
    scratch_root: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(mind_dir, repos=["acme/widgets"])
    # Seed state with a worker already running.
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "repos": {
                    "acme/widgets": {
                        "current_remediation": {
                            "head_sha": "abc1234deadbeef",
                            "spawned_at": "2026-11-06T22:00:00-05:00",
                            "pid": 12345,
                            "scratch_dir": str(scratch_root / "old"),
                            "branch": "ci-remediation/abc1234-1",
                            "pr_url": None,
                            "wall_clock_cap_seconds": 5400,
                        },
                        "failure_counts": {},
                        "last_observed_sha": "abc1234deadbeef",
                        "last_success_sha": None,
                        "escalated_shas": [],
                    }
                },
            }
        )
    )
    monkeypatch.setattr(cr, "_pid_alive", lambda pid: pid == 12345)
    runs = StubGHRuns(
        {
            "acme/widgets": {
                "conclusion": "failure",
                "status": "completed",
                "headSha": "abc1234deadbeef",
                "url": "https://github.com/acme/widgets/actions/runs/1",
            }
        }
    )
    spawner = FakeSpawner()
    # The 'now' is only 10 minutes after spawn — well under the cap.
    now = dt.datetime.fromisoformat("2026-11-06T22:10:00-05:00")
    cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        scratch_root=scratch_root,
        master_run_fn=runs,
        open_pr_fn=lambda repo, short: None,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=spawner,
        now=now,
    )
    assert spawner.calls == []
    # The in-flight record is untouched.
    state = json.loads(state_path.read_text())
    assert state["repos"]["acme/widgets"]["current_remediation"]["pid"] == 12345


def test_red_with_open_remediation_pr_is_deduped(
    mind_dir: pathlib.Path, state_path: pathlib.Path, scratch_root: pathlib.Path
) -> None:
    _write_config(mind_dir, repos=["acme/widgets"])
    runs = StubGHRuns(
        {
            "acme/widgets": {
                "conclusion": "failure",
                "status": "completed",
                "headSha": "abc1234deadbeef",
                "url": "https://github.com/acme/widgets/actions/runs/1",
            }
        }
    )
    spawner = FakeSpawner()

    def fake_open_pr(repo: str, short_sha: str) -> str | None:
        # Simulate an open PR on a matching branch.
        assert short_sha == "abc1234"
        return "https://github.com/acme/widgets/pull/99"

    cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        scratch_root=scratch_root,
        master_run_fn=runs,
        open_pr_fn=fake_open_pr,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=spawner,
    )
    assert spawner.calls == []
    # State should not record a spawn either.
    state = json.loads(state_path.read_text())
    assert state["repos"]["acme/widgets"]["current_remediation"] is None


# ---------------------------------------------------------------------------
# Green clears tracked state
# ---------------------------------------------------------------------------


def test_green_with_tracked_worker_clears_state_and_emits_success_surface(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    _write_config(mind_dir, repos=["acme/widgets"])
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "repos": {
                    "acme/widgets": {
                        "current_remediation": {
                            "head_sha": "oldredsha",
                            "spawned_at": "2026-11-06T22:00:00-05:00",
                            "pid": 7777,
                            "scratch_dir": "/tmp/scratch/old",
                            "branch": "ci-remediation/oldredsh-1",
                            "pr_url": None,
                            "wall_clock_cap_seconds": 5400,
                        },
                        "failure_counts": {"oldredsha": 1},
                        "last_observed_sha": "oldredsha",
                        "last_success_sha": None,
                        "escalated_shas": [],
                    }
                },
            }
        )
    )
    runs = StubGHRuns(
        {
            "acme/widgets": {
                "conclusion": "success",
                "status": "completed",
                "headSha": "newgreensha",
                "url": "https://github.com/acme/widgets/actions/runs/9",
            }
        }
    )
    cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        master_run_fn=runs,
        open_pr_fn=lambda repo, short: None,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=lambda d, p: pytest.fail("no spawn on green"),
    )
    state = json.loads(state_path.read_text())
    assert state["repos"]["acme/widgets"]["current_remediation"] is None
    assert state["repos"]["acme/widgets"]["last_success_sha"] == "newgreensha"
    surfaces = [p for p in (mind_dir / "inner" / "surface").glob("*.md")]
    assert any("ci-remediation-success" in p.read_text() for p in surfaces)


def test_green_without_prior_worker_is_silent(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    _write_config(mind_dir, repos=["acme/widgets"])
    runs = StubGHRuns(
        {
            "acme/widgets": {
                "conclusion": "success",
                "status": "completed",
                "headSha": "greensha",
                "url": "https://github.com/acme/widgets/actions/runs/1",
            }
        }
    )
    cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        master_run_fn=runs,
        open_pr_fn=lambda repo, short: None,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=lambda d, p: pytest.fail("no spawn on green"),
    )
    # No success surface — nothing had been in flight.
    surfaces = list((mind_dir / "inner" / "surface").glob("*.md"))
    assert surfaces == []


# ---------------------------------------------------------------------------
# Worker stall → failure counter → escalation
# ---------------------------------------------------------------------------


def test_expired_worker_is_marked_failed_and_counter_increments(
    mind_dir: pathlib.Path,
    state_path: pathlib.Path,
    scratch_root: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(mind_dir, repos=["acme/widgets"], wall_clock_cap_seconds=60)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "repos": {
                    "acme/widgets": {
                        "current_remediation": {
                            "head_sha": "sha1",
                            "spawned_at": "2026-11-06T22:00:00-05:00",
                            "pid": 11111,
                            "scratch_dir": str(scratch_root / "old"),
                            "branch": "ci-remediation/sha1-1",
                            "pr_url": None,
                            "wall_clock_cap_seconds": 60,
                        },
                        "failure_counts": {},
                        "last_observed_sha": "sha1",
                        "last_success_sha": None,
                        "escalated_shas": [],
                    }
                },
            }
        )
    )
    # Pretend the pid is alive, but the wall-clock cap has elapsed.
    monkeypatch.setattr(cr, "_pid_alive", lambda pid: True)
    now = dt.datetime.fromisoformat("2026-11-06T23:00:00-05:00")

    runs = StubGHRuns(
        {
            "acme/widgets": {
                "conclusion": "failure",
                "status": "completed",
                "headSha": "sha1",
                "url": "https://github.com/acme/widgets/actions/runs/1",
            }
        }
    )
    spawner = FakeSpawner(pid=22222)
    cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        scratch_root=scratch_root,
        master_run_fn=runs,
        open_pr_fn=lambda repo, short: None,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=spawner,
        now=now,
    )
    state = json.loads(state_path.read_text())
    assert state["repos"]["acme/widgets"]["failure_counts"]["sha1"] == 1
    # First failure is under the escalation threshold, so a new worker is spawned.
    assert len(spawner.calls) == 1
    assert state["repos"]["acme/widgets"]["current_remediation"]["pid"] == 22222


def test_two_consecutive_failures_escalate_not_respawn(
    mind_dir: pathlib.Path,
    state_path: pathlib.Path,
    scratch_root: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(mind_dir, repos=["acme/widgets"], wall_clock_cap_seconds=60)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "repos": {
                    "acme/widgets": {
                        "current_remediation": {
                            "head_sha": "sha1",
                            "spawned_at": "2026-11-06T22:00:00-05:00",
                            "pid": 11111,
                            "scratch_dir": str(scratch_root / "old"),
                            "branch": "ci-remediation/sha1-1",
                            "pr_url": None,
                            "wall_clock_cap_seconds": 60,
                        },
                        # Already failed once; this tick makes it twice.
                        "failure_counts": {"sha1": 1},
                        "last_observed_sha": "sha1",
                        "last_success_sha": None,
                        "escalated_shas": [],
                    }
                },
            }
        )
    )
    monkeypatch.setattr(cr, "_pid_alive", lambda pid: True)
    now = dt.datetime.fromisoformat("2026-11-06T23:00:00-05:00")
    runs = StubGHRuns(
        {
            "acme/widgets": {
                "conclusion": "failure",
                "status": "completed",
                "headSha": "sha1",
                "url": "https://github.com/acme/widgets/actions/runs/1",
            }
        }
    )
    spawner = FakeSpawner()
    cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        scratch_root=scratch_root,
        master_run_fn=runs,
        open_pr_fn=lambda repo, short: None,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=spawner,
        now=now,
    )
    # Second failure clears in-flight; escalation suppresses new spawn.
    assert spawner.calls == []
    state = json.loads(state_path.read_text())
    assert "sha1" in state["repos"]["acme/widgets"]["escalated_shas"]
    # Escalation surface written with deliver-to-jason.
    bodies = [
        p.read_text()
        for p in (mind_dir / "inner" / "surface").glob("*.md")
    ]
    assert any(
        "ci-remediation-escalation" in body and "deliver-to-jason: true" in body
        for body in bodies
    )


def test_already_escalated_sha_does_not_re_emit_surface(
    mind_dir: pathlib.Path,
    state_path: pathlib.Path,
    scratch_root: pathlib.Path,
) -> None:
    _write_config(mind_dir, repos=["acme/widgets"])
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "repos": {
                    "acme/widgets": {
                        "current_remediation": None,
                        "failure_counts": {"sha1": 5},
                        "last_observed_sha": "sha1",
                        "last_success_sha": None,
                        "escalated_shas": ["sha1"],
                    }
                },
            }
        )
    )
    runs = StubGHRuns(
        {
            "acme/widgets": {
                "conclusion": "failure",
                "status": "completed",
                "headSha": "sha1",
                "url": "https://github.com/acme/widgets/actions/runs/1",
            }
        }
    )
    spawner = FakeSpawner()
    cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        scratch_root=scratch_root,
        master_run_fn=runs,
        open_pr_fn=lambda repo, short: None,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=spawner,
    )
    assert spawner.calls == []
    surfaces = list((mind_dir / "inner" / "surface").glob("*.md"))
    # Zero surfaces — we don't re-nag Jason on every tick.
    assert surfaces == []


# ---------------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------------


def test_concurrency_cap_blocks_second_spawn(
    mind_dir: pathlib.Path,
    state_path: pathlib.Path,
    scratch_root: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(
        mind_dir,
        repos=["acme/widgets", "acme/thingo"],
        max_concurrent_workers=1,
    )
    runs = StubGHRuns(
        {
            "acme/widgets": {
                "conclusion": "failure",
                "status": "completed",
                "headSha": "widgetsha",
                "url": "https://github.com/acme/widgets/actions/runs/1",
            },
            "acme/thingo": {
                "conclusion": "failure",
                "status": "completed",
                "headSha": "thingosha",
                "url": "https://github.com/acme/thingo/actions/runs/1",
            },
        }
    )
    spawner = FakeSpawner(pid=33333)
    # First spawn's child pid must register as alive so the second
    # repo's poll sees global_inflight == 1.
    monkeypatch.setattr(cr, "_pid_alive", lambda pid: pid == 33333)
    cr.run(
        mind_dir=mind_dir,
        state_path=state_path,
        scratch_root=scratch_root,
        master_run_fn=runs,
        open_pr_fn=lambda repo, short: None,
        failing_checks_fn=lambda repo, sha: [],
        spawn_fn=spawner,
    )
    # Exactly one spawn across the two repos.
    assert len(spawner.calls) == 1


# ---------------------------------------------------------------------------
# gh adapters
# ---------------------------------------------------------------------------


def test_gh_master_run_parses_expected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(args: list[str], *, timeout: int = 60) -> str:
        captured["args"] = args
        return json.dumps(
            [
                {
                    "conclusion": "failure",
                    "status": "completed",
                    "headSha": "deadbeef",
                    "databaseId": 1234,
                    "url": "https://github.com/acme/widgets/actions/runs/1234",
                    "displayTitle": "fix: something",
                    "createdAt": "2026-11-06T22:00:00Z",
                }
            ]
        )

    monkeypatch.setattr(cr, "_run_gh", fake_run)
    out = cr.gh_master_run("acme/widgets")
    assert out is not None
    assert out["conclusion"] == "failure"
    assert out["headSha"] == "deadbeef"
    # Verify the gh argv shape.
    args = captured["args"]
    assert "run" in args and "list" in args
    assert "--branch" in args and "master" in args
    assert "--limit" in args and "1" in args


def test_gh_master_run_returns_none_on_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cr, "_run_gh", lambda args, timeout=60: "[]")
    assert cr.gh_master_run("acme/widgets") is None


def test_gh_open_remediation_pr_matches_branch_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cr,
        "_run_gh",
        lambda args, timeout=60: json.dumps(
            [
                {
                    "url": "https://github.com/acme/widgets/pull/5",
                    "headRefName": "ci-remediation/abc1234-9999",
                    "number": 5,
                }
            ]
        ),
    )
    out = cr.gh_open_remediation_pr("acme/widgets", "abc1234", own_login="alice")
    assert out == "https://github.com/acme/widgets/pull/5"


def test_gh_open_remediation_pr_ignores_non_matching_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cr,
        "_run_gh",
        lambda args, timeout=60: json.dumps(
            [
                {
                    "url": "https://github.com/acme/widgets/pull/6",
                    "headRefName": "some-other-branch",
                    "number": 6,
                }
            ]
        ),
    )
    out = cr.gh_open_remediation_pr("acme/widgets", "abc1234", own_login="alice")
    assert out is None
