from __future__ import annotations

import unittest


def _load_checker():
    from formal.lease_gate import check

    return check


class LeaseGateModelTests(unittest.TestCase):
    def test_normal_model_is_safe_and_non_vacuous(self) -> None:
        checker = _load_checker()
        _, _, path, broken, witnesses = checker.explore(mutant=None)
        self.assertIsNone(path)
        self.assertIsNone(broken)
        self.assertEqual(checker.WITNESSES - witnesses, frozenset())

    def test_targeted_mutants_are_killed(self) -> None:
        checker = _load_checker()
        for mutant, expected in checker.MUTANTS.items():
            with self.subTest(mutant=mutant):
                _, _, path, broken, _ = checker.explore(mutant=mutant)
                self.assertIsNotNone(path)
                self.assertEqual(broken, expected)


if __name__ == "__main__":
    unittest.main()
