"""Tests for the `python -m aiq.demo` subprocess boundary.

Deliberately exercises the real subprocess interface (not just the
directly-imported functions) for the CLI-shaped requirements, since that's
the actual contract Flow Xray calls across.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aiq.demo import generate_completed_trace, write_trace_json

_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
_EXAMPLE_SCRIPT = (
    Path(__file__).resolve().parents[1] / "examples" / "export_flow_xray_traces.py"
)


def _run_demo(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC_DIR)
    return subprocess.run(
        [sys.executable, "-m", "aiq.demo", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


_COMPLETED_SEQUENCE = [
    "UserMessageAdded",
    "ModelCallRequested",
    "ModelCallSucceeded",
    "ToolCallRequested",
    "ToolCallSucceeded",
    "ModelCallRequested",
    "ModelCallSucceeded",
    "AnswerProduced",
    "RunCompleted",
]
_ACTIVE_SEQUENCE = [
    "UserMessageAdded",
    "ModelCallRequested",
    "ModelCallSucceeded",
    "ToolCallRequested",
]
_TERMINAL_EVENT_TYPES = {"RunCompleted", "RunFailed", "RunCancelled"}


class CompletedRunTests(unittest.TestCase):
    """Requirements 1-8."""

    def test_completed_subprocess_exact_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "trace.json"
            result = _run_demo("--status", "completed", "--output", str(output_path))

            self.assertEqual(result.returncode, 0, result.stderr)  # (1)
            self.assertTrue(output_path.exists())  # (2)

            document = json.loads(output_path.read_text())  # (3) parses as JSON
            self.assertEqual(document["schema_version"], 1)  # (4)
            self.assertEqual(document["graph_kind"], "domain-event-history")  # (5)
            self.assertEqual(len(document["nodes"]), 9)  # (6)
            self.assertEqual(len(document["edges"]), 8)  # (6)
            self.assertEqual(
                [node["event_type"] for node in document["nodes"]],
                _COMPLETED_SEQUENCE,
            )  # (7)
            self.assertEqual(document["terminal_status"], "completed")  # (8)
            self.assertEqual(document["latest_stream_version"], 8)
            self.assertEqual(len(document["roots"]), 1)

            self.assertIn("output=", result.stdout)
            self.assertIn("status=completed", result.stdout)
            self.assertIn("nodes=9", result.stdout)
            self.assertIn("edges=8", result.stdout)


class ActiveRunTests(unittest.TestCase):
    """Requirements 9-13."""

    def test_active_subprocess_exact_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "trace.json"
            result = _run_demo("--status", "active", "--output", str(output_path))

            self.assertEqual(result.returncode, 0, result.stderr)  # (9)

            document = json.loads(output_path.read_text())
            self.assertEqual(document["terminal_status"], "active")  # (10)

            event_types = [node["event_type"] for node in document["nodes"]]
            self.assertTrue(_TERMINAL_EVENT_TYPES.isdisjoint(event_types))  # (11)
            self.assertEqual(event_types, _ACTIVE_SEQUENCE)  # (12)
            self.assertEqual(event_types[-1], "ToolCallRequested")  # (12)

            unresolved_request = document["nodes"][-1]
            self.assertIsNotNone(unresolved_request["operation_id"])  # (13)
            self.assertEqual(
                unresolved_request["operation_id"],
                unresolved_request["event_id"],
            )


class InvalidArgumentTests(unittest.TestCase):
    """Requirements 14-15."""

    def test_invalid_status_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = _run_demo(
                "--status", "bogus", "--output", str(Path(temp_dir) / "trace.json")
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--status", result.stderr)

    def test_missing_output_returns_nonzero(self) -> None:
        result = _run_demo("--status", "completed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--output", result.stderr)


class AtomicWriteTests(unittest.TestCase):
    """Requirements 16-18."""

    def test_output_parent_directory_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "nested" / "dirs" / "trace.json"
            result = _run_demo("--status", "completed", "--output", str(output_path))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output_path.exists())

    def test_existing_output_is_replaced_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "trace.json"
            output_path.write_text('{"stale": true}')

            result = _run_demo("--status", "active", "--output", str(output_path))
            self.assertEqual(result.returncode, 0, result.stderr)

            document = json.loads(output_path.read_text())
            self.assertEqual(document["terminal_status"], "active")
            # No leftover temp files beside the destination.
            leftovers = [
                path
                for path in Path(temp_dir).iterdir()
                if path != output_path
            ]
            self.assertEqual(leftovers, [])

    def test_failed_write_preserves_existing_destination(self) -> None:
        """Direct test of write_trace_json(): force mkstemp to fail (by
        making the destination directory read-only) and confirm the
        pre-existing destination file is untouched -- no partial/corrupt
        file is ever visible at the destination path."""

        async def build_document() -> dict:
            return await generate_completed_trace()

        import asyncio

        document = asyncio.run(build_document())

        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "readonly"
            target_dir.mkdir()
            destination = target_dir / "trace.json"
            original_content = '{"pre-existing": true}'
            destination.write_text(original_content)

            original_mode = target_dir.stat().st_mode
            target_dir.chmod(stat.S_IREAD | stat.S_IEXEC)
            try:
                with self.assertRaises(OSError):
                    write_trace_json(document, destination)
            finally:
                target_dir.chmod(original_mode)

            self.assertEqual(destination.read_text(), original_content)
            leftovers = [path for path in target_dir.iterdir() if path != destination]
            self.assertEqual(leftovers, [])


class DeterminismTests(unittest.TestCase):
    """Requirement 19."""

    def test_repeated_completed_runs_preserve_deterministic_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = Path(temp_dir) / "first.json"
            second_path = Path(temp_dir) / "second.json"
            self.assertEqual(
                _run_demo("--status", "completed", "--output", str(first_path)).returncode,
                0,
            )
            self.assertEqual(
                _run_demo("--status", "completed", "--output", str(second_path)).returncode,
                0,
            )

            def strip_created_at(document: dict) -> dict:
                document = dict(document)
                document["nodes"] = [
                    {key: value for key, value in node.items() if key != "created_at"}
                    for node in document["nodes"]
                ]
                return document

            first = strip_created_at(json.loads(first_path.read_text()))
            second = strip_created_at(json.loads(second_path.read_text()))
            self.assertEqual(first, second)


class SharedLogicTests(unittest.TestCase):
    """Requirement 20."""

    def test_example_script_uses_shared_demo_module(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(_SRC_DIR)
            result = subprocess.run(
                [sys.executable, str(_EXAMPLE_SCRIPT), "--output-dir", temp_dir],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            completed = json.loads(
                (Path(temp_dir) / "aiq-completed-domain-event-history-v1.json").read_text()
            )
            active = json.loads(
                (Path(temp_dir) / "aiq-active-domain-event-history-v1.json").read_text()
            )
            self.assertEqual(
                [node["event_type"] for node in completed["nodes"]],
                _COMPLETED_SEQUENCE,
            )
            self.assertEqual(
                [node["event_type"] for node in active["nodes"]],
                _ACTIVE_SEQUENCE,
            )

            # Same generator, invoked directly: must match the example
            # script's output exactly (excluding wall-clock created_at),
            # proving there is exactly one implementation, not two.
            import asyncio

            direct = asyncio.run(generate_completed_trace())

            def strip_created_at(document: dict) -> dict:
                document = dict(document)
                document["nodes"] = [
                    {key: value for key, value in node.items() if key != "created_at"}
                    for node in document["nodes"]
                ]
                return document

            self.assertEqual(strip_created_at(completed), strip_created_at(direct))


if __name__ == "__main__":
    unittest.main()
