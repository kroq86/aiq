from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "formal/middleware/check.py"


class MiddlewareModelTests(unittest.TestCase):
    def run_checker(self, *args):
        return subprocess.run(
            [sys.executable, str(CHECKER), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_bounded_model_has_no_violation(self):
        completed = self.run_checker()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS bound=8", completed.stdout)

    def test_targeted_before_failure_mutant_is_killed(self):
        completed = self.run_checker("--mutant", "before_invokes")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("MUTANT_KILLED", completed.stdout)
        self.assertIn("before_model_fail", completed.stdout)

    def test_response_identity_mutant_is_killed(self):
        completed = self.run_checker("--mutant", "rewrite_response_identity")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("AfterModelPreservesResponseIdentity", completed.stdout)


if __name__ == "__main__":
    unittest.main()
