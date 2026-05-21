"""Tests for ``alice_forge.sm.transitions``."""

from __future__ import annotations

import pytest

from alice_forge.sm.states import SMState
from alice_forge.sm.transitions import (
    EventTransition,
    SelfLoop,
    TransitionTo,
    TRANSITIONS,
    Verbs,
)


class TestTransitionsTable:
    def test_every_state_keyed(self):
        for state in SMState:
            assert state in TRANSITIONS, f"missing key for {state.label}"

    def test_only_three_action_kinds(self):
        for state, edges in TRANSITIONS.items():
            for verb_or_event, action in edges.items():
                assert isinstance(
                    action, (TransitionTo, SelfLoop, EventTransition)
                ), (
                    f"unexpected action type in {state.label}[{verb_or_event!r}]: "
                    f"{type(action).__name__}"
                )

    def test_self_loops_only_on_continue_verb(self):
        for state, edges in TRANSITIONS.items():
            for verb_or_event, action in edges.items():
                if isinstance(action, SelfLoop):
                    assert verb_or_event is Verbs.CONTINUE, (
                        f"SelfLoop allowed only on Verbs.CONTINUE; "
                        f"{state.label} has SelfLoop on {verb_or_event!r}"
                    )

    def test_continue_verb_present_in_every_non_terminal_state(self):
        # I-3 enforcement at the table level: every non-terminal
        # state must accept the universal continue verb.
        from alice_forge.sm.states import STATE_META

        for state in SMState:
            if STATE_META[state].terminal:
                continue
            edges = TRANSITIONS[state]
            # BLOCKED uses UNBLOCK as its outgoing verb, not CONTINUE
            # — blocked issues don't tick a TTL; they wait for an
            # explicit unblock.
            if state is SMState.BLOCKED:
                assert Verbs.UNBLOCK in edges
                continue
            assert Verbs.CONTINUE in edges, (
                f"non-terminal state {state.label} missing CONTINUE verb"
            )

    def test_no_self_loop_on_terminal(self):
        from alice_forge.sm.states import STATE_META

        for state, edges in TRANSITIONS.items():
            if not STATE_META[state].terminal:
                continue
            for verb_or_event, action in edges.items():
                if isinstance(action, SelfLoop):
                    pytest.fail(
                        f"terminal state {state.label} has SelfLoop "
                        f"on {verb_or_event!r}"
                    )

    def test_transition_targets_are_valid_states(self):
        for state, edges in TRANSITIONS.items():
            for verb_or_event, action in edges.items():
                if isinstance(action, (TransitionTo, EventTransition)):
                    assert isinstance(action.target, SMState)


class TestVerbsEnum:
    def test_continue_is_single_universal_verb(self):
        # No per-state continue-* verbs — the v3 simplification.
        per_state = [
            v for v in Verbs if v.value.startswith("continue-")
        ]
        assert per_state == [], (
            f"v3 should have one [SM] continue verb, found "
            f"per-state variants: {[v.value for v in per_state]}"
        )

    def test_route_to_study_verb_present(self):
        assert Verbs.ROUTE_TO_STUDY.value == "route-to-study"

    def test_study_complete_verb_present(self):
        assert Verbs.STUDY_COMPLETE.value == "study-complete"

    def test_design_approved_verb_present(self):
        assert Verbs.DESIGN_APPROVED.value == "design-approved"

    def test_unblock_verb_present(self):
        assert Verbs.UNBLOCK.value == "unblock"
