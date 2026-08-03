from __future__ import annotations

import unittest
from dataclasses import replace

from aiq import (
    CausalTrace,
    DurableDispatcher,
    DurableEffectDispatcher,
    Event,
    InMemoryEffectAttemptStore,
    InMemoryEventStore,
    ModelLoopLimits,
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolRegistry,
    TraceEvent,
    TraceService,
    build_run_report,
    run_report_to_json,
    run_stream_id,
)

from tests.test_model_loop_policy import define, run


def get_weather(city: str) -> dict:
    return {"city": city, "temperature": 23}


class ToolThenFinalProvider:
    async def complete(self, request, *, operation_id):
        if request.messages[-1].role == "tool":
            return ModelResponse(ModelMessage("assistant", "done"))
        return ModelResponse(
            ModelMessage("assistant", "checking"),
            (ToolCall("weather-1", "get_weather", {"city": "Tbilisi"}),),
        )


async def _drive_to_completion(
    agent, runtime, store, stream_id, *, attempt_store=None
) -> None:
    reactions = DurableDispatcher(
        agent=runtime.agent, store=store, subscription_name="report:reactions"
    )
    effects = DurableEffectDispatcher(
        agent=runtime.agent,
        store=store,
        effects=runtime.effects,
        context=runtime.context,
        subscription_name="report:effects",
        attempt_store=attempt_store,
    )
    for _ in range(30):
        if not (await reactions.run_once() or await effects.run_once()):
            break


class RunReportTests(unittest.TestCase):
    def test_report_counts_match_a_completed_goal_gated_tool_run(self) -> None:
        tools = ToolRegistry.from_functions(get_weather)
        agent, loop = define(
            tools,
            snapshot_state=lambda state: {"answer": state.answer},
            goal_satisfied=lambda state: True,
            limits=ModelLoopLimits(max_model_steps=6, max_tool_calls=6),
        )
        runtime = agent.build_runtime(
            context={"model": ToolThenFinalProvider(), "tools": tools}
        )
        store = InMemoryEventStore()
        stream_id = run_stream_id("assistant", "report-happy-path")
        run(
            store.append(
                stream_id,
                -1,
                (
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": "1"},
                    ),
                    agent.handle_command("message", {"text": "weather"})[0],
                ),
            )
        )
        run(_drive_to_completion(agent, runtime, store, stream_id))

        service = TraceService(store=store, agents={"assistant": runtime.agent})
        trace = run(service.export("assistant", "report-happy-path"))
        report = build_run_report(trace)

        self.assertEqual(report.terminal_status, "completed")
        self.assertEqual(report.tool_call_count, 1)
        self.assertEqual(report.tool_call_succeeded_count, 1)
        self.assertEqual(report.tool_call_failed_count, 0)
        self.assertEqual(report.tool_call_rejected_count, 0)
        self.assertEqual(report.validation_retry_count, 0)
        self.assertTrue(report.goal_policy_observed)
        self.assertTrue(report.goal_satisfied)
        self.assertFalse(report.workflow_invariant_violated)
        self.assertFalse(report.workflow_cycle_detected)
        self.assertFalse(report.abstained)
        self.assertGreaterEqual(report.model_step_count, 2)
        self.assertEqual(len(report.tool_call_latency_seconds), 1)
        self.assertTrue(all(s >= 0 for s in report.tool_call_latency_seconds))

        payload = run_report_to_json(report)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["report_kind"], "aiq-run-report")
        self.assertEqual(payload["tool_outcomes"]["succeeded"], 1)
        self.assertEqual(payload["control"]["goal_satisfied"], True)
        self.assertEqual(
            len(payload["latency_seconds"]["tool_call"]), 1
        )
        self.assertIsNone(payload["idempotency"])

    def test_report_joins_explicit_attempt_telemetry(self) -> None:
        tools = ToolRegistry.from_functions(get_weather)
        agent, loop = define(tools)
        runtime = agent.build_runtime(
            context={"model": ToolThenFinalProvider(), "tools": tools}
        )
        store = InMemoryEventStore()
        attempt_store = InMemoryEffectAttemptStore()
        stream_id = run_stream_id("assistant", "report-attempts")
        run(
            store.append(
                stream_id,
                -1,
                (
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": "1"},
                    ),
                    agent.handle_command("message", {"text": "weather"})[0],
                ),
            )
        )
        run(
            _drive_to_completion(
                agent,
                runtime,
                store,
                stream_id,
                attempt_store=attempt_store,
            )
        )
        service = TraceService(store=store, agents={"assistant": runtime.agent})
        trace = run(service.export("assistant", "report-attempts"))
        attempts = run(attempt_store.load_for_stream(stream_id))
        tool_request = next(
            event
            for event in trace.events
            if event.event_type == "ToolCallRequested"
        )
        run(
            attempt_store.record_start(
                operation_id=tool_request.operation_id,
                stream_id=tool_request.stream_id,
                request_event_type=tool_request.event_type,
                request_global_position=tool_request.global_position,
                subscription_name="report:effects",
            )
        )
        attempts = run(attempt_store.load_for_stream(stream_id))

        report = build_run_report(trace, effect_attempts=attempts)
        metrics = report.effect_attempt_metrics

        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.attempt_count, 4)
        self.assertEqual(metrics.operation_count, 3)
        self.assertEqual(metrics.retried_operation_count, 1)
        self.assertEqual(metrics.retry_attempt_count, 1)
        self.assertEqual(metrics.max_attempts_per_operation, 2)
        self.assertEqual(
            metrics.attempt_count_by_event_type,
            {"ModelCallRequested": 2, "ToolCallRequested": 2},
        )
        payload = run_report_to_json(report)
        self.assertEqual(
            payload["idempotency"],
            {
                "observation_kind": "durable-dispatch-attempt",
                "dispatch_attempt_count": 4,
                "operation_count": 3,
                "retried_operation_count": 1,
                "retry_attempt_count": 1,
                "max_attempts_per_operation": 2,
                "dispatch_attempt_count_by_event_type": {
                    "ModelCallRequested": 2,
                    "ToolCallRequested": 2,
                },
            },
        )

        observed_zero = build_run_report(trace, effect_attempts=())
        self.assertEqual(observed_zero.effect_attempt_metrics.attempt_count, 0)
        self.assertIsNone(build_run_report(trace).effect_attempt_metrics)

        with self.assertRaisesRegex(ValueError, "does not identify"):
            build_run_report(
                trace,
                effect_attempts=(
                    replace(attempts[0], operation_id="unknown-operation"),
                ),
            )
        with self.assertRaisesRegex(ValueError, "does not match trace"):
            build_run_report(
                trace,
                effect_attempts=(
                    replace(attempts[0], stream_id="assistant:other-run"),
                ),
            )

    def test_report_is_neutral_when_no_control_policy_is_configured(self) -> None:
        tools = ToolRegistry.from_functions(get_weather)
        agent, loop = define(tools)
        runtime = agent.build_runtime(
            context={"model": ToolThenFinalProvider(), "tools": tools}
        )
        store = InMemoryEventStore()
        stream_id = run_stream_id("assistant", "report-no-control")
        run(
            store.append(
                stream_id,
                -1,
                (
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": "1"},
                    ),
                    agent.handle_command("message", {"text": "weather"})[0],
                ),
            )
        )
        run(_drive_to_completion(agent, runtime, store, stream_id))

        service = TraceService(store=store, agents={"assistant": runtime.agent})
        trace = run(service.export("assistant", "report-no-control"))
        report = build_run_report(trace)

        self.assertEqual(report.terminal_status, "completed")
        self.assertFalse(report.goal_policy_observed)
        self.assertIsNone(report.goal_satisfied)
        self.assertFalse(report.abstained)

    def test_report_uses_explicit_custom_namespace_contract(self) -> None:
        tools = ToolRegistry.from_functions(get_weather)
        agent, loop = define(
            tools,
            snapshot_state=lambda state: {"answer": state.answer},
            goal_satisfied=lambda state: True,
            limits=ModelLoopLimits(max_model_steps=6, max_tool_calls=6),
            namespace="billing",
        )
        runtime = agent.build_runtime(
            context={"model": ToolThenFinalProvider(), "tools": tools}
        )
        store = InMemoryEventStore()
        stream_id = run_stream_id("assistant", "report-custom-namespace")
        run(
            store.append(
                stream_id,
                -1,
                (
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": "1"},
                    ),
                    agent.handle_command("message", {"text": "weather"})[0],
                ),
            )
        )
        run(_drive_to_completion(agent, runtime, store, stream_id))

        service = TraceService(store=store, agents={"assistant": runtime.agent})
        trace = run(service.export("assistant", "report-custom-namespace"))

        with self.assertRaisesRegex(ValueError, "originating loop.events"):
            build_run_report(trace)

        _, other_loop = define(tools, namespace="other")
        with self.assertRaisesRegex(ValueError, "does not match trace"):
            build_run_report(trace, loop_events=other_loop.events)

        report = build_run_report(trace, loop_events=loop.events)
        self.assertEqual(report.terminal_status, "completed")
        self.assertEqual(report.model_step_count, 2)
        self.assertEqual(report.tool_call_count, 1)
        self.assertEqual(report.tool_call_succeeded_count, 1)
        self.assertTrue(report.goal_policy_observed)
        self.assertTrue(report.goal_satisfied)
        self.assertFalse(report.workflow_invariant_violated)
        self.assertFalse(report.workflow_cycle_detected)
        self.assertFalse(report.abstained)
        self.assertEqual(len(report.model_latency_seconds), 2)
        self.assertEqual(len(report.tool_call_latency_seconds), 1)

    def test_default_contract_ignores_unrelated_suffix_collision(self) -> None:
        def trace_event(event_id: str, event_type: str, position: int) -> TraceEvent:
            return TraceEvent(
                event_id=event_id,
                event_type=event_type,
                stream_id="agent/run",
                stream_version=position - 1,
                global_position=position,
                correlation_id=None,
                causation_id=None,
                operation_id=None,
                data={},
                metadata={},
                created_at="2026-08-03T00:00:00+00:00",
            )

        trace = CausalTrace(
            agent_name="agent",
            run_id="run",
            events=(
                trace_event("model-request", "ModelCallRequested", 1),
                trace_event("domain-event", "BillingToolCallFailed", 2),
            ),
            edges=(),
            roots=("model-request", "domain-event"),
            dangling_causation=(),
            terminal=False,
            terminal_event_type=None,
            latest_stream_version=1,
        )

        report = build_run_report(trace)
        self.assertEqual(report.model_step_count, 1)
        self.assertEqual(report.tool_call_failed_count, 0)


if __name__ == "__main__":
    unittest.main()
