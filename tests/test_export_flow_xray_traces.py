import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from export_flow_xray_traces import (  # noqa: E402
    generate_active_trace,
    generate_completed_trace,
)


def run(coro):
    return asyncio.run(coro)


_EXPECTED_TOP_LEVEL_KEYS = {
    "schema_version",
    "graph_kind",
    "agent_name",
    "run_id",
    "terminal_status",
    "latest_stream_version",
    "roots",
    "nodes",
    "edges",
    "timeline",
    "dangling_causation",
}
_EXPECTED_NODE_KEYS = {
    "event_id",
    "event_type",
    "stream_id",
    "stream_version",
    "global_position",
    "correlation_id",
    "causation_id",
    "operation_id",
    "data",
    "metadata",
    "created_at",
}


def _assert_matches_schema_v1(testcase: unittest.TestCase, document: dict) -> None:
    testcase.assertEqual(set(document), _EXPECTED_TOP_LEVEL_KEYS)
    testcase.assertEqual(document["schema_version"], 1)
    testcase.assertEqual(document["graph_kind"], "domain-event-history")
    for node in document["nodes"]:
        testcase.assertEqual(set(node), _EXPECTED_NODE_KEYS)
    for edge in document["edges"]:
        testcase.assertEqual(set(edge), {"source_event_id", "target_event_id", "kind"})
        testcase.assertEqual(edge["kind"], "caused")
    for entry in document["timeline"]:
        testcase.assertEqual(set(entry), {"event_id", "stream_version", "global_position"})
    testcase.assertEqual(
        [entry["stream_version"] for entry in document["timeline"]],
        sorted(entry["stream_version"] for entry in document["timeline"]),
    )


class GeneratorFunctionTests(unittest.TestCase):
    def test_completed_trace_has_terminal_status_completed(self) -> None:
        document = run(generate_completed_trace())
        _assert_matches_schema_v1(self, document)
        self.assertEqual(document["terminal_status"], "completed")
        self.assertEqual(
            [node["event_type"] for node in document["nodes"]],
            [
                "UserMessageAdded",
                "ModelCallRequested",
                "ModelCallSucceeded",
                "ToolCallRequested",
                "ToolCallSucceeded",
                "ModelCallRequested",
                "ModelCallSucceeded",
                "AnswerProduced",
                "RunCompleted",
            ],
        )
        self.assertEqual(document["dangling_causation"], [])

    def test_active_trace_has_terminal_status_active(self) -> None:
        document = run(generate_active_trace())
        _assert_matches_schema_v1(self, document)
        self.assertEqual(document["terminal_status"], "active")
        self.assertEqual(
            [node["event_type"] for node in document["nodes"]],
            [
                "UserMessageAdded",
                "ModelCallRequested",
                "ModelCallSucceeded",
                "ToolCallRequested",
            ],
        )
        # The trailing ToolCallRequested has no result yet -- confirming this
        # really is a mid-flight snapshot, not a relabeled completed run.
        self.assertNotIn(
            "ToolCallSucceeded",
            [node["event_type"] for node in document["nodes"]],
        )

    def test_event_ids_are_deterministic_across_runs(self) -> None:
        first = run(generate_completed_trace())
        second = run(generate_completed_trace())

        def strip_created_at(document: dict) -> dict:
            document = dict(document)
            document["nodes"] = [
                {key: value for key, value in node.items() if key != "created_at"}
                for node in document["nodes"]
            ]
            return document

        self.assertEqual(strip_created_at(first), strip_created_at(second))


class GeneratorCliTests(unittest.TestCase):
    def test_cli_writes_both_canonical_filenames_with_valid_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            script = (
                Path(__file__).resolve().parents[1]
                / "examples"
                / "export_flow_xray_traces.py"
            )
            src_dir = Path(__file__).resolve().parents[1] / "src"

            import os
            import subprocess

            env = dict(os.environ)
            env["PYTHONPATH"] = str(src_dir)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--output-dir",
                    str(output_dir),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            completed_path = output_dir / "agentlog-completed-domain-event-history-v1.json"
            active_path = output_dir / "agentlog-active-domain-event-history-v1.json"
            self.assertTrue(completed_path.exists())
            self.assertTrue(active_path.exists())

            completed_document = json.loads(completed_path.read_text())
            active_document = json.loads(active_path.read_text())
            _assert_matches_schema_v1(self, completed_document)
            _assert_matches_schema_v1(self, active_document)
            self.assertEqual(completed_document["terminal_status"], "completed")
            self.assertEqual(active_document["terminal_status"], "active")


if __name__ == "__main__":
    unittest.main()
