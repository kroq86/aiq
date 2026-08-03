from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "pyproject.toml"
RELEASE_HEADING = re.compile(
    r"^## (?P<version>\d+\.\d+\.\d+) - (?P<date>\d{4}-\d{2}-\d{2})$",
    re.MULTILINE,
)


class ReleaseMetadataContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.changelog = CHANGELOG.read_text(encoding="utf-8")
        cls.project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        cls.releases = RELEASE_HEADING.findall(cls.changelog)

    def test_package_version_matches_latest_changelog_release(self) -> None:
        self.assertTrue(self.releases, "CHANGELOG has no numeric release headings")
        latest_version, _ = self.releases[0]

        self.assertEqual(self.project["project"]["version"], latest_version)

    def test_distribution_name_is_separate_from_aiq_import_name(self) -> None:
        self.assertEqual(self.project["project"]["name"], "aiq-runtime")
        self.assertEqual(
            self.project["project"]["scripts"]["aiq"],
            "aiq.evals.cli:main",
        )

    def test_unreleased_precedes_unique_descending_release_history(self) -> None:
        unreleased_at = self.changelog.index("## Unreleased")
        first_release_at = RELEASE_HEADING.search(self.changelog)
        self.assertIsNotNone(first_release_at)
        self.assertLess(unreleased_at, first_release_at.start())

        versions = [version for version, _ in self.releases]
        self.assertEqual(len(versions), len(set(versions)))
        version_keys = [tuple(map(int, version.split("."))) for version in versions]
        self.assertEqual(version_keys, sorted(version_keys, reverse=True))

    def test_reconstructed_release_sections_are_present(self) -> None:
        self.assertEqual(
            {version for version, _ in self.releases},
            {
                "0.3.0",
                "0.4.0",
                "0.4.1",
                "0.4.2",
                "0.4.3",
                "0.5.0",
                "0.5.1",
                "0.5.2",
            },
        )


if __name__ == "__main__":
    unittest.main()
