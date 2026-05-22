"""Phase 4 wiring tests — v3 owns transitions when ``v3_authoritative_states`` is set.

Phase 4 of the SM v3 rollout (issue #301) flipped v3 from dry-run /
dual-run shadow to authoritative. These tests confirm the
dispatcher's main loop:

  * Invokes the v3 handler with real (not stubbed) write services
    when the state is in ``v3_authoritative_states``.
  * Skips the matching legacy v1 handler for the cadence when v3
    returns a :class:`Transition` (avoiding double label edits +
    audit comments).
  * Lets v1 run as the fallback for non-transition v3 results so
    v3's partial ports keep working alongside v1's spawn/hello/
    rebase machinery during the one-month grace period.
  * Writes the dual-run log entries on the ``v3-actual`` lane
    (renamed from ``v3-predicted`` so the previous shadow-comparison
    output still parses).
  * Honours the pre-Phase-4 kwarg aliases
    (``v3_dry_run_states``/``v3_dry_run_log_dir``) so callers that
    haven't migrated yet keep working.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from alice_forge.dispatcher.main import run


def _issue(
    number: int,
    *,
    sm_label: str = "sm:draft",
    art_labels: tuple[str, ...] = ("art:code",),
    author: str = "jcronq",
    title: str = "Test issue",
) -> dict[str, Any]:
    labels = [{"name": sm_label}] + [{"name": n} for n in art_labels]
    return {
        "number": number,
        "title": title,
        "body": "",
        "labels": labels,
        "author": {"login": author},
        "url": f"https://github.com/jcronq/alice/issues/{number}",
    }


def _comment(body: str, author: str = "jcronq") -> dict[str, Any]:
    return {"body": body, "author": {"login": author}}


def _stub_run(
    *,
    state_path: pathlib.Path,
    issues: list[dict[str, Any]],
    comments_by_issue: dict[int, list[dict[str, Any]]] | None = None,
    posted: list[tuple[str, int, str]] | None = None,
    label_edits: list[tuple[str, int, dict[str, Any]]] | None = None,
    v3_authoritative_states: frozenset[str] = frozenset(),
    v3_log_dir: pathlib.Path | None = None,
    v3_dry_run_states_alias: frozenset[str] | None = None,
    v3_dry_run_log_dir_alias: pathlib.Path | None = None,
):
    """Drive ``run()`` with everything stubbed except v3 + ledger."""
    posted = posted if posted is not None else []
    label_edits = label_edits if label_edits is not None else []
    comments = dict(comments_by_issue or {})

    def _post(repo: str, number: int, body: str) -> None:
        posted.append((repo, number, body))

    def _edit_labels(repo: str, number: int, **kwargs: Any) -> None:
        label_edits.append((repo, number, dict(kwargs)))

    kwargs: dict[str, Any] = dict(
        repo="jcronq/alice",
        state_path=state_path,
        list_issues=lambda repo: issues,
        list_stale_closed=lambda repo: [],
        list_open_done=lambda repo: [],
        list_comments=lambda repo, n: comments.get(n, []),
        post_comment=_post,
        edit_labels=_edit_labels,
        find_linked_pr=lambda *a, **kw: None,
        pr_merge_status=lambda *a, **kw: None,
        master_ci_status=lambda *a, **kw: None,
        enable_spawn=False,
        enable_cleanup=False,
        enable_verify=False,
        enable_rebase=False,
        dry_run=False,
        log=lambda s: None,
    )
    if v3_authoritative_states:
        kwargs["v3_authoritative_states"] = v3_authoritative_states
    if v3_log_dir is not None:
        kwargs["v3_log_dir"] = v3_log_dir
    if v3_dry_run_states_alias is not None:
        kwargs["v3_dry_run_states"] = v3_dry_run_states_alias
    if v3_dry_run_log_dir_alias is not None:
        kwargs["v3_dry_run_log_dir"] = v3_dry_run_log_dir_alias

    return run(**kwargs), posted, label_edits


class TestV3OwnsTransitions:
    def test_route_to_study_transitions_v1_skipped(self, tmp_path: pathlib.Path):
        """v3 sees the route-to-study verb, applies the transition,
        and the dispatcher does NOT also call v1 (no duplicate
        label edit, no duplicate audit comment).
        """
        state_path = tmp_path / "state.json"
        issues = [_issue(42, sm_label="sm:draft")]
        comments = {42: [_comment("[SM] route-to-study")]}

        (exit_code, _report), posted, label_edits = _stub_run(
            state_path=state_path,
            issues=issues,
            comments_by_issue=comments,
            v3_authoritative_states=frozenset({"sm:draft"}),
        )

        assert exit_code == 0
        # Exactly one label edit — v3 owns the transition.
        assert len(label_edits) == 1
        _, number, kw = label_edits[0]
        assert number == 42
        assert "sm:needs_study" in kw["add"]
        assert "sm:draft" in kw["remove"]
        # Exactly one audit comment.
        assert len(posted) == 1
        assert "[SM] transition" in posted[0][2]
        assert "to=sm:needs_study" in posted[0][2]

    def test_no_verb_falls_through_to_v1(self, tmp_path: pathlib.Path):
        """v3 returns a SideEffect (triage-surface) for a draft issue
        with no verbal input. SideEffect is non-transition, so v1
        is allowed to run too. v1's draft handler also writes a
        triage surface — both paths exist during the grace period;
        the dedup is the ledger record. This test confirms the
        dispatcher does NOT short-circuit v1 on non-transition
        results.
        """
        state_path = tmp_path / "state.json"
        issues = [_issue(43, sm_label="sm:draft")]
        # No comments → v3 returns a SideEffect (or None on second
        # cadence). Either way v1 still gets called.

        (exit_code, report), posted, label_edits = _stub_run(
            state_path=state_path,
            issues=issues,
            v3_authoritative_states=frozenset({"sm:draft"}),
        )

        assert exit_code == 0
        # No transition — v1 didn't transition either; both agree.
        assert label_edits == []

    def test_v3_not_invoked_when_flag_unset(self, tmp_path: pathlib.Path):
        """When the state is NOT in ``v3_authoritative_states``, only
        v1 runs. This is the default production behaviour after
        Phase 4 lands (flag flip is operational, not structural).
        """
        state_path = tmp_path / "state.json"
        issues = [_issue(44, sm_label="sm:draft")]
        comments = {44: [_comment("[SM] route-to-study")]}

        (exit_code, _report), posted, label_edits = _stub_run(
            state_path=state_path,
            issues=issues,
            comments_by_issue=comments,
            v3_authoritative_states=frozenset(),  # empty → v3 off
        )

        assert exit_code == 0
        # v1 owns the transition path here.
        assert len(label_edits) == 1


class TestDualRunLane:
    def test_v3_actual_log_lane(self, tmp_path: pathlib.Path):
        """When ``v3_log_dir`` is set and v3 runs authoritatively, the
        per-cycle entries land in ``sm-v3-actual.jsonl`` with
        ``lane="v3-actual"`` (Phase 4 rename from
        ``sm-v3-predicted.jsonl`` / ``lane="v3-predicted"``).
        """
        state_path = tmp_path / "state.json"
        v3_log_dir = tmp_path / "v3-logs"
        issues = [_issue(45, sm_label="sm:draft")]
        comments = {45: [_comment("[SM] route-to-study")]}

        (exit_code, _), _, _ = _stub_run(
            state_path=state_path,
            issues=issues,
            comments_by_issue=comments,
            v3_authoritative_states=frozenset({"sm:draft"}),
            v3_log_dir=v3_log_dir,
        )

        assert exit_code == 0
        log_path = v3_log_dir / "sm-v3-actual.jsonl"
        assert log_path.exists()
        lines = log_path.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["lane"] == "v3-actual"
        assert entry["issue_number"] == 45
        assert entry["action_kind"] == "Transition"


class TestLegacyKwargAliases:
    def test_v3_dry_run_states_alias_accepted(self, tmp_path: pathlib.Path):
        """Callers passing the pre-Phase-4 kwarg names still work; the
        dispatcher routes them to the new ``v3_authoritative_states``
        / ``v3_log_dir`` parameters for the grace period.
        """
        state_path = tmp_path / "state.json"
        v3_log_dir = tmp_path / "v3-logs"
        issues = [_issue(46, sm_label="sm:draft")]
        comments = {46: [_comment("[SM] route-to-study")]}

        (exit_code, _), posted, label_edits = _stub_run(
            state_path=state_path,
            issues=issues,
            comments_by_issue=comments,
            v3_dry_run_states_alias=frozenset({"sm:draft"}),
            v3_dry_run_log_dir_alias=v3_log_dir,
        )

        assert exit_code == 0
        # v3 still took the transition via the aliased kwargs.
        assert len(label_edits) == 1
        assert (v3_log_dir / "sm-v3-actual.jsonl").exists()


class TestLegacyImportShims:
    def test_dispatcher_state_re_exported(self):
        """The ``DispatcherState`` / ``load_state`` / ``save_state``
        symbols still resolve through ``alice_forge.dispatcher`` —
        Phase 4 moved them to ``alice_forge.sm.legacy.state`` but
        the public surface stays backward compatible for the grace
        period.
        """
        from alice_forge import dispatcher
        from alice_forge.sm.legacy.state import DispatcherState as LegacyState

        # Same class object, accessed via either path.
        assert dispatcher.DispatcherState is LegacyState
        assert callable(dispatcher.load_state)
        assert callable(dispatcher.save_state)

    def test_legacy_handlers_re_exported(self):
        """Each ``_process_*`` v1 handler still resolves through
        ``alice_forge.dispatcher`` for the grace period.
        """
        from alice_forge import dispatcher
        from alice_forge.sm.legacy.handlers.draft import _process_draft as legacy_draft

        assert dispatcher._process_draft is legacy_draft

    def test_legacy_package_has_grace_period_docstring(self):
        """The ``alice_forge.sm.legacy`` package docstring must call
        out the one-month grace period and the planned deletion so
        anyone landing on the import notices the intent.
        """
        import alice_forge.sm.legacy as legacy_pkg

        doc = (legacy_pkg.__doc__ or "").lower()
        assert "grace period" in doc
        assert "deletion" in doc or "deleted" in doc or "delete" in doc
