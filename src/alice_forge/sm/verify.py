"""State-machine invariant verifier for SM v3.

Runs at dispatcher startup AND in CI. Enforces:

  * **I-1 Completeness:** every non-terminal :class:`SMState` has at
    least one outgoing transition entry in :data:`TRANSITIONS`.
  * **I-2 Closure:** every non-initial :class:`SMState` appears as
    the target of at least one transition.

Violations raise :class:`StateMachineInvariantError` with the full
list of violations (one error message per missing edge) so a single
run surfaces all problems at once — no whack-a-mole.
"""

from __future__ import annotations

from collections.abc import Mapping

from alice_forge.sm.states import SMState, STATE_META
from alice_forge.sm.transitions import (
    EventTransition,
    TransitionTo,
    TRANSITIONS,
)


class StateMachineInvariantError(Exception):
    """Raised when :func:`verify_state_machine` finds a violation.

    ``violations`` is the full list of one-line messages so the
    caller can surface them all rather than fix-one-at-a-time.
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = list(violations)
        super().__init__(
            "SM v3 invariants violated:\n"
            + "\n".join(f"  - {v}" for v in self.violations)
        )


def verify_state_machine(
    transitions: Mapping[SMState, Mapping] = TRANSITIONS,
    initial: SMState = SMState.DRAFT,
) -> None:
    """Verify the v3 transition table satisfies I-1 and I-2.

    Raises :class:`StateMachineInvariantError` listing every
    violation, or returns ``None`` on success.

    Parameterised on the table + initial state so tests can probe
    deliberately-broken tables without touching the production one.
    """
    violations: list[str] = []
    violations.extend(_check_completeness(transitions))
    violations.extend(_check_closure(transitions, initial))
    violations.extend(_check_all_states_in_table(transitions))
    if violations:
        raise StateMachineInvariantError(violations)


def _check_completeness(
    transitions: Mapping[SMState, Mapping],
) -> list[str]:
    """I-1: every non-terminal state has at least one outgoing edge.

    Terminal states (``STATE_META[s].terminal == True``) are exempt;
    they're allowed to have zero outgoing entries. Conditionally-
    terminal states (``sm:blocked``) need at least the unblock
    entry; the transition table enforces this via the explicit
    ``Verbs.UNBLOCK`` row.
    """
    violations: list[str] = []
    for state in SMState:
        meta = STATE_META[state]
        if meta.terminal:
            continue
        edges = transitions.get(state) or {}
        if not edges:
            violations.append(
                f"I-1 completeness: non-terminal state {state.label} has "
                f"no outgoing transitions"
            )
    return violations


def _check_closure(
    transitions: Mapping[SMState, Mapping],
    initial: SMState,
) -> list[str]:
    """I-2: every non-initial state is reachable from some *other* state.

    The initial state (typically :data:`SMState.DRAFT`) is exempt —
    issues enter it from "outside" the dispatcher (a human or a
    watcher labels them ``sm:draft``).

    Self-loops do NOT satisfy closure: a state that only loops to
    itself is unreachable from the rest of the graph. The point of
    I-2 is "can this state be entered from elsewhere," not "does
    this state have any incoming edge at all."
    """
    violations: list[str] = []
    targets: set[SMState] = set()
    for source, edges in transitions.items():
        for verb_or_event, action in edges.items():
            if isinstance(action, TransitionTo):
                if action.target is not source:
                    targets.add(action.target)
            elif isinstance(action, EventTransition):
                if action.target is not source:
                    targets.add(action.target)
            # SelfLoop intentionally not counted — a self-loop is
            # not an incoming edge for closure purposes.
    for state in SMState:
        if state is initial:
            continue
        if state not in targets:
            violations.append(
                f"I-2 closure: state {state.label} has no incoming "
                f"transitions (orphan)"
            )
    return violations


def _check_all_states_in_table(
    transitions: Mapping[SMState, Mapping],
) -> list[str]:
    """Every :class:`SMState` must appear as a key in the table.

    Terminal states are allowed to have an empty mapping
    (``transitions[SMState.REJECTED] == {}``) but the key must be
    present — this catches the "forgot to add the new state to
    TRANSITIONS" class of bug at startup, not at first traffic.
    """
    violations: list[str] = []
    for state in SMState:
        if state not in transitions:
            violations.append(
                f"transition table missing key for state {state.label}"
            )
    return violations
