"""Tests for ``alice_forge.sm.enforcement`` — Phase 3.

Covers:
  * Flag-off path: every call is a pass-through.
  * Per-cycle strike accounting (strike 1, strike 2, strike 3 → block).
  * Strike reset on non-duplicate continue / real transition /
    substantive side-effect.
  * Hash dedup whitespace + case normalization.
  * One-time grace transition (idempotent, TTL-respecting,
    sm:blocked skipped, terminal states skipped).
"""

from __future__ import annotations

import datetime as dt

import pytest

from alice_forge.sm.enforcement import (
    ENFORCEMENT_ENV_VAR,
    GRACE_TRANSITION_BODY,
    STRIKE_LIMIT,
    STRIKES_SIDE_EFFECT,
    StrikeAction,
    apply_enforcement,
    clear_strikes,
    compute_grace_block,
    grace_pass_over_issues,
    is_duplicate_continue,
    is_enforcement_enabled,
    record_continue,
)
from alice_forge.sm.ledger import EmittedLedger
from alice_forge.sm.result import (
    BlockedByTTL,
    Continue,
    NoProgress,
    SideEffect,
    Transition,
)
from alice_forge.sm.states import STATE_META, SMState


NOW = dt.datetime(2026, 5, 22, 14, 0, tzinfo=dt.timezone.utc)
ON_ENV = {ENFORCEMENT_ENV_VAR: "1"}
OFF_ENV: dict[str, str] = {}


# ----------------------------------------------------------------
# Flag
# ----------------------------------------------------------------


class TestIsEnforcementEnabled:
    def test_default_off(self):
        assert is_enforcement_enabled({}) is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
    def test_truthy_values(self, val):
        assert is_enforcement_enabled({ENFORCEMENT_ENV_VAR: val}) is True

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "x"])
    def test_falsy_values(self, val):
        assert is_enforcement_enabled({ENFORCEMENT_ENV_VAR: val}) is False


# ----------------------------------------------------------------
# Hash dedup
# ----------------------------------------------------------------


class TestIsDuplicateContinue:
    def test_empty_ledger_not_duplicate(self):
        ledger = EmittedLedger()
        is_dup, prior = is_duplicate_continue(
            issue_number=1, reason="working on retrieval", ledger=ledger
        )
        assert is_dup is False
        assert prior is None

    def test_exact_match_is_duplicate(self):
        ledger = EmittedLedger()
        record_continue(
            issue_number=1,
            reason="working on retrieval",
            ledger=ledger,
            now=NOW,
        )
        is_dup, prior = is_duplicate_continue(
            issue_number=1, reason="working on retrieval", ledger=ledger
        )
        assert is_dup is True
        assert prior == NOW

    def test_whitespace_normalized(self):
        ledger = EmittedLedger()
        record_continue(
            issue_number=1, reason="working on retrieval", ledger=ledger, now=NOW
        )
        is_dup, _ = is_duplicate_continue(
            issue_number=1,
            reason="  working\ton   retrieval  ",
            ledger=ledger,
        )
        assert is_dup is True

    def test_case_normalized(self):
        ledger = EmittedLedger()
        record_continue(
            issue_number=1, reason="working on retrieval", ledger=ledger, now=NOW
        )
        is_dup, _ = is_duplicate_continue(
            issue_number=1, reason="Working On Retrieval", ledger=ledger
        )
        assert is_dup is True

    def test_different_reason_not_duplicate(self):
        ledger = EmittedLedger()
        record_continue(
            issue_number=1, reason="working on retrieval", ledger=ledger, now=NOW
        )
        is_dup, _ = is_duplicate_continue(
            issue_number=1, reason="hit an integration test fail", ledger=ledger
        )
        assert is_dup is False

    def test_lookback_horizon(self):
        ledger = EmittedLedger()
        # Push the matching continue beyond the lookback window.
        record_continue(issue_number=1, reason="match-me", ledger=ledger, now=NOW)
        for i in range(5):
            record_continue(
                issue_number=1,
                reason=f"distinct-{i}",
                ledger=ledger,
                now=NOW + dt.timedelta(minutes=i + 1),
            )
        is_dup, _ = is_duplicate_continue(
            issue_number=1, reason="match-me", ledger=ledger, lookback=3
        )
        assert is_dup is False

    def test_different_issues_isolated(self):
        ledger = EmittedLedger()
        record_continue(issue_number=1, reason="shared", ledger=ledger, now=NOW)
        is_dup, _ = is_duplicate_continue(
            issue_number=2, reason="shared", ledger=ledger
        )
        assert is_dup is False


# ----------------------------------------------------------------
# apply_enforcement — flag OFF
# ----------------------------------------------------------------


class TestFlagOff:
    def test_pass_through_on_no_progress(self):
        ledger = EmittedLedger()
        result = NoProgress(duplicate_reason="x", duplicate_of_emitted_at="t")
        action = apply_enforcement(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            handler_result=result,
            ledger=ledger,
            now=NOW,
            env=OFF_ENV,
        )
        assert action.kind == "pass-through"
        assert action.side_effect is None
        # No strikes recorded.
        assert ledger.find(1, STRIKES_SIDE_EFFECT) is None

    def test_pass_through_on_duplicate_continue(self):
        ledger = EmittedLedger()
        record_continue(issue_number=1, reason="x", ledger=ledger, now=NOW)
        action = apply_enforcement(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            handler_result=Continue(reason="x"),
            ledger=ledger,
            now=NOW,
            env=OFF_ENV,
        )
        assert action.kind == "pass-through"

    def test_pass_through_on_transition(self):
        ledger = EmittedLedger()
        action = apply_enforcement(
            issue_number=1,
            state=SMState.DRAFT,
            handler_result=Transition(target=SMState.NEEDS_STUDY, reason="r"),
            ledger=ledger,
            now=NOW,
            env=OFF_ENV,
        )
        assert action.kind == "pass-through"


# ----------------------------------------------------------------
# apply_enforcement — flag ON, strike paths
# ----------------------------------------------------------------


class TestStrikeAccounting:
    def _trigger_strike(self, *, strike_count: int) -> tuple[StrikeAction, EmittedLedger]:
        """Helper: run ``strike_count`` consecutive duplicate continues."""
        ledger = EmittedLedger()
        # Seed a prior continue so subsequent identical ones hit dedup.
        record_continue(issue_number=1, reason="dup", ledger=ledger, now=NOW)
        action = None
        for _ in range(strike_count):
            action = apply_enforcement(
                issue_number=1,
                state=SMState.NEEDS_STUDY,
                handler_result=Continue(reason="dup"),
                ledger=ledger,
                now=NOW,
                env=ON_ENV,
            )
        return action, ledger  # type: ignore[return-value]

    def test_strike_1(self):
        action, ledger = self._trigger_strike(strike_count=1)
        assert action.kind == "strike-1"
        assert action.side_effect is not None
        assert "no-progress" in action.side_effect.body
        assert "next iteration" in action.side_effect.body
        # Ledger has count=1.
        rec = ledger.find(1, STRIKES_SIDE_EFFECT)
        assert rec is not None
        assert rec.metadata["count"] == 1

    def test_strike_2(self):
        action, ledger = self._trigger_strike(strike_count=2)
        assert action.kind == "strike-2"
        assert action.side_effect is not None
        assert "second strike" in action.side_effect.body.lower()
        # Active (uncleared) record has count=2. The strike-1
        # record is on the ledger marked ``cleared_by="replaced"``.
        active = [
            r
            for r in ledger.records
            if r.issue_number == 1
            and r.side_effect == STRIKES_SIDE_EFFECT
            and r.cleared_at is None
        ]
        assert len(active) == 1
        assert active[0].metadata["count"] == 2

    def test_strike_3_transitions_to_blocked(self):
        action, ledger = self._trigger_strike(strike_count=3)
        assert action.kind == "strike-3-block"
        assert action.transition is not None
        assert action.transition.target is SMState.BLOCKED
        assert action.transition.metadata["prior_state"] == "sm:needs_study"
        # Audit body present.
        audit = action.transition.metadata.get("audit_body", "")
        assert "from=sm:needs_study" in audit
        assert "to=blocked" in audit
        assert "three no-progress strikes" in audit

    def test_strike_limit_matches_constant(self):
        assert STRIKE_LIMIT == 3

    def test_strikes_explicit_no_progress_variant(self):
        """A handler that returns :class:`NoProgress` directly counts
        as a strike under enforcement."""
        ledger = EmittedLedger()
        action = apply_enforcement(
            issue_number=2,
            state=SMState.REVIEWING,
            handler_result=NoProgress(
                duplicate_reason="ci pending",
                duplicate_of_emitted_at="2026-05-22T13:00:00+00:00",
            ),
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert action.kind == "strike-1"


class TestStrikeReset:
    def _seeded_strike_ledger(self) -> EmittedLedger:
        """Two-strike state for #1."""
        ledger = EmittedLedger()
        record_continue(issue_number=1, reason="dup", ledger=ledger, now=NOW)
        apply_enforcement(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            handler_result=Continue(reason="dup"),
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        apply_enforcement(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            handler_result=Continue(reason="dup"),
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        active = [
            r
            for r in ledger.records
            if r.issue_number == 1
            and r.side_effect == STRIKES_SIDE_EFFECT
            and r.cleared_at is None
        ]
        assert len(active) == 1 and active[0].metadata["count"] == 2
        return ledger

    def _active_strike_record(self, ledger: EmittedLedger):
        active = [
            r
            for r in ledger.records
            if r.issue_number == 1
            and r.side_effect == STRIKES_SIDE_EFFECT
            and r.cleared_at is None
        ]
        return active[0] if active else None

    def _has_active_strike(self, ledger: EmittedLedger) -> bool:
        return self._active_strike_record(ledger) is not None

    def test_real_transition_resets(self):
        ledger = self._seeded_strike_ledger()
        apply_enforcement(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            handler_result=Transition(
                target=SMState.SELECTED, reason="study complete"
            ),
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert self._has_active_strike(ledger) is False

    def test_fresh_continue_resets(self):
        ledger = self._seeded_strike_ledger()
        apply_enforcement(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            handler_result=Continue(reason="actually-new-information"),
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert self._has_active_strike(ledger) is False

    def test_side_effect_resets(self):
        ledger = self._seeded_strike_ledger()
        apply_enforcement(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            handler_result=SideEffect(
                name="study-hint",
                body="[SM] study-hint ...",
                ttl_seconds=None,
            ),
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert self._has_active_strike(ledger) is False

    def test_blocked_by_ttl_resets(self):
        ledger = self._seeded_strike_ledger()
        apply_enforcement(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            handler_result=BlockedByTTL(state_ttl_seconds=86400),
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert self._has_active_strike(ledger) is False

    def test_clear_strikes_function(self):
        ledger = self._seeded_strike_ledger()
        cleared = clear_strikes(1, ledger, NOW)
        assert cleared is True
        assert self._has_active_strike(ledger) is False

    def test_clear_strikes_no_record(self):
        ledger = EmittedLedger()
        assert clear_strikes(99, ledger, NOW) is False


class TestStrikeBoundaryCases:
    def test_none_result_does_not_strike(self):
        """Handler returning None (the v1 silent-noop pattern) does
        NOT trigger a strike. The TTL path is the right escalation
        for 'agent produced nothing this cycle'; strikes are for the
        specific failure mode 'agent produced the SAME comment twice.'
        """
        ledger = EmittedLedger()
        action = apply_enforcement(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            handler_result=None,
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert action.kind == "pass-through"

    def test_fresh_continue_records_and_passes_through(self):
        ledger = EmittedLedger()
        action = apply_enforcement(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            handler_result=Continue(reason="brand new info"),
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert action.kind == "pass-through"
        rec = ledger.find(1, "continue")
        assert rec is not None
        assert rec.metadata["reason"] == "brand new info"


# ----------------------------------------------------------------
# Grace transition
# ----------------------------------------------------------------


class TestGraceTransition:
    def _ttl(self, state: SMState) -> int:
        ttl = STATE_META[state].default_continue_ttl_seconds
        assert ttl is not None
        return ttl

    def test_flag_off_returns_none(self):
        ledger = EmittedLedger()
        last_activity = NOW - dt.timedelta(days=30)
        t = compute_grace_block(
            issue_number=1,
            state=SMState.NEEDS_STUDY,
            issue_last_activity=last_activity,
            ledger=ledger,
            now=NOW,
            env=OFF_ENV,
        )
        assert t is None

    def test_fires_when_past_ttl(self):
        ledger = EmittedLedger()
        ttl = self._ttl(SMState.SELECTED)
        last_activity = NOW - dt.timedelta(seconds=ttl + 60)
        t = compute_grace_block(
            issue_number=1,
            state=SMState.SELECTED,
            issue_last_activity=last_activity,
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert t is not None
        assert t.target is SMState.BLOCKED
        assert t.metadata["prior_state"] == "sm:selected"
        assert t.metadata["audit_body"] == GRACE_TRANSITION_BODY

    def test_no_op_within_ttl(self):
        ledger = EmittedLedger()
        ttl = self._ttl(SMState.SELECTED)
        last_activity = NOW - dt.timedelta(seconds=ttl - 60)
        t = compute_grace_block(
            issue_number=1,
            state=SMState.SELECTED,
            issue_last_activity=last_activity,
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert t is None

    def test_terminal_states_skipped(self):
        ledger = EmittedLedger()
        for state in (SMState.DONE, SMState.REJECTED):
            t = compute_grace_block(
                issue_number=1,
                state=state,
                issue_last_activity=NOW - dt.timedelta(days=365),
                ledger=ledger,
                now=NOW,
                env=ON_ENV,
            )
            assert t is None

    def test_sm_blocked_skipped(self):
        ledger = EmittedLedger()
        t = compute_grace_block(
            issue_number=1,
            state=SMState.BLOCKED,
            issue_last_activity=NOW - dt.timedelta(days=365),
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert t is None

    def test_idempotent_after_firing(self):
        ledger = EmittedLedger()
        ttl = self._ttl(SMState.SELECTED)
        last_activity = NOW - dt.timedelta(seconds=ttl + 60)
        t1 = compute_grace_block(
            issue_number=1,
            state=SMState.SELECTED,
            issue_last_activity=last_activity,
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert t1 is not None
        # Second call: ledger already has the grace record.
        t2 = compute_grace_block(
            issue_number=1,
            state=SMState.SELECTED,
            issue_last_activity=last_activity,
            ledger=ledger,
            now=NOW + dt.timedelta(seconds=60),
            env=ON_ENV,
        )
        assert t2 is None

    def test_recent_continue_blocks_grace(self):
        """An issue that's emitting healthy continues is exempt
        from the grace block even if updated_at is old."""
        ledger = EmittedLedger()
        ttl = self._ttl(SMState.SELECTED)
        # Last activity (e.g. last issue update) is ancient ...
        last_activity = NOW - dt.timedelta(seconds=ttl + 60)
        # ... but a recent continue is on the ledger.
        record_continue(
            issue_number=1,
            reason="fresh",
            ledger=ledger,
            now=NOW - dt.timedelta(seconds=ttl // 2),
        )
        t = compute_grace_block(
            issue_number=1,
            state=SMState.SELECTED,
            issue_last_activity=last_activity,
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert t is None


class TestGracePassOverIssues:
    def test_flag_off_short_circuits(self):
        ledger = EmittedLedger()
        out = grace_pass_over_issues(
            issues=[
                (
                    1,
                    SMState.SELECTED,
                    NOW - dt.timedelta(days=10),
                )
            ],
            ledger=ledger,
            now=NOW,
            env=OFF_ENV,
        )
        assert out == []

    def test_emits_one_transition_per_eligible_issue(self):
        ledger = EmittedLedger()
        ttl_sel = STATE_META[SMState.SELECTED].default_continue_ttl_seconds
        assert ttl_sel is not None
        old = NOW - dt.timedelta(seconds=ttl_sel + 60)
        fresh = NOW - dt.timedelta(seconds=60)
        out = grace_pass_over_issues(
            issues=[
                (1, SMState.SELECTED, old),
                (2, SMState.SELECTED, fresh),  # within TTL — skipped
                (3, SMState.DONE, old),  # terminal — skipped
                (4, SMState.NEEDS_STUDY, NOW - dt.timedelta(days=10)),
            ],
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        emitted_issues = {n for n, _ in out}
        assert emitted_issues == {1, 4}

    def test_subsequent_pass_is_no_op(self):
        ledger = EmittedLedger()
        ttl_sel = STATE_META[SMState.SELECTED].default_continue_ttl_seconds
        assert ttl_sel is not None
        old = NOW - dt.timedelta(seconds=ttl_sel + 60)
        first = grace_pass_over_issues(
            issues=[(1, SMState.SELECTED, old)],
            ledger=ledger,
            now=NOW,
            env=ON_ENV,
        )
        assert len(first) == 1
        second = grace_pass_over_issues(
            issues=[(1, SMState.SELECTED, old)],
            ledger=ledger,
            now=NOW + dt.timedelta(seconds=300),
            env=ON_ENV,
        )
        assert second == []
