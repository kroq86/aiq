import unittest

from formal.run_abstained.check import (
    EXPECTED_CASES,
    MUTANTS,
    WITNESS_EVENTS,
    explore,
    initial_states,
)


class RunAbstainedModelTests(unittest.TestCase):
    def test_phase_and_decision_axes_are_explicit_and_non_vacuous(
        self,
    ) -> None:
        initial = tuple(state for _, state in initial_states())

        self.assertEqual(len(initial), 4)
        self.assertEqual(
            {
                (state.validation_phase, state.decision)
                for state in initial
            },
            EXPECTED_CASES,
        )
        for state in initial:
            with self.subTest(
                validation_phase=state.validation_phase,
                decision=state.decision,
            ):
                if state.validation_phase == "request":
                    self.assertEqual(len(state.history), 1)
                else:
                    self.assertEqual(
                        state.history[0],
                        "ToolValidationSucceeded(request)",
                    )

    def test_normal_model_exhausts_expected_finite_state_space(self) -> None:
        (
            states,
            transitions,
            path,
            broken,
            witnessed_events,
            cases,
            terminal_deadlocks,
        ) = explore(mutant=None)

        self.assertEqual((states, transitions), (8, 4))
        self.assertIsNone(path)
        self.assertIsNone(broken)
        self.assertTrue(WITNESS_EVENTS.issubset(witnessed_events))
        self.assertEqual(cases, EXPECTED_CASES)
        self.assertEqual(terminal_deadlocks, 4)

    def test_each_targeted_mutant_has_its_intended_counterexample(
        self,
    ) -> None:
        for mutant, expected_property in MUTANTS.items():
            with self.subTest(mutant=mutant):
                _, _, path, broken, _, _, _ = explore(mutant=mutant)

                self.assertEqual(broken, expected_property)
                self.assertTrue(path)


if __name__ == "__main__":
    unittest.main()
