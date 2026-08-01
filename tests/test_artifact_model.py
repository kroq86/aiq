from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


CHECKER = Path(__file__).resolve().parents[1] / "formal/artifacts/check.py"
MUTANTS = (
    "missing_version_invocation",
    "different_digest",
    "different_storage_reference",
    "external_stores_blob",
    "external_overwrites_embedded",
    "retry_second_logical_version",
    "failed_registration_partial_row",
)


class ArtifactModelTests(unittest.TestCase):
    def run_checker(self, *args):
        return subprocess.run(
            [sys.executable, str(CHECKER), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_model_is_safe_within_bound(self):
        completed = self.run_checker()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS bound=7", completed.stdout)
        self.assertIn("deadlocks=0", completed.stdout)

    def test_semantic_mutants_are_killed(self):
        for mutant in MUTANTS:
            with self.subTest(mutant=mutant):
                completed = self.run_checker("--mutant", mutant)
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
                self.assertIn(f"MUTANT_KILLED mutant={mutant}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
