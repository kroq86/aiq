from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COVERAGE = ROOT / "docs" / "aiq-coverage.md"
TICKET_HEADING = re.compile(r"^## Ticket (\d+): .+$", re.MULTILINE)
STATUS = re.compile(
    r"^- Status: `(implemented|partial|out_of_scope)`$", re.MULTILINE
)
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _sections(text: str) -> dict[int, str]:
    headings = list(TICKET_HEADING.finditer(text))
    sections: dict[int, str] = {}
    for index, heading in enumerate(headings):
        ticket = int(heading.group(1))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        if ticket in sections:
            raise AssertionError(f"duplicate AIQ ticket: {ticket}")
        sections[ticket] = text[heading.end() : end]
    return sections


class AIQCoverageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = COVERAGE.read_text(encoding="utf-8")
        cls.sections = _sections(cls.text)

    def test_covers_every_ticket_from_10_through_47_once(self) -> None:
        self.assertEqual(set(self.sections), set(range(10, 48)))
        self.assertEqual(len(TICKET_HEADING.findall(self.text)), 38)

    def test_readme_links_to_the_coverage_contract(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("(docs/aiq-coverage.md)", readme)

    def test_each_ticket_has_status_answer_evidence_and_boundaries(self) -> None:
        for ticket, body in self.sections.items():
            with self.subTest(ticket=ticket):
                status = STATUS.search(body)
                self.assertIsNotNone(status, "missing or invalid Status")
                self.assertRegex(body, r"(?m)^- Levels: `[^`\n]+`")
                self.assertRegex(body, r"(?m)^- Answer: \S.+$")
                self.assertRegex(body, r"(?m)^- Boundary: \S.+$")
                self.assertRegex(body, r"(?m)^- Not proved: \S.+$")

                evidence = re.search(
                    r"(?ms)^- Evidence:\n(?P<items>(?:  - .+\n)+)"
                    r"(?=- Boundary:)",
                    body,
                )
                self.assertIsNotNone(evidence, "missing Evidence list")
                targets = LINK.findall(evidence.group("items"))
                self.assertTrue(targets, "Evidence must contain a local link")

                resolved = []
                for target in targets:
                    local_target = target.split("#", 1)[0]
                    path = Path(local_target)
                    self.assertFalse(path.is_absolute(), f"absolute evidence path: {target}")
                    evidence_path = (COVERAGE.parent / path).resolve()
                    self.assertTrue(
                        evidence_path.exists(),
                        f"missing evidence path: {target}",
                    )
                    resolved.append(evidence_path)

                if status.group(1) == "implemented":
                    self.assertTrue(
                        any(
                            "tests" in path.relative_to(ROOT).parts
                            or "formal" in path.relative_to(ROOT).parts
                            for path in resolved
                        ),
                        "implemented requires executable test/formal evidence",
                    )

                if status.group(1) == "out_of_scope":
                    boundary = re.search(r"(?m)^- Boundary: (.+)$", body)
                    self.assertIsNotNone(boundary)
                    self.assertGreater(len(boundary.group(1).strip()), 20)


if __name__ == "__main__":
    unittest.main()
