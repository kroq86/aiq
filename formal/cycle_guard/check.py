"""Bounded finite abstraction of the v0.4 repeated-workflow-state guard
(`_cycle_failure` in `src/aiq/model_loop.py`).

This is a standalone small local model, not an extension of
`formal/model/spec.py`'s trace reference model and not the FASM/setdb
`ModelLoopModel` in `formal/abstract/`. It follows the same pure-Python
bounded-BFS + inductive-invariant + targeted-mutant convention as
`formal/middleware/check.py` and `formal/sequence/check.py`, because there is
no `setdb` binary available to drive a FASM-encoded model in every
environment this repository is checked in.

Scope: only the guard's own safety properties (a blocked repeat never
completes the run; a detected cycle is always followed by failure; the
guard's own class transitions never let a bypassed repeat proceed). This
model reuses the same nondeterministic `low -> low | before -> at` counter
abstraction already established for `ModelClass`/`ToolClass` in
`formal/FORMAL_MODEL.md` Sec. 4 -- it does not re-derive a new technique.

Explicitly NOT established by this model:
- a refinement/abstraction mapping (`beta`) from the real, JSON-normalized,
  unbounded-domain `_fingerprint_snapshot` mechanism down to this model's
  three classes -- that mapping, and its soundness argument, is the actual
  cost driver called "moderate" in the v0.4 release-hardening feasibility
  spike (`docs/release-evidence-0.4.md`), and remains open;
- anything about `formal/model/spec.py`'s `NOTE(vacuity)` assertions -- those
  are about the separate trace/bisimulation reference model used for runtime
  refinement, and remain vacuous exactly as documented there;
- the goal/invariant completion gate (`GoalSatisfied`/`GoalNotSatisfied`/
  `WorkflowInvariantViolated`) -- that remains outside this checker and now
  has its own local model in `formal/completion_gate/`.

What this model DOES newly establish: `WorkflowCycleDetected` is a
non-vacuous, reachable event in a checked bounded exploration (unlike the
trace reference model, where it is currently unreachable by any generated
action), with an inductive safety invariant and two killed targeted mutants.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, replace

BOUND = 10

MUTANTS = {
    "disable_cycle_guard": "AtClassNeverProceedsToToolCall",
    "cycle_allows_completion": "CycleDetectedNeverPrecedesRunCompleted",
}


@dataclass(frozen=True, slots=True)
class State:
    history: tuple[str, ...] = ("Start",)
    cycle_class: str = "low"  # low | before | at -- current fingerprint's repeat class
    terminal: bool = False
    # Ghost flag: only ever set true by the disable_cycle_guard mutant path,
    # the same pattern as `response_identity_preserved` in
    # formal/middleware/check.py.
    cycle_bypassed: bool = False


def successors(
    state: State, *, mutant: str | None
) -> tuple[tuple[str, State], ...]:
    if state.terminal or len(state.history) >= BOUND:
        return ()

    options: list[tuple[str, State]] = [
        (
            "answer",
            replace(
                state,
                history=state.history + ("AnswerProduced", "RunCompleted"),
                terminal=True,
            ),
        )
    ]

    guard_blocks = state.cycle_class == "at"
    if guard_blocks and mutant != "disable_cycle_guard":
        outcome = "RunCompleted" if mutant == "cycle_allows_completion" else "RunFailed"
        options.append(
            (
                "repeat_blocked",
                replace(
                    state,
                    history=state.history + ("WorkflowCycleDetected", outcome),
                    terminal=True,
                ),
            )
        )
    else:
        bypassed = guard_blocks and mutant == "disable_cycle_guard"
        next_classes = ("low", "before") if state.cycle_class == "low" else ("at",)
        for next_class in next_classes:
            options.append(
                (
                    f"repeat_to_{next_class}",
                    replace(
                        state,
                        history=state.history + ("ToolCallRequested", "ToolCallSucceeded"),
                        cycle_class=next_class,
                        cycle_bypassed=state.cycle_bypassed or bypassed,
                    ),
                )
            )

    # The model may always propose a genuinely different workflow state;
    # a first visit to that state resets its own repeat counter to "low".
    options.append(
        (
            "new_state",
            replace(
                state,
                history=state.history + ("ToolCallRequested", "ToolCallSucceeded"),
                cycle_class="low",
            ),
        )
    )
    return tuple(options)


def violation(state: State) -> str | None:
    if state.cycle_bypassed:
        return "AtClassNeverProceedsToToolCall"
    history = state.history
    if "WorkflowCycleDetected" in history and "RunCompleted" in history:
        return "CycleDetectedNeverPrecedesRunCompleted"
    if "WorkflowCycleDetected" in history:
        detected_at = history.index("WorkflowCycleDetected")
        if "RunFailed" not in history[detected_at:]:
            return "CycleDetectedIsAlwaysFollowedByFailure"
    if state.terminal and history[-1] not in {"RunCompleted", "RunFailed"}:
        return "TerminalIsAbsorbing"
    return None


def explore(
    *, mutant: str | None
) -> tuple[int, int, tuple[str, ...] | None, str | None, bool]:
    initial = State()
    queue = deque([(initial, ())])
    seen = {initial}
    transitions = 0
    cycle_detected_witnessed = False
    while queue:
        state, path = queue.popleft()
        if "WorkflowCycleDetected" in state.history:
            cycle_detected_witnessed = True
        broken = violation(state)
        if broken:
            return len(seen), transitions, path, broken, cycle_detected_witnessed
        for action, candidate in successors(state, mutant=mutant):
            transitions += 1
            if candidate not in seen:
                seen.add(candidate)
                queue.append((candidate, path + (action,)))
    return len(seen), transitions, None, None, cycle_detected_witnessed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutant", choices=tuple(MUTANTS))
    args = parser.parse_args()
    states, transitions, path, broken, witnessed = explore(mutant=args.mutant)

    if args.mutant:
        expected = MUTANTS[args.mutant]
        if broken != expected:
            print(
                f"MUTANT_SURVIVED mutant={args.mutant} states={states} "
                f"transitions={transitions}"
            )
            return 1
        print(
            f"MUTANT_KILLED mutant={args.mutant} property={broken} bound={BOUND} "
            f"states={states} transitions={transitions} "
            f"path={' -> '.join(path or ())}"
        )
        return 0

    if broken:
        print(f"FAIL property={broken} path={' -> '.join(path or ())}")
        return 1
    if not witnessed:
        print("VACUOUS: WorkflowCycleDetected was never reached -- not evidence")
        return 1
    print(
        f"PASS bound={BOUND} states={states} transitions={transitions} "
        f"cycle_detected_witnessed={witnessed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
