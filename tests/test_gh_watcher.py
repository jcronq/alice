"""Tests for ``alice_watchers.github`` — the GitHub repo watcher.

Exercised end-to-end with a fake ``api`` callable (in place of ``gh api``)
pointed at a tmp mind + state dir. The watcher's load-bearing behaviors:

  * First run primes seen-ID sets without emitting notes (no historical
    flood when the user adds a repo).
  * Second run only emits notes for events not in the primed set.
  * ``author_association`` trust gating silences randos on issues + PR
    conversation comments + standalone issue comments. PR reviews,
    inline review comments, and check failures always fire.
  * Auth failures emit one loud note and short-circuit, deduped by
    the dedup window.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_watchers import github as gh_watcher


@pytest.fixture
def mind_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "mind"
    (d / "config").mkdir(parents=True)
    (d / "inner" / "notes").mkdir(parents=True)
    (d / "config" / "alice.config.json").write_text(
        json.dumps({"github_watcher": {"enabled": True, "repos": ["acme/widgets"]}})
    )
    return d


@pytest.fixture
def state_path(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "state"
    d.mkdir()
    return d / "gh-watcher-state.json"


def _make_pr(
    *,
    number: int,
    title: str = "PR",
    state: str = "open",
    merged_at: str | None = None,
    head_sha: str = "deadbeef",
    body: str = "",
    user: str = "alice",
) -> dict:
    return {
        "number": number,
        "title": title,
        "state": state,
        "merged_at": merged_at,
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "head": {"sha": head_sha},
        "body": body,
        "user": {"login": user},
        "draft": False,
        "created_at": "2026-04-29T12:00:00Z",
    }


def _make_issue(
    *,
    number: int,
    title: str = "Issue",
    state: str = "open",
    body: str = "",
    user: str = "alice",
    author_association: str = "OWNER",
) -> dict:
    return {
        "number": number,
        "title": title,
        "state": state,
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
        "body": body,
        "user": {"login": user},
        "author_association": author_association,
        "created_at": "2026-04-29T12:00:00Z",
        # No ``pull_request`` key — pure issue.
    }


class FakeAPI:
    """Replaces ``gh_api`` for tests. Routes by URL prefix to canned data."""

    def __init__(self) -> None:
        self.pulls: list[dict] = []
        self.reviews: dict[int, list[dict]] = {}
        self.review_comments: dict[int, list[dict]] = {}
        self.pr_conversation_comments: dict[int, list[dict]] = {}
        self.check_runs: dict[str, list[dict]] = {}
        self.issues: list[dict] = []
        self.issue_thread_comments: dict[int, list[dict]] = {}
        self.calls: list[str] = []

    def __call__(self, path: str):
        self.calls.append(path)
        if path.startswith("repos/acme/widgets/pulls?"):
            return self.pulls
        if path.startswith("repos/acme/widgets/issues?"):
            return self.issues
        if "/reviews" in path:
            n = _extract_number(path, "/pulls/", "/reviews")
            return self.reviews.get(n, [])
        if "/pulls/" in path and path.endswith("/comments?per_page=100"):
            n = _extract_number(path, "/pulls/", "/comments")
            return self.review_comments.get(n, [])
        if "/issues/" in path and "/comments" in path:
            n = _extract_number(path, "/issues/", "/comments")
            # Same endpoint serves both PR-conversation and standalone-issue
            # comments. Tests track them separately so we look up by which
            # numbers were registered as issues vs PRs.
            issue_numbers = {i["number"] for i in self.issues}
            if n in issue_numbers:
                return self.issue_thread_comments.get(n, [])
            return self.pr_conversation_comments.get(n, [])
        if "/check-runs" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            return {"check_runs": self.check_runs.get(sha, [])}
        raise AssertionError(f"unexpected api path: {path}")


def _extract_number(path: str, lhs: str, rhs: str) -> int:
    return int(path.split(lhs)[1].split(rhs)[0])


# ---------------------------------------------------------------------------
# Original PR-coverage tests (carried forward; trust now applies to the
# PR-conversation comment, so the comment author needs an OWNER association
# for the existing "should emit a note" assertion to hold).
# ---------------------------------------------------------------------------


def test_first_run_primes_without_emitting_notes(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """A fresh state file means we have no baseline — emitting historical
    activity from the recent-PR window would flood inner/notes/. The first
    run must capture all current IDs but write zero notes."""
    api = FakeAPI()
    api.pulls = [_make_pr(number=42, title="Add widgets")]
    api.reviews[42] = [
        {
            "id": 1001,
            "state": "APPROVED",
            "user": {"login": "bob"},
            "body": "lgtm",
            "submitted_at": "2026-04-29T10:00:00Z",
            "html_url": "https://example.com/r/1001",
        }
    ]
    api.pr_conversation_comments[42] = [
        {
            "id": 2001,
            "user": {"login": "carol"},
            "body": "tests?",
            "author_association": "OWNER",
            "created_at": "2026-04-29T11:00:00Z",
            "html_url": "https://example.com/c/2001",
        }
    ]
    api.review_comments[42] = []
    api.check_runs["deadbeef"] = []

    rc = gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    assert rc == 0
    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert notes == [], f"first run should not emit notes, got {[n.name for n in notes]}"

    state = json.loads(state_path.read_text())
    repo_state = state["repos"]["acme/widgets"]
    assert repo_state["first_run"] is False
    assert 1001 in repo_state["seen_review_ids"]
    assert 2001 in repo_state["seen_issue_comment_ids"]
    assert repo_state["pr_state"]["42"] == "open"


def test_second_run_emits_only_new_events(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """After priming, only IDs not yet seen should produce notes."""
    api = FakeAPI()
    api.pulls = [_make_pr(number=42, title="Add widgets")]
    api.reviews[42] = [
        {
            "id": 1001,
            "state": "APPROVED",
            "user": {"login": "bob"},
            "body": "lgtm",
            "submitted_at": "2026-04-29T10:00:00Z",
            "html_url": "https://example.com/r/1001",
        }
    ]
    api.pr_conversation_comments[42] = []
    api.review_comments[42] = []
    api.check_runs["deadbeef"] = []

    # First pass: prime.
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    # Second pass: a new (trusted) comment shows up + a check run fails.
    api.pr_conversation_comments[42] = [
        {
            "id": 2002,
            "user": {"login": "dave"},
            "body": "I have concerns",
            "author_association": "OWNER",
            "created_at": "2026-04-29T12:00:00Z",
            "html_url": "https://example.com/c/2002",
        }
    ]
    api.check_runs["deadbeef"] = [
        {
            "id": 9001,
            "name": "lint",
            "status": "completed",
            "conclusion": "failure",
            "completed_at": "2026-04-29T12:01:00Z",
            "html_url": "https://example.com/run/9001",
            "output": {"summary": "ruff complained"},
        }
    ]

    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    notes = sorted((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 2, [n.name for n in notes]
    bodies = [p.read_text() for p in notes]
    joined = "\n".join(bodies)
    assert "tag: github" in joined
    assert "I have concerns" in joined
    assert "lint" in joined and "failure" in joined


def test_state_transition_emits_pr_state_event(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    api = FakeAPI()
    api.pulls = [_make_pr(number=7, title="Refactor")]
    api.reviews[7] = []
    api.pr_conversation_comments[7] = []
    api.review_comments[7] = []
    api.check_runs["deadbeef"] = []

    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    api.pulls = [
        _make_pr(
            number=7,
            title="Refactor",
            state="closed",
            merged_at="2026-04-29T15:00:00Z",
        )
    ]
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    body = notes[0].read_text()
    assert "open → merged" in body
    assert "Refactor" in body


def test_disabled_or_empty_repos_is_noop(
    tmp_path: pathlib.Path, state_path: pathlib.Path
) -> None:
    mind = tmp_path / "mind"
    (mind / "config").mkdir(parents=True)
    (mind / "inner" / "notes").mkdir(parents=True)
    (mind / "config" / "alice.config.json").write_text("{}")

    rc = gh_watcher.run(
        mind_dir=mind,
        state_path=state_path,
        api=lambda _: pytest.fail("api must not be called when watcher disabled"),
        log=lambda _: None,
    )
    assert rc == 0
    assert not state_path.exists() or json.loads(state_path.read_text()).get("repos") in (None, {}), (
        "no state should be written when there are no repos to poll"
    )


def test_auth_failure_emits_loud_note_and_dedups(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    def boom(_path: str):
        raise gh_watcher.GHCommandError(
            returncode=1,
            stderr="HTTP 401: Bad credentials",
            args=["gh", "api", "x"],
        )

    rc = gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=boom, log=lambda _: None)
    assert rc == 1

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    assert "github-watcher-error" in notes[0].read_text()

    # Second pass within the dedup window must not write another note.
    rc2 = gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=boom, log=lambda _: None)
    assert rc2 == 1
    notes_after = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes_after) == 1, "auth-error note should be deduped within the window"


def test_seen_id_lists_capped(mind_dir: pathlib.Path, state_path: pathlib.Path) -> None:
    """The state file would grow without bound otherwise; verify the cap."""
    state = {
        "version": 1,
        "repos": {
            "acme/widgets": {
                "seen_review_ids": list(range(gh_watcher.SEEN_ID_CAP + 500)),
                "seen_review_comment_ids": [],
                "seen_issue_comment_ids": [],
                "seen_standalone_issue_comment_ids": [],
                "seen_check_run_ids": [],
                "pr_state": {},
                "issue_state": {},
                "first_run": False,
            }
        },
    }
    gh_watcher.save_state(state_path, state)
    reloaded = json.loads(state_path.read_text())
    assert (
        len(reloaded["repos"]["acme/widgets"]["seen_review_ids"]) == gh_watcher.SEEN_ID_CAP
    )
    assert reloaded["repos"]["acme/widgets"]["seen_review_ids"][-1] == (
        gh_watcher.SEEN_ID_CAP + 499
    )


# ---------------------------------------------------------------------------
# author_association trust gating
# ---------------------------------------------------------------------------


def test_is_trusted_association_helper() -> None:
    trusted = frozenset({"OWNER", "COLLABORATOR", "MEMBER"})
    assert gh_watcher.is_trusted_association("OWNER", trusted)
    assert gh_watcher.is_trusted_association("collaborator", trusted)  # case-insensitive
    assert not gh_watcher.is_trusted_association("CONTRIBUTOR", trusted)
    assert not gh_watcher.is_trusted_association("FIRST_TIME_CONTRIBUTOR", trusted)
    assert not gh_watcher.is_trusted_association("NONE", trusted)
    assert not gh_watcher.is_trusted_association(None, trusted)
    assert not gh_watcher.is_trusted_association("", trusted)


def test_trusted_owner_issue_emits_new_issue_note(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """An issue from a trusted author (OWNER / COLLABORATOR / MEMBER) seen
    after the prime pass produces a ``new_issue`` note."""
    api = FakeAPI()
    api.issues = []  # nothing yet at prime
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    api.issues = [_make_issue(number=11, title="Caught a bug", user="jcronq")]
    api.issue_thread_comments[11] = []
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1, [n.name for n in notes]
    body = notes[0].read_text()
    assert "New issue opened" in body
    assert "jcronq" in body and "(OWNER)" in body
    assert "#11" in body


def test_untrusted_issue_silent_but_marked_seen(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """An issue from a NONE author (random drive-by) writes no note. Its
    state still gets recorded so we don't re-evaluate it forever."""
    api = FakeAPI()
    api.issues = []
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    api.issues = [
        _make_issue(
            number=99,
            title="please fix",
            user="randoperson",
            author_association="NONE",
        )
    ]
    api.issue_thread_comments[99] = []
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert notes == [], "rando-opened issue must not produce a note"
    state = json.loads(state_path.read_text())
    assert state["repos"]["acme/widgets"]["issue_state"]["99"] == "open", (
        "issue state should still be tracked so future polls don't re-fire"
    )


def test_untrusted_issue_comment_silent_but_marked_seen(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """A comment from a NONE author on a trusted-author issue is silent,
    and its ID is recorded so it doesn't re-evaluate forever."""
    api = FakeAPI()
    # Prime with the issue already there so we don't fire a new_issue event.
    api.issues = [_make_issue(number=12, title="Let's discuss", user="jcronq")]
    api.issue_thread_comments[12] = []
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    # A rando shows up.
    api.issue_thread_comments[12] = [
        {
            "id": 8001,
            "user": {"login": "randotalker"},
            "body": "+1 me too",
            "author_association": "NONE",
            "created_at": "2026-04-29T13:00:00Z",
            "html_url": "https://example.com/c/8001",
        }
    ]
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert notes == [], "rando comment must not produce a note"
    state = json.loads(state_path.read_text())
    assert 8001 in state["repos"]["acme/widgets"]["seen_standalone_issue_comment_ids"]


def test_trusted_member_issue_comment_emits_note(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """A MEMBER-association comment on an existing issue produces a
    standalone_issue_comment note."""
    api = FakeAPI()
    api.issues = [_make_issue(number=12, title="Coordinate", user="jcronq")]
    api.issue_thread_comments[12] = []
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    api.issue_thread_comments[12] = [
        {
            "id": 8002,
            "user": {"login": "drapw"},
            "body": "I have a thought",
            "author_association": "MEMBER",
            "created_at": "2026-04-29T13:30:00Z",
            "html_url": "https://example.com/c/8002",
        }
    ]
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    body = notes[0].read_text()
    assert "Issue comment" in body
    assert "drapw" in body and "(MEMBER)" in body
    assert "I have a thought" in body


def test_pr_review_always_fires_regardless_of_association(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """Reviews and inline review comments aren't trust-gated. Even a
    NONE-association review still produces a note — randos rarely review
    code, and when they do it's signal."""
    api = FakeAPI()
    api.pulls = [_make_pr(number=42, title="Patch from outside")]
    api.reviews[42] = []
    api.review_comments[42] = []
    api.pr_conversation_comments[42] = []
    api.check_runs["deadbeef"] = []
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    api.reviews[42] = [
        {
            "id": 5005,
            "state": "CHANGES_REQUESTED",
            "user": {"login": "stranger"},
            "author_association": "NONE",
            "body": "needs work",
            "submitted_at": "2026-04-29T14:00:00Z",
            "html_url": "https://example.com/r/5005",
        }
    ]
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    assert "stranger" in notes[0].read_text()
    assert "changes_requested" in notes[0].read_text()


def test_legacy_state_primes_issues_silently_on_upgrade(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """A state file written before issue support shipped (no
    ``issues_primed`` key, ``first_run: false`` from prior PR-only polls)
    must not flood the inbox with every existing trusted-author issue on
    the first post-upgrade poll. PR-side behavior must continue normally."""
    # Hand-craft a "legacy" state — exactly what was on disk after the
    # PR-only watcher ran. No ``issues_primed``, no ``issue_state``.
    legacy = {
        "version": 1,
        "repos": {
            "acme/widgets": {
                "first_run": False,
                "seen_review_ids": [],
                "seen_review_comment_ids": [],
                "seen_issue_comment_ids": [],
                "seen_check_run_ids": [],
                "pr_state": {},
            }
        },
    }
    state_path.write_text(json.dumps(legacy))

    api = FakeAPI()
    api.issues = [
        _make_issue(number=4, title="Pre-existing issue", user="jcronq"),
        _make_issue(number=5, title="Another", user="jcronq"),
    ]
    api.issue_thread_comments[4] = []
    api.issue_thread_comments[5] = []

    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert notes == [], (
        f"upgrade poll must not emit historical issues, got {[n.name for n in notes]}"
    )
    state = json.loads(state_path.read_text())
    assert state["repos"]["acme/widgets"]["issues_primed"] is True
    assert state["repos"]["acme/widgets"]["issue_state"]["4"] == "open"

    # A *new* issue on the next poll fires normally now that priming is done.
    api.issues = [
        _make_issue(number=4, title="Pre-existing issue", user="jcronq"),
        _make_issue(number=5, title="Another", user="jcronq"),
        _make_issue(number=6, title="Brand new", user="jcronq"),
    ]
    api.issue_thread_comments[6] = []
    gh_watcher.run(mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None)
    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    assert "Brand new" in notes[0].read_text()


def test_trusted_associations_configurable(
    tmp_path: pathlib.Path, state_path: pathlib.Path
) -> None:
    """Operator can override the trust set in alice.config.json."""
    mind = tmp_path / "mind"
    (mind / "config").mkdir(parents=True)
    (mind / "inner" / "notes").mkdir(parents=True)
    (mind / "config" / "alice.config.json").write_text(
        json.dumps(
            {
                "github_watcher": {
                    "enabled": True,
                    "repos": ["acme/widgets"],
                    "trusted_associations": ["OWNER", "CONTRIBUTOR"],
                }
            }
        )
    )

    api = FakeAPI()
    api.issues = []
    gh_watcher.run(mind_dir=mind, state_path=state_path, api=api, log=lambda _: None)

    # CONTRIBUTOR is now trusted; MEMBER is not.
    api.issues = [
        _make_issue(
            number=33,
            title="From a contributor",
            user="ex_pr_author",
            author_association="CONTRIBUTOR",
        ),
        _make_issue(
            number=34,
            title="From a member",
            user="some_member",
            author_association="MEMBER",
        ),
    ]
    api.issue_thread_comments[33] = []
    api.issue_thread_comments[34] = []
    gh_watcher.run(mind_dir=mind, state_path=state_path, api=api, log=lambda _: None)

    notes = list((mind / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    assert "From a contributor" in notes[0].read_text()
