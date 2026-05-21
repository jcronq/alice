"""Tests for ``alice_forge.sm.verify``."""

from __future__ import annotations

import pytest

from alice_forge.sm.states import SMState
from alice_forge.sm.transitions import (
    EventTransition,
    TransitionTo,
    TRANSITIONS,
)
from alice_forge.sm.verify import (
    StateMachineInvariantError,
    verify_state_machine,
)


class TestProductionTable:
    def test_production_transitions_pass(self):
        # The real table must satisfy I-1 + I-2; this is the gate
        # that runs at dispatcher startup and in CI.
        verify_state_machine(TRANSITIONS, initial=SMState.DRAFT)


class TestCompleteness:
    def test_non_terminal_with_no_edges_fails_i1(self):
        broken = dict(TRANSITIONS)
        # NEEDS_STUDY is non-terminal; emptying its edges violates I-1.
        broken[SMState.NEEDS_STUDY] = {}
        with pytest.raises(StateMachineInvariantError) as exc:
            verify_state_machine(broken, initial=SMState.DRAFT)
        msg = str(exc.value)
        assert "I-1" in msg
        assert "sm:needs_study" in msg

    def test_terminal_with_no_edges_passes(self):
        # REJECTED already has {} in the production table — must pass.
        verify_state_machine(TRANSITIONS, initial=SMState.DRAFT)


class TestClosure:
    def test_orphan_state_fails_i2(self):
        # Drop every edge that targets DESIGNED → DESIGNED becomes
        # an orphan, violating I-2.
        broken = {
            state: {
                verb_or_event: action
                for verb_or_event, action in edges.items()
                if not (
                    isinstance(action, (TransitionTo, EventTransition))
                    and action.target is SMState.DESIGNED
                )
            }
            for state, edges in TRANSITIONS.items()
        }
        with pytest.raises(StateMachineInvariantError) as exc:
            verify_state_machine(broken, initial=SMState.DRAFT)
        msg = str(exc.value)
        assert "I-2" in msg
        assert "sm:designed" in msg

    def test_initial_state_exempt_from_closure(self):
        # DRAFT has no incoming transitions (it's the initial state)
        # — that's allowed.
        verify_state_machine(TRANSITIONS, initial=SMState.DRAFT)


class TestMissingTableKey:
    def test_missing_state_in_table_fails(self):
        # Drop DESIGNING from the table entirely; the missing-key
        # check should catch it.
        broken = {k: v for k, v in TRANSITIONS.items() if k is not SMState.DESIGNING}
        with pytest.raises(StateMachineInvariantError) as exc:
            verify_state_machine(broken, initial=SMState.DRAFT)
        msg = str(exc.value)
        assert "sm:designing" in msg
        assert "missing key" in msg


class TestMultipleViolationsSurfaced:
    def test_multiple_violations_listed_at_once(self):
        # Break I-1 AND I-2 in the same table; both should appear in
        # the raised exception's violations list.
        broken = {state: dict(edges) for state, edges in TRANSITIONS.items()}
        broken[SMState.BUILDING] = {}  # I-1 violation
        # Strip incoming edges to REVIEWING → I-2 violation
        for state, edges in broken.items():
            for k in list(edges):
                action = edges[k]
                if isinstance(action, (TransitionTo, EventTransition)):
                    if action.target is SMState.REVIEWING:
                        del edges[k]
        with pytest.raises(StateMachineInvariantError) as exc:
            verify_state_machine(broken, initial=SMState.DRAFT)
        violations = exc.value.violations
        assert any("I-1" in v for v in violations)
        assert any("I-2" in v for v in violations)
