"""Bounded finite abstraction of validation-failure terminal routing.

The model starts after ToolValidationFailed is committed and covers only
DurableModelLoop.handle_validation_failure:

    abstain -> RunAbstained
    fail    -> RunFailed

Request-side validation happens before tool execution. Result-side validation
happens after the request was accepted and the physical tool may have run, so
that prefix remains explicit in the modeled history.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, replace

BOUND = 2

MUTANTS = {
    "abstain_routes_to_run_failed": "AbstainRoutesToRunAbstained",
    "fail_routes_to_run_abstained": "FailRoutesToRunFailed",
    "abstain_reaches_completion": "AbstainNeverCompletes",
    "duplicate_terminal": "SingleTerminal",
    "terminal_not_absorbing": "TerminalIsAbsorbing",
}

TERMINAL_EVENTS = frozenset(("RunAbstained", "RunFailed", "RunCompleted"))
WITNESS_EVENTS = frozenset(
    (
        "ToolValidationFailed(status=abstain,phase=request)",
        "ToolValidationFailed(status=fail,phase=request)",
        "ToolValidationSucceeded(request)",
        "ToolValidationFailed(status=abstain,phase=result)",
        "ToolValidationFailed(status=fail,phase=result)",
        "RunAbstained",
        "RunFailed",
    )
)
EXPECTED_CASES = frozenset(
    (
        ("request", "abstain"),
        ("request", "fail"),
        ("result", "abstain"),
        ("result", "fail"),
    )
)


@dataclass(frozen=True, slots=True)
class State:
    phase: str
    validation_phase: str
    decision: str
    history: tuple[str, ...]


def initial_states() -> tuple[tuple[str, State], ...]:
    states: list[tuple[str, State]] = []
    for validation_phase in ("request", "result"):
        for decision in ("abstain", "fail"):
            validation_failed = (
                f"ToolValidationFailed(status={decision},"
                f"phase={validation_phase})"
            )
            prefix = (
                ()
                if validation_phase == "request"
                else ("ToolValidationSucceeded(request)",)
            )
            states.append(
                (
                    f"case_{validation_phase}_{decision}",
                    State(
                        phase="validation_failed",
                        validation_phase=validation_phase,
                        decision=decision,
                        history=(*prefix, validation_failed),
                    ),
                )
            )
    return tuple(states)


def _terminal_outcome(state: State, phase: str, event: str) -> State:
    return replace(
        state,
        phase=phase,
        history=state.history + (event,),
    )


def successors(
    state: State, *, mutant: str | None
) -> tuple[tuple[str, State], ...]:
    if state.phase == "validation_failed":
        if state.decision == "abstain":
            if mutant == "abstain_routes_to_run_failed":
                return (
                    (
                        "route_abstain_to_failure",
                        _terminal_outcome(state, "failed", "RunFailed"),
                    ),
                )
            if mutant == "abstain_reaches_completion":
                return (
                    (
                        "complete_after_abstention",
                        _terminal_outcome(
                            state, "completed", "RunCompleted"
                        ),
                    ),
                )
            return (
                (
                    "commit_run_abstained",
                    _terminal_outcome(
                        state, "abstained", "RunAbstained"
                    ),
                ),
            )

        if mutant == "fail_routes_to_run_abstained":
            return (
                (
                    "route_failure_to_abstention",
                    _terminal_outcome(
                        state, "abstained", "RunAbstained"
                    ),
                ),
            )
        return (
            (
                "commit_run_failed",
                _terminal_outcome(state, "failed", "RunFailed"),
            ),
        )

    if state.phase in {"abstained", "failed", "completed"}:
        if mutant == "duplicate_terminal":
            return (
                (
                    "commit_duplicate_terminal",
                    replace(
                        state,
                        phase="after_terminal",
                        history=state.history + (state.history[-1],),
                    ),
                ),
            )
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
    if state.decision == "abstain":
        if "RunFailed" in history:
            return "AbstainRoutesToRunAbstained"
        if "RunCompleted" in history:
            return "AbstainNeverCompletes"
    if state.decision == "fail":
        if "RunAbstained" in history or "RunCompleted" in history:
            return "FailRoutesToRunFailed"

    terminal_indexes = [
        index for index, event in enumerate(history) if event in TERMINAL_EVENTS
    ]
    if len(terminal_indexes) > 1:
        return "SingleTerminal"
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
    frozenset[tuple[str, str]],
    int,
]:
    queue = deque()
    seen: set[State] = set()
    cases: set[tuple[str, str]] = set()
    for label, state in initial_states():
        queue.append((state, (label,), 0))
        seen.add(state)
        cases.add((state.validation_phase, state.decision))

    transitions = 0
    witnessed_events: set[str] = set()
    terminal_deadlocks = 0
    while queue:
        state, path, depth = queue.popleft()
        witnessed_events.update(state.history)
        broken = violation(state)
        if broken:
            return (
                len(seen),
                transitions,
                path,
                broken,
                frozenset(witnessed_events),
                frozenset(cases),
                terminal_deadlocks,
            )

        options = () if depth >= BOUND else successors(state, mutant=mutant)
        if not options:
            if state.phase in {"abstained", "failed", "completed"}:
                terminal_deadlocks += 1
            elif state.phase != "after_terminal":
                return (
                    len(seen),
                    transitions,
                    path,
                    "UnexpectedNonTerminalDeadlock",
                    frozenset(witnessed_events),
                    frozenset(cases),
                    terminal_deadlocks,
                )

        for action, candidate in options:
            transitions += 1
            if candidate not in seen:
                seen.add(candidate)
                queue.append((candidate, path + (action,), depth + 1))

    return (
        len(seen),
        transitions,
        None,
        None,
        frozenset(witnessed_events),
        frozenset(cases),
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
        cases,
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
    if missing_events or cases != EXPECTED_CASES:
        print(
            "VACUOUS: "
            f"missing_events={sorted(missing_events)} "
            f"cases={sorted(cases)}"
        )
        return 1
    print(
        f"PASS bound={BOUND} states={states} transitions={transitions} "
        f"cases={len(cases)} "
        f"witnessed_events={','.join(sorted(WITNESS_EVENTS))} "
        f"terminal_deadlocks={terminal_deadlocks}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
