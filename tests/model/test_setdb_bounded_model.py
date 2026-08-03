from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


class SetdbBoundedModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.binary = os.environ.get("SETDB_BIN") or shutil.which("setdb")
        if not cls.binary:
            raise unittest.SkipTest("setdb executable is not available")
        cls.checker = Path(__file__).parents[2] / "formal/setdb/check_aiq_model.py"

    def run_checker(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.checker), "--setdb-bin", self.binary, *arguments],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_bounded_transition_system_has_no_invariant_violation(self) -> None:
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("violations=0", result.stdout)

    def test_terminal_mutation_produces_counterexample(self) -> None:
        result = self.run_checker("--mutant", "duplicate_terminal")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("counterexample=", result.stdout)


if __name__ == "__main__":
    unittest.main()
