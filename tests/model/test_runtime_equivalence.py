from __future__ import annotations

import unittest

from .normalization import normalize_history
from .reference import assert_invariants, initial_state, step
from .runtime_harness import RuntimeHarness


class RuntimeReferenceEquivalenceTests(unittest.TestCase):
    def test_validation_control_outcomes_refine_reference_model(self) -> None:
        outcomes = (
            "accept",
            "reject",
            "retry",
            "ambiguous",
            "postcondition_failure",
        )
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                reference = initial_state()
                runtime = RuntimeHarness.create(validation=True)
                validation_applied = False
                for _ in range(20):
                    actions = ["reaction"]
                    next_effect = reference.effect_checkpoint
                    if (
                        not validation_applied
                        and next_effect < len(reference.history)
                        and reference.history[next_effect].event_type
                        == "ToolCallRequested"
                    ):
                        actions.append(f"effect_validation_{outcome}")
                        validation_applied = True
                    else:
                        actions.append("effect")
                    for action in actions:
                        previous = reference
                        reference = step(reference, action)
                        runtime.dispatch(action)
                        assert_invariants(previous, reference)
                        self.assertEqual(
                            normalize_history(runtime.history()), reference.history
                        )
                        self.assertEqual(
                            runtime.checkpoints(),
                            (
                                reference.reaction_checkpoint,
                                reference.effect_checkpoint,
                            ),
                        )
                        previous = reference
                        reference = step(reference, "restart")
                        runtime.dispatch("restart")
                        assert_invariants(previous, reference)
                        self.assertEqual(
                            normalize_history(runtime.history()), reference.history
                        )
                    if reference.terminal:
                        break
                self.assertTrue(validation_applied)
                self.assertTrue(reference.terminal)
                if outcome in {"accept", "retry", "ambiguous"}:
                    self.assertEqual(reference.answer, "23 C")
                else:
                    self.assertIsNone(reference.answer)

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
