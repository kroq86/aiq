from __future__ import annotations

import unittest

from aiq import (
    DurableDispatcher,
    DurableEffectDispatcher,
    Event,
    InMemoryEventStore,
    ModelLoopLimits,
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolRegistry,
    ValidationDecision,
    run_stream_id,
)

from formal.model.spec import ReferenceState, assert_invariants
from tests.model.normalization import normalize_history
from tests.model.runtime_harness import RuntimeHarness
from tests.test_model_loop_policy import define, get_weather, run


def execute(outcome: str, *, restart_after_every_dispatch: bool):
    runtime = RuntimeHarness.create(validation=True)
    validation_applied = False
    for _ in range(30):
        runtime.dispatch("reaction")
        if restart_after_every_dispatch:
            runtime.dispatch("restart")

        history = runtime.history()
        effect_checkpoint = runtime.checkpoints()[1]
        if (
            not validation_applied
            and effect_checkpoint < len(history)
            and history[effect_checkpoint].event.event_type == "ToolCallRequested"
        ):
            runtime.dispatch(f"effect_validation_{outcome}")
            validation_applied = True
        else:
            runtime.dispatch("effect")
        if restart_after_every_dispatch:
            runtime.dispatch("restart")

        history = runtime.history()
        if history[-1].event.event_type in {"RunCompleted", "RunFailed"}:
            break
    else:
        raise AssertionError("v0.4 acceptance run did not terminate")

    if not validation_applied:
        raise AssertionError("validation policy was never applied")
    normalized = normalize_history(history)
    state = ReferenceState(normalized, len(normalized), len(normalized))
    assert_invariants(ReferenceState((), 0, 0), state)
    return history


class V04ConstrainedExecutionEndToEndTests(unittest.TestCase):
    def run_controlled(
        self,
        provider,
        *,
        goal_satisfied=None,
        workflow_invariant=None,
        max_state_visits=2,
        policy=None,
    ):
        calls = []

        def tracked_weather(city: str) -> dict:
            calls.append(city)
            return {"city": city, "temperature": 23}

        tracked_weather.__name__ = "get_weather"
        tools = ToolRegistry.from_functions(tracked_weather)
        agent, loop = define(
            tools,
            tool_policy="policy" if policy is not None else None,
            snapshot_state=lambda state: {"answer": state.answer},
            goal_satisfied=goal_satisfied,
            workflow_invariant=workflow_invariant,
            limits=ModelLoopLimits(
                max_model_steps=6,
                max_tool_calls=6,
                max_state_visits=max_state_visits,
            ),
        )
        context = {"model": provider, "tools": tools}
        if policy is not None:
            context["policy"] = policy
        runtime = agent.build_runtime(context=context)
        store = InMemoryEventStore()
        stream_id = run_stream_id("assistant", "v04-control")
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
        reactions = DurableDispatcher(
            agent=runtime.agent,
            store=store,
            subscription_name="v04:control:reactions",
        )
        effects = DurableEffectDispatcher(
            agent=runtime.agent,
            store=store,
            effects=runtime.effects,
            context=runtime.context,
            subscription_name="v04:control:effects",
        )
        for _ in range(50):
            if not (run(reactions.run_once()) | run(effects.run_once())):
                break
        return tuple(item.event for item in run(store.load(stream_id))), calls, loop

    def assert_restart_equivalent(self, outcome: str):
        normal = execute(outcome, restart_after_every_dispatch=False)
        restarted = execute(outcome, restart_after_every_dispatch=True)
        self.assertEqual(normalize_history(normal), normalize_history(restarted))
        return restarted

    def test_accept_records_both_evidence_boundaries_and_completes(self) -> None:
        history = self.assert_restart_equivalent("accept")
        events = tuple(item.event for item in history)
        validation = tuple(
            event for event in events if event.event_type == "ToolValidationSucceeded"
        )
        self.assertEqual(tuple(event.data["phase"] for event in validation), ("request", "result"))
        self.assertEqual(events[-1].event_type, "RunCompleted")
        self.assertEqual(sum(event.event_type == "ToolCallSucceeded" for event in events), 1)

    def test_irrelevant_request_fails_before_tool_result(self) -> None:
        history = self.assert_restart_equivalent("reject")
        events = tuple(item.event for item in history)
        failure = next(event for event in events if event.event_type == "ToolValidationFailed")
        self.assertEqual(failure.data["phase"], "request")
        self.assertFalse(failure.data["retryable"])
        self.assertNotIn("ToolCallSucceeded", tuple(event.event_type for event in events))
        self.assertEqual(events[-1].event_type, "RunFailed")

    def test_ambiguity_replans_without_executing_rejected_call(self) -> None:
        history = self.assert_restart_equivalent("ambiguous")
        events = tuple(item.event for item in history)
        failure = next(event for event in events if event.event_type == "ToolValidationFailed")
        self.assertTrue(failure.data["retryable"])
        self.assertEqual(len(failure.data["details"]["candidates"]), 2)
        self.assertNotIn("ToolCallSucceeded", tuple(event.event_type for event in events))
        self.assertGreaterEqual(
            sum(event.event_type == "ModelCallRequested" for event in events), 2
        )
        self.assertEqual(events[-1].event_type, "RunCompleted")

    def test_postcondition_failure_never_commits_tool_success_or_retries(self) -> None:
        history = self.assert_restart_equivalent("postcondition_failure")
        events = tuple(item.event for item in history)
        failure = next(event for event in events if event.event_type == "ToolValidationFailed")
        self.assertEqual(failure.data["phase"], "result")
        self.assertFalse(failure.data["retryable"])
        self.assertEqual(
            tuple(
                event.data["phase"]
                for event in events
                if event.event_type == "ToolValidationSucceeded"
            ),
            ("request",),
        )
        self.assertNotIn("ToolCallSucceeded", tuple(event.event_type for event in events))
        self.assertEqual(events[-1].event_type, "RunFailed")

    def test_false_goal_prevents_successful_completion(self) -> None:
        class FinalProvider:
            async def complete(self, request, *, operation_id):
                return ModelResponse(ModelMessage("assistant", "done"))

        events, calls, _ = self.run_controlled(
            FinalProvider(), goal_satisfied=lambda state: False
        )
        self.assertEqual(calls, [])
        self.assertIn("GoalNotSatisfied", tuple(event.event_type for event in events))
        self.assertNotIn("RunCompleted", tuple(event.event_type for event in events))
        self.assertEqual(events[-1].event_type, "RunFailed")

    def test_satisfied_goal_is_recorded_before_completion(self) -> None:
        class FinalProvider:
            async def complete(self, request, *, operation_id):
                return ModelResponse(ModelMessage("assistant", "done"))

        events, calls, _ = self.run_controlled(
            FinalProvider(), goal_satisfied=lambda state: True
        )
        types = tuple(event.event_type for event in events)
        self.assertEqual(calls, [])
        self.assertLess(types.index("GoalSatisfied"), types.index("RunCompleted"))
        self.assertEqual(events[-1].event_type, "RunCompleted")

    def test_workflow_invariant_blocks_false_completion(self) -> None:
        class FinalProvider:
            async def complete(self, request, *, operation_id):
                return ModelResponse(ModelMessage("assistant", "done"))

        events, calls, _ = self.run_controlled(
            FinalProvider(),
            workflow_invariant=lambda state: "required evidence is missing",
        )
        self.assertEqual(calls, [])
        violation = next(
            event for event in events if event.event_type == "WorkflowInvariantViolated"
        )
        self.assertEqual(violation.data["reason"], "required evidence is missing")
        self.assertEqual(events[-1].event_type, "RunFailed")

    def test_repeated_workflow_state_is_stopped_before_second_tool(self) -> None:
        class LoopingProvider:
            async def complete(self, request, *, operation_id):
                return ModelResponse(
                    ModelMessage("assistant", "checking"),
                    (ToolCall("weather-loop", "get_weather", {"city": "Tbilisi"}),),
                )

        events, calls, _ = self.run_controlled(
            LoopingProvider(), max_state_visits=1
        )
        self.assertEqual(calls, ["Tbilisi"])
        self.assertIn(
            "WorkflowCycleDetected", tuple(event.event_type for event in events)
        )
        self.assertEqual(events[-1].event_type, "RunFailed")

    def test_unified_abstain_has_distinct_terminal_status(self) -> None:
        class ToolProvider:
            async def complete(self, request, *, operation_id):
                return ModelResponse(
                    ModelMessage("assistant", "checking"),
                    (ToolCall("weather-1", "get_weather", {"city": "Tbilisi"}),),
                )

        class AbstainingPolicy:
            async def validate_request(self, call, context):
                return ValidationDecision(
                    "abstain", code="no_relevant_context", evidence={"top_score": 0.2}
                )

            async def validate_result(self, call, result, evidence, context):
                raise AssertionError("abstained call must not execute")

        events, calls, loop = self.run_controlled(
            ToolProvider(), policy=AbstainingPolicy()
        )
        self.assertEqual(calls, [])
        self.assertEqual(events[-1].event_type, "RunAbstained")

    def test_run_abstained_is_registered_as_a_terminal_status(self) -> None:
        tools = ToolRegistry.from_functions(get_weather)
        agent, loop = define(tools)

        class Provider:
            async def complete(self, request, *, operation_id):
                raise AssertionError("model must not be called by this check")

        runtime = agent.build_runtime(context={"model": Provider(), "tools": tools})
        terminal = runtime.agent.terminal_status_by_event_type
        self.assertEqual(
            terminal.get(loop.events.RunAbstained.__name__), "abstained"
        )
        self.assertEqual(
            terminal.get(loop.events.RunCompleted.__name__), "completed"
        )
        self.assertEqual(terminal.get(loop.events.RunFailed.__name__), "failed")

    def test_unified_reject_retry_replan_and_fail_transitions(self) -> None:
        class ToolThenAnswerProvider:
            async def complete(self, request, *, operation_id):
                if request.messages[-1].role == "tool":
                    return ModelResponse(ModelMessage("assistant", "replanned"))
                return ModelResponse(
                    ModelMessage("assistant", "checking"),
                    (ToolCall("weather-1", "get_weather", {"city": "Tbilisi"}),),
                )

        for status, terminal in (
            ("reject", "RunFailed"),
            ("retry", "RunCompleted"),
            ("replan", "RunCompleted"),
            ("fail", "RunFailed"),
        ):
            with self.subTest(status=status):
                class DecisionPolicy:
                    async def validate_request(self, call, context):
                        return ValidationDecision(status, code=f"test_{status}")

                    async def validate_result(self, call, result, evidence, context):
                        raise AssertionError("non-accept request must not execute")

                events, calls, _ = self.run_controlled(
                    ToolThenAnswerProvider(), policy=DecisionPolicy()
                )
                failure = next(
                    event
                    for event in events
                    if event.event_type == "ToolValidationFailed"
                )
                self.assertEqual(failure.data["details"]["status"], status)
                self.assertEqual(calls, [])
                self.assertEqual(events[-1].event_type, terminal)

    def test_unified_accept_normalizes_input_and_output(self) -> None:
        class ToolThenAnswerProvider:
            async def complete(self, request, *, operation_id):
                if request.messages[-1].role == "tool":
                    return ModelResponse(ModelMessage("assistant", "done"))
                return ModelResponse(
                    ModelMessage("assistant", "checking"),
                    (ToolCall("weather-1", "get_weather", {"city": "untrusted"}),),
                )

        class NormalizingPolicy:
            async def validate_input(self, call, context):
                assert dict(context.workflow_state) == {"answer": None}
                return ValidationDecision(
                    "accept",
                    evidence={"source": "trusted-geocoder"},
                    normalized_value={"city": "Batumi"},
                )

            async def validate_transition(self, call, context):
                return ValidationDecision(
                    "accept", evidence={"guard": "weather-read-allowed"}
                )

            async def capture_pre_state(self, call, context):
                return {"workflow_state": context.workflow_state}

            async def validate_output(self, call, result, evidence, context):
                assert (
                    evidence["validation"]["transition"]["guard"]
                    == "weather-read-allowed"
                )
                return ValidationDecision(
                    "accept",
                    evidence={"checked": True},
                    normalized_value={"city": result["city"], "verified": True},
                )

        events, calls, _ = self.run_controlled(
            ToolThenAnswerProvider(), policy=NormalizingPolicy()
        )
        self.assertEqual(calls, ["Batumi"])
        succeeded = next(
            event for event in events if event.event_type == "ToolCallSucceeded"
        )
        self.assertEqual(
            dict(succeeded.data["result"]), {"city": "Batumi", "verified": True}
        )
        request_evidence = next(
            event
            for event in events
            if event.event_type == "ToolValidationSucceeded"
            and event.data["phase"] == "request"
        )
        self.assertEqual(
            dict(request_evidence.data["evidence"]["pre_state"]["workflow_state"]),
            {"answer": None},
        )
        self.assertEqual(events[-1].event_type, "RunCompleted")


class ToolThenFinalAnswerProvider:
    async def complete(self, request, *, operation_id):
        if request.messages[-1].role == "tool":
            return ModelResponse(ModelMessage("assistant", "done"))
        return ModelResponse(
            ModelMessage("assistant", "checking"),
            (ToolCall("weather-1", "get_weather", {"city": "Tbilisi"}),),
        )


class RepeatingToolProvider:
    async def complete(self, request, *, operation_id):
        return ModelResponse(
            ModelMessage("assistant", "checking"),
            (ToolCall("weather-loop", "get_weather", {"city": "Tbilisi"}),),
        )


def run_control_restart(
    provider_factory,
    *,
    goal_satisfied=None,
    workflow_invariant=None,
    max_state_visits=2,
    restart_after_every_dispatch,
):
    """Drive a control-gated run to termination, rebuilding the runtime and
    dispatchers from the persisted store around every reaction/effect
    boundary when restart_after_every_dispatch is set."""
    calls = []

    def tracked_weather(city: str) -> dict:
        calls.append(city)
        return {"city": city, "temperature": 23}

    tracked_weather.__name__ = "get_weather"

    def build():
        tools = ToolRegistry.from_functions(tracked_weather)
        agent, loop = define(
            tools,
            snapshot_state=lambda state: {"answer": state.answer},
            goal_satisfied=goal_satisfied,
            workflow_invariant=workflow_invariant,
            limits=ModelLoopLimits(
                max_model_steps=6, max_tool_calls=6, max_state_visits=max_state_visits
            ),
        )
        context = {"model": provider_factory(), "tools": tools}
        return agent, loop, context

    store = InMemoryEventStore()
    stream_id = run_stream_id("assistant", "v04-control-restart")
    agent, _, _ = build()
    run(
        store.append(
            stream_id,
            -1,
            (
                Event("RunCreated", {"agent": "assistant", "definition_version": "1"}),
                agent.handle_command("message", {"text": "weather"})[0],
            ),
        )
    )

    def runtime_pair():
        fresh_agent, fresh_loop, context = build()
        runtime = fresh_agent.build_runtime(context=context)
        return (
            fresh_loop,
            DurableDispatcher(
                agent=runtime.agent,
                store=store,
                subscription_name="v04:control-restart:reactions",
            ),
            DurableEffectDispatcher(
                agent=runtime.agent,
                store=store,
                effects=runtime.effects,
                context=runtime.context,
                subscription_name="v04:control-restart:effects",
            ),
        )

    loop, reactions, effects = runtime_pair()
    for _ in range(60):
        progressed = run(reactions.run_once())
        if restart_after_every_dispatch:
            loop, reactions, effects = runtime_pair()
        progressed |= run(effects.run_once())
        if restart_after_every_dispatch:
            loop, reactions, effects = runtime_pair()
        history = run(store.load(stream_id))
        if history[-1].event.event_type in {"RunCompleted", "RunFailed", "RunAbstained"}:
            return history, tuple(calls), loop
        if not progressed:
            continue
    raise AssertionError("v0.4 control-restart run did not terminate")


def assert_control_restart_equivalent(
    testcase,
    provider_factory,
    *,
    goal_satisfied=None,
    workflow_invariant=None,
    max_state_visits=2,
):
    normal, normal_calls, _ = run_control_restart(
        provider_factory,
        goal_satisfied=goal_satisfied,
        workflow_invariant=workflow_invariant,
        max_state_visits=max_state_visits,
        restart_after_every_dispatch=False,
    )
    restarted, restarted_calls, _ = run_control_restart(
        provider_factory,
        goal_satisfied=goal_satisfied,
        workflow_invariant=workflow_invariant,
        max_state_visits=max_state_visits,
        restart_after_every_dispatch=True,
    )
    testcase.assertEqual(normalize_history(normal), normalize_history(restarted))
    testcase.assertEqual(normal_calls, restarted_calls)
    return restarted, restarted_calls


class V04ControlRestartEquivalenceTests(unittest.TestCase):
    """Restart-boundary coverage for the three negative control terminals
    that assert_restart_equivalent/execute() above do not exercise."""

    def test_goal_not_satisfied_is_restart_equivalent_and_terminal(self) -> None:
        history, calls = assert_control_restart_equivalent(
            self,
            ToolThenFinalAnswerProvider,
            goal_satisfied=lambda state: False,
        )
        types = tuple(item.event.event_type for item in history)
        self.assertEqual(calls, ("Tbilisi",))
        self.assertIn("GoalNotSatisfied", types)
        self.assertNotIn("RunCompleted", types)
        self.assertEqual(sum(t == "ToolCallSucceeded" for t in types), 1)
        self.assertEqual(
            sum(t in {"RunCompleted", "RunFailed", "RunAbstained"} for t in types), 1
        )
        self.assertEqual(types[-1], "RunFailed")

    def test_workflow_invariant_violation_is_restart_equivalent_and_terminal(
        self,
    ) -> None:
        history, calls = assert_control_restart_equivalent(
            self,
            ToolThenFinalAnswerProvider,
            workflow_invariant=lambda state: "required evidence is missing",
            goal_satisfied=lambda state: True,
        )
        types = tuple(item.event.event_type for item in history)
        self.assertEqual(calls, ("Tbilisi",))
        self.assertIn("WorkflowInvariantViolated", types)
        self.assertNotIn("GoalSatisfied", types)
        self.assertNotIn("RunCompleted", types)
        self.assertEqual(sum(t == "ToolCallSucceeded" for t in types), 1)
        self.assertEqual(
            sum(t in {"RunCompleted", "RunFailed", "RunAbstained"} for t in types), 1
        )
        self.assertEqual(types[-1], "RunFailed")

    def test_workflow_cycle_detected_is_restart_equivalent_and_terminal(self) -> None:
        history, calls = assert_control_restart_equivalent(
            self,
            RepeatingToolProvider,
            max_state_visits=1,
        )
        types = tuple(item.event.event_type for item in history)
        self.assertEqual(calls, ("Tbilisi",))
        self.assertIn("WorkflowCycleDetected", types)
        self.assertNotIn("RunCompleted", types)
        self.assertEqual(sum(t == "ToolCallSucceeded" for t in types), 1)
        self.assertLess(
            types.index("WorkflowCycleDetected"), types.index("RunFailed")
        )
        self.assertEqual(
            sum(t in {"RunCompleted", "RunFailed", "RunAbstained"} for t in types), 1
        )
        self.assertEqual(types[-1], "RunFailed")


if __name__ == "__main__":
    unittest.main()
