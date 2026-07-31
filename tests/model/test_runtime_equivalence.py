from __future__ import annotations

import unittest

from .normalization import normalize_history
from .reference import assert_invariants, initial_state, step
from .runtime_harness import RuntimeHarness


class RuntimeReferenceEquivalenceTests(unittest.TestCase):
    def test_restart_after_every_dispatch_boundary_is_bisimilar(self) -> None:
        reference = initial_state()
        runtime = RuntimeHarness.create()
        actions = tuple(
            action
            for _ in range(18)
            for action in ("reaction", "restart", "effect", "restart")
        )

        self.assertEqual(normalize_history(runtime.history()), reference.history)
        for action in actions:
            previous = reference
            reference = step(reference, action)
            runtime.dispatch(action)
            assert_invariants(previous, reference)
            self.assertEqual(normalize_history(runtime.history()), reference.history)
            self.assertEqual(runtime.checkpoints(), (
                reference.reaction_checkpoint,
                reference.effect_checkpoint,
            ))
            rebuilt = runtime.runtime.agent.rebuild(runtime.history())
            self.assertEqual(rebuilt.answer, reference.answer)

        self.assertTrue(reference.terminal)
        self.assertEqual(reference.answer, "23 C")

    def test_request_result_relations_are_unique_and_causal(self) -> None:
        runtime = RuntimeHarness.create()
        for _ in range(30):
            runtime.dispatch("reaction")
            runtime.dispatch("effect")
        history = normalize_history(runtime.history())
        results_by_cause: dict[str, list] = {}
        result_types = {
            "ModelCallSucceeded",
            "ModelCallFailed",
            "ModelCallRejected",
            "ModelOutputRejected",
            "ToolCallSucceeded",
            "ToolCallFailed",
            "ToolCallRejected",
        }
        for event in history:
            if event.event_type in result_types:
                self.assertIsNotNone(event.causation)
                results_by_cause.setdefault(event.causation, []).append(event)
                self.assertEqual(event.operation, event.causation)
        self.assertTrue(results_by_cause)
        self.assertTrue(all(len(results) == 1 for results in results_by_cause.values()))


if __name__ == "__main__":
    unittest.main()
