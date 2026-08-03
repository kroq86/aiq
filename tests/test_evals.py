import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aiq.evals import EvalCase, EvalDataset, EvalRunner, evaluate_trace
from aiq.trace import CausalTrace, TraceEvent


def trace_event(
    event_id: str,
    event_type: str,
    *,
    data=None,
    causation_id: str | None = None,
    operation_id: str | None = None,
) -> TraceEvent:
    return TraceEvent(
        event_id=event_id,
        event_type=event_type,
        stream_id="agent:run",
        stream_version=0,
        global_position=1,
        correlation_id=None,
        causation_id=causation_id,
        operation_id=operation_id,
        data=data or {},
        metadata={},
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def completed_trace(*events: TraceEvent) -> CausalTrace:
    return CausalTrace(
        agent_name="agent",
        run_id="run",
        events=events,
        edges=(),
        roots=tuple(event.event_id for event in events if event.causation_id is None),
        dangling_causation=(),
        terminal=True,
        terminal_event_type="RunCompleted",
        latest_stream_version=len(events) - 1,
    )


class EvalCaseTests(unittest.TestCase):
    def test_loads_documented_case_shape(self) -> None:
        document = {
            "cases": [
                {
                    "id": "weather",
                    "input": "Find the weather and save it",
                    "expected_tools": ["get_weather", "save_result"],
                    "expected_terminal": "completed",
                    "max_model_steps": 3,
                    "assertions": {
                        "no_tool_failure": True,
                        "stable_operation_ids": True,
                    },
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evals.json"
            path.write_text(json.dumps(document))
            dataset = EvalDataset.load(path)

        case = dataset.cases[0]
        self.assertEqual(case.case_id, "weather")
        self.assertEqual(case.expected_tools, ("get_weather", "save_result"))
        self.assertTrue(case.assertions.stable_operation_ids)

    def test_rejects_unknown_assertion(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown eval assertions"):
            EvalCase.from_dict(
                {"input": "hello", "assertions": {"hallucination_score": 0.5}}
            )


class TraceAssertionTests(unittest.TestCase):
    def test_matching_trace_passes(self) -> None:
        request = trace_event(
            "tool-request",
            "ToolCallRequested",
            data={"call": {"name": "get_weather", "arguments": {}}},
            operation_id="tool-request",
        )
        result = trace_event(
            "tool-result",
            "ToolCallSucceeded",
            causation_id="tool-request",
            operation_id="tool-request",
        )
        trace = completed_trace(
            trace_event(
                "model-request", "ModelCallRequested", operation_id="model-request"
            ),
            request,
            result,
            trace_event("terminal", "RunCompleted", causation_id="tool-result"),
        )
        case = EvalCase.from_dict(
            {
                "input": "weather",
                "expected_tools": ["get_weather"],
                "expected_terminal": "completed",
                "max_model_steps": 1,
                "assertions": {
                    "no_tool_failure": True,
                    "stable_operation_ids": True,
                },
            }
        )

        self.assertEqual(evaluate_trace(case, trace), ())

    def test_reports_trajectory_limit_failure_and_unstable_operation(self) -> None:
        trace = completed_trace(
            trace_event("model-1", "ModelCallRequested", operation_id="wrong"),
            trace_event("model-2", "ModelCallRequested", operation_id="model-2"),
            trace_event(
                "tool-request",
                "ToolCallRequested",
                data={"call": {"name": "other_tool"}},
                operation_id="tool-request",
            ),
            trace_event("failure", "ToolCallFailed", causation_id="tool-request"),
        )
        case = EvalCase.from_dict(
            {
                "input": "weather",
                "expected_tools": ["get_weather"],
                "max_model_steps": 1,
                "assertions": {
                    "no_tool_failure": True,
                    "stable_operation_ids": True,
                },
            }
        )

        names = {failure.assertion for failure in evaluate_trace(case, trace)}
        self.assertEqual(
            names,
            {"expected_tools", "max_model_steps", "no_tool_failure", "stable_operation_ids"},
        )
        operation_failures = [
            failure
            for failure in evaluate_trace(case, trace)
            if failure.assertion == "stable_operation_ids"
        ]
        self.assertEqual(len(operation_failures), 2)


class EvalRunnerTests(unittest.TestCase):
    def test_runs_cases_sequentially_and_summarizes(self) -> None:
        calls: list[str] = []

        async def execute(case: EvalCase) -> CausalTrace:
            calls.append(case.input)
            return completed_trace(trace_event("terminal", "RunCompleted"))

        dataset = EvalDataset.from_data(
            [
                {"input": "first", "expected_terminal": "completed"},
                {"input": "second", "expected_terminal": "failed"},
            ]
        )
        result = asyncio.run(EvalRunner(execute).run(dataset))

        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(result.passed_count, 1)
        self.assertEqual(result.failed_count, 1)
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
