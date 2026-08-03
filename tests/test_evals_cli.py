import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


EXECUTOR_SOURCE = """
from datetime import datetime, timezone
from aiq.trace import CausalTrace, TraceEvent

async def execute(case):
    completed = case.input == "pass"
    event_type = "RunCompleted" if completed else "RunFailed"
    event = TraceEvent(
        event_id="terminal",
        event_type=event_type,
        stream_id="agent:run",
        stream_version=0,
        global_position=1,
        correlation_id=None,
        causation_id=None,
        operation_id=None,
        data={},
        metadata={},
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return CausalTrace(
        agent_name="agent",
        run_id="run",
        events=(event,),
        edges=(),
        roots=(event.event_id,),
        dangling_causation=(),
        terminal=True,
        terminal_event_type=event_type,
        latest_stream_version=0,
    )
"""


class EvalCliTests(unittest.TestCase):
    def run_cli(self, directory: Path, dataset: dict, *args: str):
        (directory / "eval_fixture.py").write_text(textwrap.dedent(EXECUTOR_SOURCE))
        dataset_path = directory / "dataset.json"
        dataset_path.write_text(json.dumps(dataset))
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(directory), str(ROOT / "src"), str(ROOT))
        )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "aiq.evals.cli",
                "eval",
                "run",
                str(dataset_path),
                *args,
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_writes_ci_report_and_returns_one_for_assertion_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            report_path = directory / "report.json"
            completed = self.run_cli(
                directory,
                {
                    "name": "tool-loop",
                    "executor": "eval_fixture:execute",
                    "cases": [
                        {"id": "pass", "input": "pass", "expected_terminal": "completed"},
                        {"id": "fail", "input": "fail", "expected_terminal": "completed"},
                    ],
                },
                "--json-report",
                str(report_path),
            )
            report = json.loads(report_path.read_text())

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("dataset: tool-loop", completed.stdout)
        self.assertIn("cases total: 2", completed.stdout)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["cases"][1]["id"], "fail")
        self.assertTrue(report["cases"][1]["failures"])

    def test_returns_two_when_executor_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = self.run_cli(
                Path(temp_dir),
                {"name": "invalid", "cases": [{"input": "pass"}]},
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("must define executor", completed.stderr)

    def test_compare_returns_one_for_regression(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            baseline = directory / "baseline.json"
            candidate = directory / "candidate.json"
            output = directory / "comparison.json"
            summary = {
                "terminal_status": "completed",
                "tool_trajectory": [],
                "model_steps": 1,
                "tool_failures": 0,
                "causal_shape_digest": "causal",
                "operation_relation_digest": "operation",
                "committed_observation_digest": "observations",
            }
            baseline.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "dataset": "base",
                        "total": 1,
                        "passed": 1,
                        "failed": 0,
                        "cases": [
                            {
                                "id": "case",
                                "passed": True,
                                "failures": [],
                                "trace_summary": summary,
                            }
                        ],
                    }
                )
            )
            failed_summary = {**summary, "terminal_status": "failed"}
            candidate.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "dataset": "candidate",
                        "total": 1,
                        "passed": 0,
                        "failed": 1,
                        "cases": [
                            {
                                "id": "case",
                                "passed": False,
                                "failures": ["terminal mismatch"],
                                "trace_summary": failed_summary,
                            }
                        ],
                    }
                )
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "aiq.evals.cli",
                    "eval",
                    "compare",
                    str(baseline),
                    str(candidate),
                    "--json-report",
                    str(output),
                ],
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            comparison = json.loads(output.read_text())

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(comparison["regressions"], 1)
        self.assertEqual(comparison["cases"][0]["status"], "regressed")


if __name__ == "__main__":
    unittest.main()
