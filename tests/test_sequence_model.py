from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


CHECKER = Path(__file__).resolve().parents[1] / "formal/sequence/check.py"
MUTANTS = (
    "early_next",
    "restart_completed",
    "skip_index",
    "wrong_run",
    "replace_run_id",
    "advance_failure",
    "early_parent_complete",
    "double_terminal",
    "wrong_output",
    "duplicate_start",
)


class SequenceModelTests(unittest.TestCase):
    def run_checker(self, *args):
        return subprocess.run(
            [sys.executable, str(CHECKER), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_base_model_is_safe_and_non_vacuous(self):
        completed = self.run_checker()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("SEQUENCE_PASS children=3", completed.stdout)

    def test_transition_and_interface_mutants_are_killed(self):
        for mutant in MUTANTS:
            with self.subTest(mutant=mutant):
                completed = self.run_checker("--mutant", mutant)
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
                self.assertIn(f"MUTANT_KILLED mutant={mutant}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
