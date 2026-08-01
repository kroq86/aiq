from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agentlog.evals import EvalCase, EvalReport, EvalRunner, compare_reports
from agentlog.trace import CausalTrace, TraceEvent


def event(
    event_id: str,
    event_type: str,
    *,
    cause: str | None = None,
    operation: str | None = None,
    data=None,
) -> TraceEvent:
    return TraceEvent(
        event_id=event_id,
        event_type=event_type,
        stream_id="agent:run",
        stream_version=0,
        global_position=1,
        correlation_id=None,
        causation_id=cause,
        operation_id=operation,
        data=data or {},
        metadata={},
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def trace(*events: TraceEvent, terminal="RunCompleted") -> CausalTrace:
    return CausalTrace(
        agent_name="agent",
        run_id="run",
        events=events,
        edges=(),
        roots=(),
        dangling_causation=(),
        terminal=True,
        terminal_event_type=terminal,
        latest_stream_version=len(events) - 1,
    )


def report(
    dataset: str,
    case_id: str,
    actual: CausalTrace,
    *,
    expected="completed",
    expected_tools=(),
):
    case = EvalCase.from_dict(
        {
            "id": case_id,
            "input": "input",
            "expected_terminal": expected,
            "expected_tools": expected_tools,
        }
    )
    result = __import__("asyncio").run(EvalRunner(lambda _: _return(actual)).run_case(case))
    from agentlog.evals.runner import EvalRunResult

    return EvalReport.from_result(dataset, EvalRunResult((result,)))


async def _return(value):
    return value


class EvalComparisonTests(unittest.TestCase):
    def test_normalization_ignores_concrete_ids_and_timestamps(self) -> None:
        baseline = report(
            "base",
            "same",
            trace(
                event("a", "ModelCallRequested", operation="a"),
                event("b", "ModelCallSucceeded", cause="a", operation="a"),
                event("c", "RunCompleted", cause="b"),
            ),
        )
        candidate = report(
            "candidate",
            "same",
            trace(
                event("x", "ModelCallRequested", operation="x"),
                event("y", "ModelCallSucceeded", cause="x", operation="x"),
                event("z", "RunCompleted", cause="y"),
            ),
        )
        comparison = compare_reports(baseline, candidate)
        self.assertEqual(comparison.cases[0].status, "unchanged")

    def test_detects_regression_and_each_durable_dimension(self) -> None:
        baseline = report(
            "base",
            "weather",
            trace(
                event("m", "ModelCallRequested", operation="m"),
                event(
                    "t",
                    "ToolCallRequested",
                    cause="m",
                    operation="t",
                    data={"call": {"name": "weather"}},
                ),
                event("r", "ToolCallSucceeded", cause="t", operation="t"),
                event("done", "RunCompleted", cause="r"),
            ),
            expected_tools=("weather",),
        )
        candidate = report(
            "candidate",
            "weather",
            trace(
                event("m1", "ModelCallRequested", operation="m1"),
                event("m2", "ModelCallRequested", cause="m1", operation="m2"),
                event(
                    "t",
                    "ToolCallRequested",
                    cause="m2",
                    operation="t",
                    data={"call": {"name": "other"}},
                ),
                event("f", "ToolCallFailed", cause="t", operation="different"),
                event("done", "RunFailed", cause="f"),
                terminal="RunFailed",
            ),
            expected="completed",
            expected_tools=("weather",),
        )
        comparison = compare_reports(baseline, candidate)
        case = comparison.cases[0]
        self.assertEqual(case.status, "regressed")
        self.assertEqual(
            {difference.field for difference in case.differences},
            {
                "passed",
                "terminal_status",
                "tool_trajectory",
                "model_steps",
                "tool_failures",
                "causal_shape",
                "operation_identity_relations",
                "committed_observations",
            },
        )

    def test_reports_added_missing_improved_and_round_trips(self) -> None:
        passed = trace(event("done", "RunCompleted"))
        failed = trace(event("failed", "RunFailed"), terminal="RunFailed")
        base_one = report("base", "improved", failed, expected="completed")
        base_two = report("base", "missing", passed)
        candidate_one = report("candidate", "improved", passed)
        candidate_two = report("candidate", "added", passed)
        baseline = EvalReport(
            "base", 2, 1, 1, base_one.cases + base_two.cases
        )
        candidate = EvalReport(
            "candidate", 2, 2, 0, candidate_one.cases + candidate_two.cases
        )
        comparison = compare_reports(baseline, candidate)
        self.assertEqual(
            {case.case_id: case.status for case in comparison.cases},
            {"improved": "improved", "missing": "missing", "added": "added"},
        )
        self.assertEqual(EvalReport.from_dict(baseline.to_dict()), baseline)


if __name__ == "__main__":
    unittest.main()
