from __future__ import annotations

import unittest
from dataclasses import replace

from .reference import NormalizedEvent, assert_invariants, initial_state, step


def completed_state():
    state = initial_state()
    for _ in range(30):
        state = step(state, "reaction")
        state = step(state, "effect")
    return state


class InvariantOracleMutationTests(unittest.TestCase):
    def test_oracle_rejects_changed_causation_and_operation(self) -> None:
        valid = completed_state()
        index = next(
            index
            for index, event in enumerate(valid.history)
            if event.event_type == "ToolCallSucceeded"
        )
        original = valid.history[index]
        mutated = replace(
            valid,
            history=valid.history[:index]
            + (replace(original, operation="e1"),)
            + valid.history[index + 1 :],
        )
        with self.assertRaises(AssertionError):
            assert_invariants(valid, mutated)

    def test_oracle_rejects_duplicate_result_and_terminal(self) -> None:
        valid = completed_state()
        model_result = next(
            event for event in valid.history if event.event_type == "ModelCallSucceeded"
        )
        duplicate = replace(
            model_result,
            identity=f"e{len(valid.history) + 1}",
        )
        with self.assertRaises(AssertionError):
            assert_invariants(
                valid,
                replace(valid, history=valid.history + (duplicate,)),
            )

        terminal = NormalizedEvent(
            "RunFailed",
            f"e{len(valid.history) + 1}",
            causation=valid.history[-1].identity,
        )
        with self.assertRaises(AssertionError):
            assert_invariants(valid, replace(valid, history=valid.history + (terminal,)))

    def test_oracle_rejects_history_rewrite_and_checkpoint_rollback(self) -> None:
        valid = completed_state()
        rewritten = replace(valid, history=valid.history[1:])
        with self.assertRaises(AssertionError):
            assert_invariants(valid, rewritten)

        rollback = replace(valid, reaction_checkpoint=valid.reaction_checkpoint - 1)
        with self.assertRaises(AssertionError):
            assert_invariants(valid, rollback)


if __name__ == "__main__":
    unittest.main()
