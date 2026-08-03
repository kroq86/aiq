import unittest

from formal.completion_gate.check import (
    MUTANTS,
    WITNESS_EVENTS,
    explore,
    initial_states,
)


class CompletionGateModelTests(unittest.TestCase):
    def test_configuration_axes_are_independent_and_non_vacuous(self) -> None:
        initial = tuple(state for _, state in initial_states())

        self.assertEqual(len(initial), 9)
        self.assertEqual(
            {
                (state.invariant_configured, state.goal_configured)
                for state in initial
            },
            {(False, False), (False, True), (True, False), (True, True)},
        )

    def test_normal_model_exhausts_expected_finite_state_space(self) -> None:
        (
            states,
            transitions,
            path,
            broken,
            witnessed_events,
            configured_cases,
            terminal_deadlocks,
        ) = explore(mutant=None)

        self.assertEqual((states, transitions), (15, 11))
        self.assertIsNone(path)
        self.assertIsNone(broken)
        self.assertTrue(WITNESS_EVENTS.issubset(witnessed_events))
        self.assertEqual(len(configured_cases), 4)
        self.assertEqual(terminal_deadlocks, 4)

    def test_each_targeted_mutant_has_a_counterexample(self) -> None:
        for mutant, expected_property in MUTANTS.items():
            with self.subTest(mutant=mutant):
                _, _, path, broken, _, _, _ = explore(mutant=mutant)

                self.assertEqual(broken, expected_property)
                self.assertTrue(path)


if __name__ == "__main__":
    unittest.main()
