"""Bounded finite abstraction of the v0.4 final-answer completion gate.

The model covers only the ordering in `DurableModelLoop.interpret_model`:

    invariant -> goal -> GoalSatisfied -> AnswerProduced -> RunCompleted

It is a standalone pure-Python bounded checker, not a refinement proof for the
runtime and not an extension of the FASM/setdb models. The two configuration
axes are independent: an invariant and a goal may each be absent or present,
and each configured predicate may pass or fail.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, replace

BOUND = 3

MUTANTS = {
    "invariant_allows_completion": "InvariantViolationNeverCompletes",
    "goal_allows_completion": "UnsatisfiedGoalNeverCompletes",
    "completion_before_goal": "GoalSatisfiedPrecedesCompletion",
    "goal_checked_before_invariant": "InvariantIsCheckedBeforeGoal",
    "terminal_not_absorbing": "TerminalIsAbsorbing",
}

TERMINAL_EVENTS = frozenset(("RunCompleted", "RunFailed"))
WITNESS_EVENTS = frozenset(
    (
        "WorkflowInvariantViolated",
        "GoalNotSatisfied",
        "GoalSatisfied",
        "RunCompleted",
        "RunFailed",
    )
)


@dataclass(frozen=True, slots=True)
class State:
    phase: str = "ready"
    history: tuple[str, ...] = ("ModelCallSucceeded(answer)",)
    invariant_configured: bool = False
    invariant_holds: bool = True
    goal_configured: bool = False
    goal_holds: bool = True
    # Ghost fact used only by the priority mutant. Outcome states otherwise
    # discard predicate inputs that are irrelevant after the gate decision.
    invariant_bypassed: bool = False


def initial_states() -> tuple[tuple[str, State], ...]:
    states: list[tuple[str, State]] = []
    for invariant_configured in (False, True):
        invariant_values = (True,) if not invariant_configured else (False, True)
        for goal_configured in (False, True):
            goal_values = (True,) if not goal_configured else (False, True)
            for invariant_holds in invariant_values:
                for goal_holds in goal_values:
                    label = (
                        "case_"
                        f"invariant_{'off' if not invariant_configured else invariant_holds}_"
                        f"goal_{'off' if not goal_configured else goal_holds}"
                    )
                    states.append(
                        (
                            label,
                            State(
                                invariant_configured=invariant_configured,
                                invariant_holds=invariant_holds,
                                goal_configured=goal_configured,
                                goal_holds=goal_holds,
                            ),
                        )
                    )
    return tuple(states)


def _outcome(
    phase: str,
    *events: str,
    terminal: bool = False,
    invariant_bypassed: bool = False,
) -> State:
    return State(
        phase=phase,
        history=("ModelCallSucceeded(answer)", *events),
        invariant_bypassed=invariant_bypassed,
    )


def successors(
    state: State, *, mutant: str | None
) -> tuple[tuple[str, State], ...]:
    if len(state.history) >= BOUND + 2:
        return ()

    if state.phase == "ready":
        invariant_failed = (
            state.invariant_configured and not state.invariant_holds
        )
        goal_failed = state.goal_configured and not state.goal_holds

        if (
            mutant == "goal_checked_before_invariant"
            and invariant_failed
            and goal_failed
        ):
            return (
                (
                    "reject_goal_before_invariant",
                    _outcome(
                        "goal_failed",
                        "GoalNotSatisfied",
                        invariant_bypassed=True,
                    ),
                ),
            )

        if invariant_failed:
            if mutant == "invariant_allows_completion":
                return (
                    (
                        "complete_despite_invariant",
                        _outcome(
                            "completed",
                            "WorkflowInvariantViolated",
                            "AnswerProduced",
                            "RunCompleted",
                        ),
                    ),
                )
            return (
                (
                    "reject_invariant",
                    _outcome(
                        "invariant_failed", "WorkflowInvariantViolated"
                    ),
                ),
            )

        if goal_failed:
            if mutant == "goal_allows_completion":
                return (
                    (
                        "complete_despite_goal",
                        _outcome(
                            "completed",
                            "GoalNotSatisfied",
                            "AnswerProduced",
                            "RunCompleted",
                        ),
                    ),
                )
            return (
                (
                    "reject_goal",
                    _outcome("goal_failed", "GoalNotSatisfied"),
                ),
            )

        if state.goal_configured:
            events = (
                ("AnswerProduced", "RunCompleted", "GoalSatisfied")
                if mutant == "completion_before_goal"
                else ("GoalSatisfied", "AnswerProduced", "RunCompleted")
            )
            return (("complete_with_goal", _outcome("completed", *events)),)
        return (
            (
                "complete_without_goal",
                _outcome("completed", "AnswerProduced", "RunCompleted"),
            ),
        )

    if state.phase in {"invariant_failed", "goal_failed"}:
        return (
            (
                "commit_run_failed",
                _outcome(
                    "failed",
                    state.history[1],
                    "RunFailed",
                ),
            ),
        )

    if state.phase in {"completed", "failed"}:
        if mutant == "terminal_not_absorbing":
            return (
                (
                    "event_after_terminal",
                    replace(
                        state,
                        phase="after_terminal",
                        history=state.history + ("ToolCallRequested",),
                    ),
                ),
            )
        return ()

    return ()


def violation(state: State) -> str | None:
    history = state.history
    if state.invariant_bypassed:
        return "InvariantIsCheckedBeforeGoal"
    if "WorkflowInvariantViolated" in history and "RunCompleted" in history:
        return "InvariantViolationNeverCompletes"
    if "GoalNotSatisfied" in history and "RunCompleted" in history:
        return "UnsatisfiedGoalNeverCompletes"
    if "GoalSatisfied" in history and "RunCompleted" in history:
        if history.index("GoalSatisfied") > history.index("RunCompleted"):
            return "GoalSatisfiedPrecedesCompletion"
    terminal_indexes = [
        index for index, event in enumerate(history) if event in TERMINAL_EVENTS
    ]
    if terminal_indexes and terminal_indexes[0] != len(history) - 1:
        return "TerminalIsAbsorbing"
    return None


def explore(
    *, mutant: str | None
) -> tuple[
    int,
    int,
    tuple[str, ...] | None,
    str | None,
    frozenset[str],
    frozenset[tuple[bool, bool]],
    int,
]:
    queue = deque()
    seen: set[State] = set()
    configured_cases: set[tuple[bool, bool]] = set()
    for label, state in initial_states():
        queue.append((state, (label,)))
        seen.add(state)
        configured_cases.add(
            (state.invariant_configured, state.goal_configured)
        )

    transitions = 0
    witnessed_events: set[str] = set()
    terminal_deadlocks = 0
    while queue:
        state, path = queue.popleft()
        witnessed_events.update(state.history)
        broken = violation(state)
        if broken:
            return (
                len(seen),
                transitions,
                path,
                broken,
                frozenset(witnessed_events),
                frozenset(configured_cases),
                terminal_deadlocks,
            )
        options = successors(state, mutant=mutant)
        if not options:
            if state.phase in {"completed", "failed"}:
                terminal_deadlocks += 1
            elif state.phase != "after_terminal":
                return (
                    len(seen),
                    transitions,
                    path,
                    "UnexpectedNonTerminalDeadlock",
                    frozenset(witnessed_events),
                    frozenset(configured_cases),
                    terminal_deadlocks,
                )
        for action, candidate in options:
            transitions += 1
            if candidate not in seen:
                seen.add(candidate)
                queue.append((candidate, path + (action,)))

    return (
        len(seen),
        transitions,
        None,
        None,
        frozenset(witnessed_events),
        frozenset(configured_cases),
        terminal_deadlocks,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutant", choices=tuple(MUTANTS))
    args = parser.parse_args()
    (
        states,
        transitions,
        path,
        broken,
        witnessed_events,
        configured_cases,
        terminal_deadlocks,
    ) = explore(mutant=args.mutant)

    if args.mutant:
        expected = MUTANTS[args.mutant]
        if broken != expected:
            print(
                f"MUTANT_SURVIVED mutant={args.mutant} states={states} "
                f"transitions={transitions}"
            )
            return 1
        print(
            f"MUTANT_KILLED mutant={args.mutant} property={broken} "
            f"bound={BOUND} states={states} transitions={transitions} "
            f"path={' -> '.join(path or ())}"
        )
        return 0

    if broken:
        print(f"FAIL property={broken} path={' -> '.join(path or ())}")
        return 1
    missing_events = WITNESS_EVENTS - witnessed_events
    expected_cases = frozenset(
        ((False, False), (False, True), (True, False), (True, True))
    )
    if missing_events or configured_cases != expected_cases:
        print(
            "VACUOUS: "
            f"missing_events={sorted(missing_events)} "
            f"configured_cases={sorted(configured_cases)}"
        )
        return 1
    print(
        f"PASS bound={BOUND} states={states} transitions={transitions} "
        f"configured_cases={len(configured_cases)} "
        f"witnessed_events={','.join(sorted(WITNESS_EVENTS))} "
        f"terminal_deadlocks={terminal_deadlocks}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
