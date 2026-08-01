from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


CHECKER = Path(__file__).resolve().parents[1] / "formal/instructions/check.py"
MUTANTS = (
    "latest_artifact",
    "missing_empty",
    "omit_template_version",
    "ignore_digest",
    "reresolve_restart",
)


class InstructionModelTests(unittest.TestCase):
    def run_checker(self, *args):
        return subprocess.run(
            [sys.executable, str(CHECKER), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_finite_domain_is_safe_and_non_vacuous(self):
        completed = self.run_checker()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS domain=2x2x2", completed.stdout)

    def test_targeted_semantic_mutants_are_killed(self):
        for mutant in MUTANTS:
            with self.subTest(mutant=mutant):
                completed = self.run_checker("--mutant", mutant)
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
                self.assertIn(f"MUTANT_KILLED mutant={mutant}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
