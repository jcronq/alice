"""Tests for ``alice_forge.sm.states``."""

from __future__ import annotations


from alice_forge.sm.states import STATE_META, SMState


class TestSMStateEnum:
    def test_all_twelve_states_present(self):
        names = {s.name for s in SMState}
        assert names == {
            "DRAFT",
            "NEEDS_STUDY",
            "SELECTED",
            "DESIGNING",
            "DESIGN_REVIEW",
            "DESIGNED",
            "COMPACTING",
            "BUILDING",
            "REVIEWING",
            "DONE",
            "REJECTED",
            "BLOCKED",
        }

    def test_values_match_v1_labels(self):
        # Label values must match v1's strings exactly so the
        # migration is a typed wrapper, not a rename.
        assert SMState.DRAFT.value == "sm:draft"
        assert SMState.NEEDS_STUDY.value == "sm:needs_study"
        assert SMState.SELECTED.value == "sm:selected"
        assert SMState.DESIGN_REVIEW.value == "sm:design_review"
        assert SMState.DONE.value == "sm:done"
        assert SMState.BLOCKED.value == "sm:blocked"

    def test_from_label_round_trip(self):
        for state in SMState:
            assert SMState.from_label(state.value) is state

    def test_from_label_unknown_returns_none(self):
        assert SMState.from_label("sm:totally-fake") is None
        assert SMState.from_label("not-an-sm-label") is None
        assert SMState.from_label("") is None

    def test_label_property_matches_value(self):
        for state in SMState:
            assert state.label == state.value


class TestStateMeta:
    def test_every_state_has_meta_entry(self):
        for state in SMState:
            assert state in STATE_META

    def test_terminal_states_have_no_ttl(self):
        for state in SMState:
            meta = STATE_META[state]
            if meta.terminal:
                assert meta.default_continue_ttl_seconds is None

    def test_non_terminal_states_have_ttl_or_blocked(self):
        # Non-terminal states need a TTL — except BLOCKED, which
        # has no TTL because escape requires an explicit unblock
        # comment (no time-based escalation from blocked).
        for state in SMState:
            meta = STATE_META[state]
            if meta.terminal:
                continue
            if state is SMState.BLOCKED:
                assert meta.default_continue_ttl_seconds is None
                continue
            assert isinstance(meta.default_continue_ttl_seconds, int)
            assert meta.default_continue_ttl_seconds > 0

    def test_only_done_and_rejected_are_terminal(self):
        terminals = {s for s in SMState if STATE_META[s].terminal}
        assert terminals == {SMState.DONE, SMState.REJECTED}

    def test_role_strings_are_one_liners(self):
        for state in SMState:
            role = STATE_META[state].role
            assert isinstance(role, str) and role.strip() == role
            assert "\n" not in role
            assert len(role) < 120
