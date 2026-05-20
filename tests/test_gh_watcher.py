"""Tests for ``watchers.github`` — the GitHub repo watcher.

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

from watchers import github as gh_watcher


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
    labels: list[dict] | None = None,
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
        "labels": labels if labels is not None else [],
        # No ``pull_request`` key — pure issue.
    }


class FakeLabeler:
    """Replaces ``_apply_sm_draft_label`` for tests. Records every call
    and honors the same idempotency rule (skip if any sm:* present) so
    tests can assert on the outcome the same way production behaves."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, list[str]]] = []

    def __call__(self, repo: str, number: int, current_labels: list[str]) -> bool:
        self.calls.append((repo, number, list(current_labels)))
        if any(lbl.startswith("sm:") for lbl in current_labels):
            return False
        return True


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

    rc = gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

    assert rc == 0
    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert notes == [], (
        f"first run should not emit notes, got {[n.name for n in notes]}"
    )

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
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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

    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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

    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

    api.pulls = [
        _make_pr(
            number=7,
            title="Refactor",
            state="closed",
            merged_at="2026-04-29T15:00:00Z",
        )
    ]
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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
    assert not state_path.exists() or json.loads(state_path.read_text()).get(
        "repos"
    ) in (None, {}), "no state should be written when there are no repos to poll"


def test_auth_failure_emits_loud_note_and_dedups(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    def boom(_path: str):
        raise gh_watcher.GHCommandError(
            returncode=1,
            stderr="HTTP 401: Bad credentials",
            args=["gh", "api", "x"],
        )

    rc = gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=boom, log=lambda _: None
    )
    assert rc == 1

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    assert "github-watcher-error" in notes[0].read_text()

    # Second pass within the dedup window must not write another note.
    rc2 = gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=boom, log=lambda _: None
    )
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
        len(reloaded["repos"]["acme/widgets"]["seen_review_ids"])
        == gh_watcher.SEEN_ID_CAP
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
    assert gh_watcher.is_trusted_association(
        "collaborator", trusted
    )  # case-insensitive
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
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

    api.issues = [_make_issue(number=11, title="Caught a bug", user="jcronq")]
    api.issue_thread_comments[11] = []
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

    api.issues = [
        _make_issue(
            number=99,
            title="please fix",
            user="randoperson",
            author_association="NONE",
        )
    ]
    api.issue_thread_comments[99] = []
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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

    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

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
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )
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


# ---------------------------------------------------------------------------
# Self-filed marker suppression (issue #226)
# ---------------------------------------------------------------------------


def test_self_filed_marker_suppresses_new_issue_note(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """Issues Speaking files autonomously carry SELF_FILED_MARKER in the
    body; the watcher must NOT emit a ``new_issue`` event for them, so the
    thinking-analysis → attempt-issue-fix loop doesn't redundantly chase
    work Speaking already initiated. Issue state is still tracked so the
    issue isn't re-evaluated forever."""
    api = FakeAPI()
    api.issues = []
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

    api.issues = [
        _make_issue(
            number=226,
            title="Speaking filed this herself",
            user="jcronq",
            body=(
                "Some body content that mentions the fix plan.\n\n"
                f"{gh_watcher.SELF_FILED_MARKER}"
            ),
        )
    ]
    api.issue_thread_comments[226] = []
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert notes == [], (
        f"self-filed issue must not produce a new_issue note, "
        f"got {[n.name for n in notes]}"
    )
    state = json.loads(state_path.read_text())
    assert state["repos"]["acme/widgets"]["issue_state"]["226"] == "open", (
        "self-filed issue state must still be tracked to prevent re-evaluation"
    )


def test_unmarked_jcronq_issue_still_fires_new_issue_note(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """Sanity check that the suppression is marker-gated, not author-gated:
    a manually-filed jcronq issue without the marker still produces a
    note. The marker is the discriminator, not the author."""
    api = FakeAPI()
    api.issues = []
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

    api.issues = [
        _make_issue(
            number=227,
            title="Jason filed this by hand",
            user="jcronq",
            body="No marker, just a regular issue body.",
        )
    ]
    api.issue_thread_comments[227] = []
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    assert "Jason filed this by hand" in notes[0].read_text()


def test_is_self_filed_helper() -> None:
    """Direct unit coverage for the marker check — substring match, case-
    sensitive, missing/non-string bodies are treated as not self-filed."""
    assert gh_watcher._is_self_filed({"body": gh_watcher.SELF_FILED_MARKER})
    assert gh_watcher._is_self_filed(
        {"body": f"some prose\n\n{gh_watcher.SELF_FILED_MARKER}\n"}
    )
    assert not gh_watcher._is_self_filed({"body": "no marker here"})
    # Case-sensitive — the marker is fixed.
    assert not gh_watcher._is_self_filed({"body": "<!-- ALICE-SELF-FILED -->"})
    # Missing / non-string body.
    assert not gh_watcher._is_self_filed({})
    assert not gh_watcher._is_self_filed({"body": None})
    assert not gh_watcher._is_self_filed({"body": 42})


# ---------------------------------------------------------------------------
# sm:draft auto-labeling on new_issue intake
#
# The SM v2 dispatcher's GitHub query filters by ``label:sm:draft,...`` —
# unlabeled trusted-author issues are invisible to it and stall. The
# watcher closes that gap by stamping ``sm:draft`` whenever it emits a
# ``new_issue`` event. These tests pin the load-bearing behaviors:
# fires on fresh issues, idempotent against pre-existing sm:* labels,
# and gated by the same emit condition (no fire for self-filed,
# untrusted authors, first run, or unprimed issue side).
# ---------------------------------------------------------------------------


def test_labeler_fires_on_fresh_trusted_new_issue(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """The structural fix: a trusted-author new issue with no labels gets
    ``sm:draft`` stamped so the SM v2 dispatcher can pick it up."""
    api = FakeAPI()
    api.issues = []
    labeler = FakeLabeler()
    gh_watcher.run(
        mind_dir=mind_dir,
        state_path=state_path,
        api=api,
        log=lambda _: None,
        label_apply=labeler,
    )
    # Prime pass — labeler must not fire because no new_issue event.
    assert labeler.calls == []

    api.issues = [
        _make_issue(number=247, title="Real issue", user="jcronq", labels=[])
    ]
    api.issue_thread_comments[247] = []
    gh_watcher.run(
        mind_dir=mind_dir,
        state_path=state_path,
        api=api,
        log=lambda _: None,
        label_apply=labeler,
    )
    assert labeler.calls == [("acme/widgets", 247, [])]


def test_labeler_skips_when_issue_already_has_sm_label(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """If the issue already carries ``sm:needs_study`` (a human pre-routed
    it past draft), the labeler must not clobber that state. The fake
    labeler honors the same idempotency rule as production so we can
    assert on its return value via the call record."""
    api = FakeAPI()
    api.issues = []
    labeler = FakeLabeler()
    gh_watcher.run(
        mind_dir=mind_dir,
        state_path=state_path,
        api=api,
        log=lambda _: None,
        label_apply=labeler,
    )

    api.issues = [
        _make_issue(
            number=260,
            title="Pre-routed",
            user="jcronq",
            labels=[
                {"id": 1, "name": "sm:needs_study", "color": "ffffff"},
                {"id": 2, "name": "bug", "color": "000000"},
            ],
        )
    ]
    api.issue_thread_comments[260] = []
    gh_watcher.run(
        mind_dir=mind_dir,
        state_path=state_path,
        api=api,
        log=lambda _: None,
        label_apply=labeler,
    )
    # Labeler was invoked (the new_issue path runs it) but bailed early
    # because of the existing sm:* label. The call record carries the
    # extracted label names so we can verify the idempotency input.
    assert len(labeler.calls) == 1
    repo, number, names = labeler.calls[0]
    assert (repo, number) == ("acme/widgets", 260)
    assert "sm:needs_study" in names and "bug" in names

    # Direct check on the real production helper: same input ⇒ no apply.
    assert (
        gh_watcher._apply_sm_draft_label("acme/widgets", 260, names) is False
    )


def test_labeler_gated_by_new_issue_emit_condition(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """The labeler is wired inside the same branch that emits the
    ``new_issue`` event: it must not fire when emission is suppressed by
    first-run priming, the issues_primed gate, untrusted authors, or the
    self-filed marker. Covers all four suppressors in one pass."""
    api = FakeAPI()
    labeler = FakeLabeler()

    # Case 1 — first run: prime pass with two issues already present
    # must not fire the labeler (no new_issue event during priming).
    api.issues = [
        _make_issue(number=1, title="Pre-existing trusted", user="jcronq"),
    ]
    api.issue_thread_comments[1] = []
    gh_watcher.run(
        mind_dir=mind_dir,
        state_path=state_path,
        api=api,
        log=lambda _: None,
        label_apply=labeler,
    )
    assert labeler.calls == [], "first-run prime must not invoke the labeler"

    # Case 2 — untrusted author: a rando-opened issue must not be labeled.
    api.issues = [
        _make_issue(number=1, title="Pre-existing trusted", user="jcronq"),
        _make_issue(
            number=2,
            title="Rando issue",
            user="rando",
            author_association="NONE",
        ),
    ]
    api.issue_thread_comments[2] = []
    gh_watcher.run(
        mind_dir=mind_dir,
        state_path=state_path,
        api=api,
        log=lambda _: None,
        label_apply=labeler,
    )
    assert labeler.calls == [], "untrusted-author new issue must not be labeled"

    # Case 3 — self-filed marker: Speaking-filed issues are suppressed at
    # the emit branch, so the labeler must not run.
    api.issues = [
        _make_issue(number=1, title="Pre-existing trusted", user="jcronq"),
        _make_issue(
            number=2,
            title="Rando issue",
            user="rando",
            author_association="NONE",
        ),
        _make_issue(
            number=3,
            title="Self-filed",
            user="jcronq",
            body=f"plan stuff\n\n{gh_watcher.SELF_FILED_MARKER}",
        ),
    ]
    api.issue_thread_comments[3] = []
    gh_watcher.run(
        mind_dir=mind_dir,
        state_path=state_path,
        api=api,
        log=lambda _: None,
        label_apply=labeler,
    )
    assert labeler.calls == [], "self-filed issue must not be labeled"

    # Sanity: a legitimately fresh trusted non-self-filed issue on the
    # next poll DOES fire — proves the test isn't a no-op.
    api.issues = [
        _make_issue(number=1, title="Pre-existing trusted", user="jcronq"),
        _make_issue(
            number=2,
            title="Rando issue",
            user="rando",
            author_association="NONE",
        ),
        _make_issue(
            number=3,
            title="Self-filed",
            user="jcronq",
            body=f"plan stuff\n\n{gh_watcher.SELF_FILED_MARKER}",
        ),
        _make_issue(number=4, title="Real fresh", user="jcronq"),
    ]
    api.issue_thread_comments[4] = []
    gh_watcher.run(
        mind_dir=mind_dir,
        state_path=state_path,
        api=api,
        log=lambda _: None,
        label_apply=labeler,
    )
    assert labeler.calls == [("acme/widgets", 4, [])]


def test_extract_label_names_helper() -> None:
    """Tolerant of dict entries (gh api shape), bare strings (older
    fixtures), and junk (silently dropped)."""
    assert gh_watcher._extract_label_names(
        [
            {"id": 1, "name": "sm:draft", "color": "ffffff"},
            {"id": 2, "name": "bug"},
            "legacy-string-label",
            {"id": 3},  # no name — skipped
            42,  # not a dict or str — skipped
        ]
    ) == ["sm:draft", "bug", "legacy-string-label"]
    assert gh_watcher._extract_label_names(None) == []
    assert gh_watcher._extract_label_names([]) == []


def test_labeler_failure_does_not_block_new_issue_note(
    mind_dir: pathlib.Path, state_path: pathlib.Path
) -> None:
    """Best-effort contract: if the labeler raises, the watcher must
    still write the ``new_issue`` note. Labeling is a nice-to-have on
    top of the primary thinking → attempt-issue-fix path."""
    api = FakeAPI()
    api.issues = []
    gh_watcher.run(
        mind_dir=mind_dir, state_path=state_path, api=api, log=lambda _: None
    )

    def boom(_repo: str, _number: int, _labels: list[str]) -> bool:
        raise RuntimeError("gh: command not found")

    api.issues = [_make_issue(number=261, title="Has to land", user="jcronq")]
    api.issue_thread_comments[261] = []
    gh_watcher.run(
        mind_dir=mind_dir,
        state_path=state_path,
        api=api,
        log=lambda _: None,
        label_apply=boom,
    )
    notes = list((mind_dir / "inner" / "notes").glob("*.md"))
    assert len(notes) == 1
    assert "Has to land" in notes[0].read_text()
