from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, replace

CHILDREN = 3
BOUND = 12
MUTANTS = {
    "early_next": "AtMostOneActiveChild",
    "restart_completed": "CompletedChildNeverRestarts",
    "skip_index": "NoSkippedChild",
    "wrong_run": "OutcomeMatchesCurrentChild",
    "replace_run_id": "ChildRunIdentityImmutable",
    "advance_failure": "FailureIsFailFast",
    "early_parent_complete": "ParentCompleteImpliesAllChildrenComplete",
    "double_terminal": "AtMostOneTerminalOutcomePerChild",
    "wrong_output": "OutputBelongsToCompletedChild",
    "duplicate_start": "ChildRunIdentityImmutable",
}


@dataclass(frozen=True, slots=True)
class State:
    current: int = 0
    statuses: tuple[int, ...] = (0, 0, 0)  # pending/active/completed/failed
    run_ids: tuple[int, ...] = (0, 0, 0)
    starts: tuple[int, ...] = (0, 0, 0)
    outcomes: tuple[int, ...] = (0, 0, 0)
    outcome_run_ids: tuple[int, ...] = (0, 0, 0)
    output_owners: tuple[int, ...] = (0, 0, 0)
    parent: int = 1  # active/completed/failed
    steps: int = 0


def changed(values: tuple[int, ...], index: int, value: int) -> tuple[int, ...]:
    return values[:index] + (value,) + values[index + 1 :]


def successors(state: State, mutant: str | None):
    completed = next((i for i, status in enumerate(state.statuses) if status == 2), None)
    if mutant == "restart_completed" and completed is not None:
        yield "restart_completed", replace(
            state,
            statuses=changed(state.statuses, completed, 1),
            starts=changed(state.starts, completed, state.starts[completed] + 1),
            steps=state.steps + 1,
        )
        return
    if mutant == "double_terminal" and completed is not None:
        yield "second_terminal", replace(
            state,
            outcomes=changed(state.outcomes, completed, state.outcomes[completed] + 1),
            steps=state.steps + 1,
        )
        return
    if state.steps >= BOUND or state.parent != 1:
        if mutant == "replace_run_id" and any(state.run_ids):
            index = next(i for i, run_id in enumerate(state.run_ids) if run_id)
            yield "restart_replace_id", replace(
                state,
                run_ids=changed(state.run_ids, index, 99),
                steps=state.steps + 1,
            )
        else:
            yield "restart", state
        return

    index = state.current
    status = state.statuses[index]
    if status == 0:
        target = min(index + 1, CHILDREN - 1) if mutant == "skip_index" else index
        yield "start", replace(
            state,
            current=target,
            statuses=changed(state.statuses, target, 1),
            run_ids=changed(state.run_ids, target, target + 1),
            starts=changed(state.starts, target, state.starts[target] + 1),
            steps=state.steps + 1,
        )
        return

    if status == 1:
        run_id = state.run_ids[index]
        if mutant == "duplicate_start":
            yield "duplicate_start", replace(
                state,
                run_ids=changed(state.run_ids, index, run_id + 10),
                steps=state.steps + 1,
            )
            return
        yield "duplicate_start_ignored", state
        statuses = changed(state.statuses, index, 2)
        next_index = index + 1 if index < CHILDREN - 1 else index
        if mutant == "early_next" and index < CHILDREN - 1:
            statuses = changed(statuses, index, 1)
            statuses = changed(statuses, next_index, 1)
        parent = 2 if index == CHILDREN - 1 else 1
        if mutant == "early_parent_complete":
            parent = 2
        yield "complete", replace(
            state,
            current=next_index,
            statuses=statuses,
            outcomes=changed(state.outcomes, index, state.outcomes[index] + 1),
            outcome_run_ids=changed(
                state.outcome_run_ids,
                index,
                run_id + 10 if mutant == "wrong_run" else run_id,
            ),
            output_owners=changed(
                state.output_owners,
                index,
                index + 2 if mutant == "wrong_output" else index + 1,
            ),
            parent=parent,
            steps=state.steps + 1,
        )
        yield "fail", replace(
            state,
            current=min(index + 1, CHILDREN - 1) if mutant == "advance_failure" else index,
            statuses=changed(state.statuses, index, 3),
            outcomes=changed(state.outcomes, index, state.outcomes[index] + 1),
            outcome_run_ids=changed(state.outcome_run_ids, index, run_id),
            parent=1 if mutant == "advance_failure" else 3,
            steps=state.steps + 1,
        )
        return
    yield "restart", state


def violation(state: State) -> str | None:
    if sum(status == 1 for status in state.statuses) > 1:
        return "AtMostOneActiveChild"
    if any(starts > 1 for starts in state.starts):
        return "CompletedChildNeverRestarts"
    if any(
        state.statuses[index] == 0 and any(s != 0 for s in state.statuses[index + 1 :])
        for index in range(CHILDREN)
    ):
        return "NoSkippedChild"
    if any(outcomes > 1 for outcomes in state.outcomes):
        return "AtMostOneTerminalOutcomePerChild"
    if any(run_id not in (0, index + 1) for index, run_id in enumerate(state.run_ids)):
        return "ChildRunIdentityImmutable"
    if state.parent == 2 and any(status != 2 for status in state.statuses):
        return "ParentCompleteImpliesAllChildrenComplete"
    if any(status == 3 for status in state.statuses) and state.parent != 3:
        return "FailureIsFailFast"
    if any(
        state.outcomes[index] and state.outcome_run_ids[index] != state.run_ids[index]
        for index in range(CHILDREN)
    ):
        return "OutcomeMatchesCurrentChild"
    if any(owner not in (0, index + 1) for index, owner in enumerate(state.output_owners)):
        return "OutputBelongsToCompletedChild"
    return None


def explore(mutant: str | None):
    initial = State()
    queue = deque([(initial, ())])
    seen = {initial}
    transitions = 0
    while queue:
        state, path = queue.popleft()
        broken = violation(state)
        if broken:
            return len(seen), transitions, path, broken
        for action, candidate in successors(state, mutant):
            transitions += 1
            if candidate not in seen:
                seen.add(candidate)
                queue.append((candidate, path + (action,)))
    return len(seen), transitions, None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutant", choices=tuple(MUTANTS))
    args = parser.parse_args()
    states, transitions, path, broken = explore(args.mutant)
    if args.mutant:
        expected = MUTANTS[args.mutant]
        if broken != expected:
            print(
                f"MUTANT_SURVIVED mutant={args.mutant} expected={expected} "
                f"actual={broken} states={states} transitions={transitions}"
            )
            return 1
        print(
            f"MUTANT_KILLED mutant={args.mutant} property={broken} "
            f"path={' -> '.join(path or ())}"
        )
        return 0
    if broken:
        print(f"FAIL property={broken} path={' -> '.join(path or ())}")
        return 1
    print(
        f"SEQUENCE_PASS children={CHILDREN} bound={BOUND} "
        f"states={states} transitions={transitions} violations=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
