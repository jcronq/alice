"""Tests for the dispatcher's deferred-state guard (issue #297, EC-7).

The dispatcher's main per-issue loop in ``alice_forge.dispatcher.main.run``
must consult the gh-state mirror before routing an issue to its v1/v3
handler. When ``cortex-memory/gh-state/<repo>-<N>.md`` declares
``type: deferred`` (written by Speaking/Thinking via
:func:`alice_forge.gh_state_mirror.write_deferred`), the dispatcher
skips the issue, increments ``report.skipped_dedup``, and posts a
single ``[SM] deferred-skip`` audit comment per 24h throttled via the
v3 :class:`EmittedLedger` (key ``"deferred-skip"``, TTL ``86400`` s).

Prior to this guard the dispatcher re-surfaced deferred issues every
poll cycle, producing duplicate spawns / audit traffic for #247 and a
redirect-stub issue on 2026-05-19 (see
[[2026-05-19-stale-cycle-dispatcher-gap]] and EC-7 in
[[2026-05-21-state-machine-edge-case-audit]]).
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest

from alice_forge import dispatcher as sm
from alice_forge import gh_state_mirror
from alice_forge.sm.ledger import load_ledger


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


REPO = "jcronq/alice"


@pytest.fixture(autouse=True)
def _stub_gh_list_issue_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the comments fetcher to empty so v1 handlers don't shell out."""
    monkeypatch.setattr(sm, "gh_list_issue_comments", lambda _repo, _n: [])


@pytest.fixture(autouse=True)
def _stub_gh_list_open_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the open-done sweep listing to empty (mirrors top-level dispatcher tests)."""
    monkeypatch.setattr(sm, "gh_list_open_done_sm_issues", lambda _repo: [])


@pytest.fixture
def gh_state_dir(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Redirect the gh-state mirror's note dir under tmp_path.

    Mirrors the fixture used in ``tests/test_gh_state_mirror.py`` —
    the dispatcher's lazy import resolves to the same module so the
    monkeypatch takes effect when ``run()`` calls ``read_state``.
    """
    state_dir = tmp_path / "gh-state"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(gh_state_mirror, "GH_STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def state_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "sm-dispatcher-state.json"


@pytest.fixture
def ledger_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "sm-emit-ledger.json"


class Recorder:
    """Captures ``post_comment`` invocations the dispatcher would have made."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, int, str]] = []

    def __call__(self, repo: str, number: int, body: str) -> None:
        self.posted.append((repo, number, body))


class HandlerSpy:
    """Captures spawn / label-edit / close calls that must NOT fire on a deferred issue."""

    def __init__(self) -> None:
        self.spawns: list[tuple[int, str, str]] = []
        self.edits: list[tuple[str, int, list[str], list[str]]] = []
        self.closes: list[tuple[str, int]] = []

    def edit_labels(
        self, repo: str, number: int, *, add: list[str], remove: list[str]
    ) -> None:
        self.edits.append((repo, number, list(add), list(remove)))

    def close_issue(self, repo: str, number: int) -> None:
        self.closes.append((repo, number))

    def spawn(self, issue: dict, art_label: str, _repo: str) -> str | None:
        self.spawns.append((issue["number"], art_label, "<spy>"))
        return "spy-spawn-id"


def _make_issue(
    number: int,
    *,
    sm_label: str = "sm:selected",
    art_label: str = "art:code",
    title: str = "Test task",
    author: str = "jcronq",
) -> dict:
    labels = [{"name": sm_label}, {"name": art_label}]
    return {
        "number": number,
        "title": title,
        "labels": labels,
        "author": {"login": author},
        "createdAt": "2026-05-12T10:00:00Z",
        "updatedAt": "2026-05-12T10:00:00Z",
    }


def _frozen_now_iso(when: dt.datetime) -> str:
    return when.isoformat(timespec="seconds")


def _run(
    *,
    issues: list[dict],
    state_path: pathlib.Path,
    ledger_path: pathlib.Path,
    recorder: Recorder,
    spy: HandlerSpy,
    now: dt.datetime,
    dry_run: bool = False,
):
    """Drive ``run()`` with all spawn/cleanup/verify/rebase machinery off
    so deferred-state behavior is the only thing under test.
    """
    return sm.run(
        repo=REPO,
        state_path=state_path,
        ledger_path=ledger_path,
        list_issues=lambda _repo: issues,
        list_stale_closed=lambda _repo: [],
        list_open_done=lambda _repo: [],
        post_comment=recorder,
        edit_labels=spy.edit_labels,
        close_issue=spy.close_issue,
        spawn=spy.spawn,
        has_live_spawn=lambda _n: False,
        count_running=lambda: 0,
        proactive_reap=lambda: (0, 0),
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        enable_rebase=False,
        dry_run=dry_run,
        now_iso=lambda: _frozen_now_iso(now),
        log=lambda _msg: None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_deferred_issue_skips_all_handlers(
    gh_state_dir: pathlib.Path,
    state_path: pathlib.Path,
    ledger_path: pathlib.Path,
) -> None:
    """A ``type: deferred`` gh-state note short-circuits the per-issue loop.

    No handler-side side-effect (spawn, label edit, close) may fire,
    and ``report.skipped_dedup`` records the skip.
    """
    gh_state_mirror.write_deferred(
        REPO, 247, reason="router module not on master", deferred_by="speaking"
    )
    recorder = Recorder()
    spy = HandlerSpy()
    now = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)

    exit_code, report = _run(
        issues=[_make_issue(247)],
        state_path=state_path,
        ledger_path=ledger_path,
        recorder=recorder,
        spy=spy,
        now=now,
    )

    assert exit_code == 0
    assert report.polled == 1
    assert report.skipped_dedup == 1
    # The handler-side spies must not have been touched. (The v1
    # ``hello`` comment + spawn would otherwise fire for an
    # ``sm:selected`` + ``art:code`` issue on first sight.)
    assert spy.spawns == []
    assert spy.edits == []
    assert spy.closes == []


def test_deferred_skip_comment_posted_first_cycle(
    gh_state_dir: pathlib.Path,
    state_path: pathlib.Path,
    ledger_path: pathlib.Path,
) -> None:
    """First cycle: dispatcher posts ``[SM] deferred-skip`` audit comment
    and writes a ledger entry with TTL 86400s."""
    gh_state_mirror.write_deferred(
        REPO,
        247,
        reason="router module not on master",
        deferred_by="speaking",
    )
    recorder = Recorder()
    spy = HandlerSpy()
    now = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)

    _run(
        issues=[_make_issue(247)],
        state_path=state_path,
        ledger_path=ledger_path,
        recorder=recorder,
        spy=spy,
        now=now,
    )

    assert len(recorder.posted) == 1
    posted_repo, posted_number, posted_body = recorder.posted[0]
    assert posted_repo == REPO
    assert posted_number == 247
    assert posted_body.startswith("[SM] deferred-skip")
    assert 'reason="router module not on master"' in posted_body
    assert "deferred_by=speaking" in posted_body
    assert "deferred_at=" in posted_body

    ledger = load_ledger(ledger_path)
    rec = ledger.find(247, "deferred-skip")
    assert rec is not None
    assert rec.ttl_seconds == 86400
    assert rec.cleared_at is None
    assert rec.metadata.get("reason") == "router module not on master"
    assert rec.metadata.get("deferred_by") == "speaking"


def test_deferred_skip_comment_throttled_second_cycle(
    gh_state_dir: pathlib.Path,
    state_path: pathlib.Path,
    ledger_path: pathlib.Path,
) -> None:
    """Second ``run()`` within 24h: ledger says active, no second comment.

    ``skipped_dedup`` still increments — the throttle is on the
    audit, not on the skip semantic.
    """
    gh_state_mirror.write_deferred(
        REPO, 247, reason="blocked", deferred_by="speaking"
    )
    spy = HandlerSpy()

    # First cycle — comment + ledger entry.
    recorder1 = Recorder()
    t0 = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    _run(
        issues=[_make_issue(247)],
        state_path=state_path,
        ledger_path=ledger_path,
        recorder=recorder1,
        spy=spy,
        now=t0,
    )
    assert len(recorder1.posted) == 1

    # Second cycle, one hour later — ledger entry is still active.
    recorder2 = Recorder()
    t1 = t0 + dt.timedelta(hours=1)
    _, report2 = _run(
        issues=[_make_issue(247)],
        state_path=state_path,
        ledger_path=ledger_path,
        recorder=recorder2,
        spy=spy,
        now=t1,
    )

    assert recorder2.posted == []
    assert report2.skipped_dedup == 1
    # Still no handler-side effects.
    assert spy.spawns == []
    assert spy.edits == []


def test_deferred_skip_comment_refreshes_after_ttl(
    gh_state_dir: pathlib.Path,
    state_path: pathlib.Path,
    ledger_path: pathlib.Path,
) -> None:
    """After 24h+1m: prior ledger entry is expired; dispatcher posts a fresh
    audit and writes a new ledger record."""
    gh_state_mirror.write_deferred(
        REPO, 247, reason="blocked", deferred_by="speaking"
    )
    spy = HandlerSpy()

    recorder1 = Recorder()
    t0 = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    _run(
        issues=[_make_issue(247)],
        state_path=state_path,
        ledger_path=ledger_path,
        recorder=recorder1,
        spy=spy,
        now=t0,
    )
    assert len(recorder1.posted) == 1
    first_emit = load_ledger(ledger_path).find(247, "deferred-skip")
    assert first_emit is not None

    # Past the TTL — 24h + 1m.
    recorder2 = Recorder()
    t1 = t0 + dt.timedelta(seconds=86400 + 60)
    _run(
        issues=[_make_issue(247)],
        state_path=state_path,
        ledger_path=ledger_path,
        recorder=recorder2,
        spy=spy,
        now=t1,
    )

    assert len(recorder2.posted) == 1
    ledger = load_ledger(ledger_path)
    # Two records total: the first was replaced by ``mark_emitted``,
    # the new one is active at t1.
    deferred_records = [
        r for r in ledger.records if r.side_effect == "deferred-skip"
    ]
    assert len(deferred_records) == 2
    active = [r for r in deferred_records if r.cleared_at is None]
    assert len(active) == 1
    assert active[0].emitted_at == t1


def test_non_deferred_issue_type_proceeds_normally(
    gh_state_dir: pathlib.Path,
    state_path: pathlib.Path,
    ledger_path: pathlib.Path,
) -> None:
    """A ``type: issue`` gh-state note does NOT trigger the guard.

    The v1 ``_process_selected`` path runs and posts the
    ``[SM] dispatcher-hello`` audit comment.
    """
    gh_state_mirror.write_note_atomic(
        REPO,
        300,
        {
            "_type": "issue",
            "state": "open",
            "title": "open issue",
            "createdAt": "2026-05-19T00:00:00Z",
            "updatedAt": "2026-05-19T00:00:00Z",
        },
    )
    recorder = Recorder()
    spy = HandlerSpy()
    now = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)

    _, report = _run(
        issues=[_make_issue(300)],
        state_path=state_path,
        ledger_path=ledger_path,
        recorder=recorder,
        spy=spy,
        now=now,
    )

    # The v1 ``sm:selected`` handler posted the hello comment.
    assert any(
        body.startswith("[SM] dispatcher-hello")
        for _repo, _n, body in recorder.posted
    ), recorder.posted
    # And the guard did NOT count this as a deferred skip.
    assert report.skipped_dedup == 0
    # No deferred-skip ledger entry written.
    ledger = load_ledger(ledger_path)
    assert ledger.find(300, "deferred-skip") is None


def test_no_gh_state_note_proceeds_normally(
    gh_state_dir: pathlib.Path,
    state_path: pathlib.Path,
    ledger_path: pathlib.Path,
) -> None:
    """No gh-state note at all → guard is silent, normal handler runs."""
    recorder = Recorder()
    spy = HandlerSpy()
    now = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)

    _, report = _run(
        issues=[_make_issue(400)],
        state_path=state_path,
        ledger_path=ledger_path,
        recorder=recorder,
        spy=spy,
        now=now,
    )

    assert report.skipped_dedup == 0
    assert any(
        body.startswith("[SM] dispatcher-hello")
        for _repo, _n, body in recorder.posted
    ), recorder.posted
    ledger = load_ledger(ledger_path)
    assert ledger.find(400, "deferred-skip") is None


def test_dry_run_deferred_no_comment_posted(
    gh_state_dir: pathlib.Path,
    state_path: pathlib.Path,
    ledger_path: pathlib.Path,
) -> None:
    """``dry_run=True`` → guard still fires (ledger written, skip counted) but
    no ``post_comment`` call is made."""
    gh_state_mirror.write_deferred(
        REPO, 247, reason="dry-run case", deferred_by="thinking"
    )
    recorder = Recorder()
    spy = HandlerSpy()
    now = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)

    _, report = _run(
        issues=[_make_issue(247)],
        state_path=state_path,
        ledger_path=ledger_path,
        recorder=recorder,
        spy=spy,
        now=now,
        dry_run=True,
    )

    # No audit comment posted under dry_run.
    assert recorder.posted == []
    # The skip is still counted so the dry-run cycle reports the same
    # observable counters as a real cycle (the on-disk ledger is not
    # persisted under dry_run by the dispatcher's outer save gate;
    # the in-memory ``mark_emitted`` still fires inside the guard
    # — exercised by the non-dry-run tests above).
    assert report.skipped_dedup == 1
    # Handler-side spies still untouched — guard short-circuited.
    assert spy.spawns == []
    assert spy.edits == []
    assert spy.closes == []
