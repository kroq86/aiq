"""Reproducible manifest for the five targeted v0.4 runtime source mutants
recorded in docs/release-evidence-0.4.md's "Targeted mutation evidence"
table.

This is NOT the project's existing FASM/setdb mutation convention (see
formal/*/verify-mutants) -- those mutate a pure finite model via a
compiled --mutant variant with no file patching. There is no equivalent
mechanism for the real, compiled `DurableModelLoop` runtime, so this script
does literal, in-memory, restore-guaranteed source patching of
src/agentlog/model_loop.py:

  1. read the current file content into memory (whatever is on disk right
     now -- this repo's model_loop.py is an uncommitted work-in-progress
     file, so restoration MUST come from this in-memory snapshot, never
     from `git checkout`, or it would discard uncommitted work);
  2. write the mutated content;
  3. run the exact pytest command for that mutant;
  4. restore the original content from the in-memory snapshot in a
     `finally` block, unconditionally, even on failure or Ctrl-C;
  5. report MUTANT_KILLED (mutant made the expected test fail) or
     MUTANT_SURVIVED (test still passed despite the mutation).

Usage:
    PYTHONPATH=src:. python formal/model/verify_v04_runtime_mutants.py
    PYTHONPATH=src:. python formal/model/verify_v04_runtime_mutants.py --mutant goal_gate_disabled

Does not claim mutation completeness. Five targeted mutants only, matching
the five v0.4 safety claims named in docs/release-evidence-0.4.md.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = REPO_ROOT / "src" / "agentlog" / "model_loop.py"

MUTANTS: dict[str, dict[str, str]] = {
    "goal_gate_disabled": {
        "old": (
            "                if goal_satisfied is not None and not goal_satisfied(state):\n"
            "                    return events.GoalNotSatisfied(\"workflow goal is not satisfied\")"
        ),
        "new": (
            "                if False and goal_satisfied is not None and not goal_satisfied(state):  # MUTANT\n"
            "                    return events.GoalNotSatisfied(\"workflow goal is not satisfied\")"
        ),
        "test_command": [
            "tests/test_v04_constrained_execution_e2e.py",
            "-k", "test_goal_not_satisfied_is_restart_equivalent_and_terminal",
        ],
        "expected_failing_test": "V04ControlRestartEquivalenceTests.test_goal_not_satisfied_is_restart_equivalent_and_terminal",
        "property": "GoalPolicyConfigured and not goal_satisfied(state) => RunCompleted must not occur",
    },
    "goal_not_satisfied_still_completes": {
        "old": (
            "                if goal_satisfied is not None and not goal_satisfied(state):\n"
            "                    return events.GoalNotSatisfied(\"workflow goal is not satisfied\")\n"
            "                completed = ("
        ),
        "new": (
            "                if goal_satisfied is not None and not goal_satisfied(state):\n"
            "                    return (  # MUTANT\n"
            "                        events.GoalNotSatisfied(\"workflow goal is not satisfied\"),\n"
            "                        events.AnswerProduced(response.message.content),\n"
            "                        events.RunCompleted(),\n"
            "                    )\n"
            "                completed = ("
        ),
        "test_command": [
            "tests/test_v04_constrained_execution_e2e.py",
            "-k", "test_goal_not_satisfied_is_restart_equivalent_and_terminal",
        ],
        "expected_failing_test": "V04ControlRestartEquivalenceTests.test_goal_not_satisfied_is_restart_equivalent_and_terminal",
        "property": "GoalNotSatisfied => not RunCompleted",
    },
    "cycle_detection_disabled": {
        "old": (
            "            continuation = dict(event.continuation)\n"
            "            cycle_reason = _cycle_failure(continuation, limits.max_state_visits)\n"
            "            if cycle_reason is not None:"
        ),
        "new": (
            "            continuation = dict(event.continuation)\n"
            "            cycle_reason = None  # MUTANT: was _cycle_failure(continuation, limits.max_state_visits)\n"
            "            if cycle_reason is not None:"
        ),
        "test_command": [
            "tests/test_v04_constrained_execution_e2e.py",
            "-k", "test_workflow_cycle_detected_is_restart_equivalent_and_terminal",
        ],
        "expected_failing_test": "V04ControlRestartEquivalenceTests.test_workflow_cycle_detected_is_restart_equivalent_and_terminal",
        "property": "repeated-state guard must stop a second forbidden tool execution",
    },
    "cycle_detected_still_completes": {
        "old": (
            "            events.WorkflowInvariantViolated,\n"
            "            events.GoalNotSatisfied,\n"
            "            events.WorkflowCycleDetected,\n"
            "            events.MiddlewareFailed,\n"
            "            events.ArtifactResolutionFailed,\n"
            "            events.InstructionResolutionFailed,\n"
            "        ):\n"
            "            agent.react(failure_type)(\n"
            "                lambda state, event: events.RunFailed(str(event.reason))\n"
            "            )"
        ),
        "new": (
            "            events.WorkflowInvariantViolated,\n"
            "            events.GoalNotSatisfied,\n"
            "            events.MiddlewareFailed,\n"
            "            events.ArtifactResolutionFailed,\n"
            "            events.InstructionResolutionFailed,\n"
            "        ):\n"
            "            agent.react(failure_type)(\n"
            "                lambda state, event: events.RunFailed(str(event.reason))\n"
            "            )\n"
            "        agent.react(events.WorkflowCycleDetected)(  # MUTANT\n"
            "            lambda state, event: events.RunCompleted()\n"
            "        )"
        ),
        "test_command": [
            "tests/test_v04_constrained_execution_e2e.py",
            "-k", "test_workflow_cycle_detected_is_restart_equivalent_and_terminal",
        ],
        "expected_failing_test": "V04ControlRestartEquivalenceTests.test_workflow_cycle_detected_is_restart_equivalent_and_terminal",
        "property": "WorkflowCycleDetected => not RunCompleted",
    },
    "run_abstained_not_terminal": {
        "old": (
            "        agent.terminal(events.RunCompleted, status=\"completed\")\n"
            "        agent.terminal(events.RunFailed, status=\"failed\")\n"
            "        agent.terminal(events.RunAbstained, status=\"abstained\")"
        ),
        "new": (
            "        agent.terminal(events.RunCompleted, status=\"completed\")\n"
            "        agent.terminal(events.RunFailed, status=\"failed\")\n"
            "        # MUTANT: agent.terminal(events.RunAbstained, status=\"abstained\")"
        ),
        "test_command": [
            "tests/test_v04_constrained_execution_e2e.py",
            "-k", "test_run_abstained_is_registered_as_a_terminal_status",
        ],
        "expected_failing_test": "V04ConstrainedExecutionEndToEndTests.test_run_abstained_is_registered_as_a_terminal_status",
        "property": "RunAbstained must be registered as a terminal status",
    },
}


def run_one(name: str, spec: dict[str, str]) -> bool:
    original = TARGET.read_text()
    if spec["old"] not in original:
        print(f"MUTANT_INVALID mutant={name} reason=anchor_not_found_in_current_file")
        return False
    mutated = original.replace(spec["old"], spec["new"], 1)
    if mutated == original:
        print(f"MUTANT_INVALID mutant={name} reason=no_change_applied")
        return False
    try:
        TARGET.write_text(mutated)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *spec["test_command"]],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": f"src:{REPO_ROOT}"},
            capture_output=True,
            text=True,
        )
        killed = result.returncode != 0
        tag = "MUTANT_KILLED" if killed else "MUTANT_SURVIVED"
        print(f"{tag} mutant={name} property={spec['property']!r} "
              f"expected_failing_test={spec['expected_failing_test']}")
        if not killed:
            print(result.stdout[-2000:])
        return killed
    finally:
        TARGET.write_text(original)
        verify = TARGET.read_text()
        if verify != original:
            raise RuntimeError(
                f"RESTORE_FAILED mutant={name}: {TARGET} was not restored to its "
                "pre-mutation content -- fix this before trusting any other result"
            )
        print(f"RESTORE_VERIFIED mutant={name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutant", choices=tuple(MUTANTS))
    args = parser.parse_args()
    names = [args.mutant] if args.mutant else list(MUTANTS)
    all_killed = True
    for name in names:
        all_killed &= run_one(name, MUTANTS[name])
    print(f"V04_RUNTIME_MUTATION_MATRIX mutants={len(names)} "
          f"all_killed={all_killed}")
    return 0 if all_killed else 1


if __name__ == "__main__":
    raise SystemExit(main())
