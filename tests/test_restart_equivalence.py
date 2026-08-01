from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from agentlog.evals import (
    EvalCase,
    RestartEquivalenceRunner,
    RestartPoint,
    UnsupportedRestartPoint,
)
from agentlog.trace import CausalTrace, TraceEvent


def event(event_id, event_type, *, cause=None, operation=None, data=None):
    return TraceEvent(
        event_id=event_id,
        event_type=event_type,
        stream_id="agent:run",
        stream_version=0,
        global_position=0,
        correlation_id=None,
        causation_id=cause,
        operation_id=operation,
        data=data or {},
        metadata={},
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def successful_trace(prefix="e", *, result=23):
    events = (
        event(f"{prefix}1", "ModelCallRequested", operation=f"{prefix}1"),
        event(
            f"{prefix}2",
            "ToolCallRequested",
            cause=f"{prefix}1",
            operation=f"{prefix}2",
            data={"call": {"name": "weather"}},
        ),
        event(
            f"{prefix}3",
            "ToolCallSucceeded",
            cause=f"{prefix}2",
            operation=f"{prefix}2",
            data={"result": result},
        ),
        event(f"{prefix}4", "RunCompleted", cause=f"{prefix}3"),
    )
    return CausalTrace(
        agent_name="agent",
        run_id="run",
        events=events,
        edges=(),
        roots=(events[0].event_id,),
        dangling_causation=(),
        terminal=True,
        terminal_event_type="RunCompleted",
        latest_stream_version=3,
    )


class Executor:
    async def run_normal(self, case):
        return successful_trace("normal-")

    async def restart_points(self, case, normal_trace):
        return (
            RestartPoint("after-model-request"),
            RestartPoint("after-tool-result"),
            RestartPoint("unsupported-window"),
        )

    async def run_restarted(self, case, point):
        if point.boundary == "unsupported-window":
            raise UnsupportedRestartPoint("adapter cannot inject this crash window")
        if point.boundary == "after-tool-result":
            return successful_trace("restart-", result=99)
        return successful_trace("restart-")


class RestartEquivalenceTests(unittest.TestCase):
    def test_matches_identity_renaming_and_exposes_mismatch_and_unsupported(self):
        case = EvalCase.from_dict({"id": "weather", "input": "weather"})
        result = asyncio.run(RestartEquivalenceRunner(Executor()).run_case(case))
        self.assertFalse(result.passed)
        self.assertEqual(
            [scenario.status for scenario in result.scenarios],
            ["matched", "mismatched", "unsupported"],
        )
        self.assertEqual(
            {item.field for item in result.scenarios[1].differences},
            {"committed_observations"},
        )

    def test_empty_restart_point_set_is_not_vacuously_passing(self):
        class Empty(Executor):
            async def restart_points(self, case, normal_trace):
                return ()

        case = EvalCase.from_dict({"input": "hello"})
        result = asyncio.run(RestartEquivalenceRunner(Empty()).run_case(case))
        self.assertFalse(result.passed)

    def test_duplicate_boundaries_are_rejected(self):
        class Duplicate(Executor):
            async def restart_points(self, case, normal_trace):
                return (RestartPoint("same"), RestartPoint("same"))

        case = EvalCase.from_dict({"input": "hello"})
        with self.assertRaisesRegex(ValueError, "duplicate boundaries"):
            asyncio.run(RestartEquivalenceRunner(Duplicate()).run_case(case))


if __name__ == "__main__":
    unittest.main()
