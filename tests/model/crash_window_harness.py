from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path

from aiq import (
    DurableDispatcher,
    DurableEffectDispatcher,
    Event,
    ModelMessage,
    ModelResponse,
    SQLiteEventStore,
    ToolCall,
    ToolRegistry,
    agent_owns_stream,
    run_stream_id,
)
from aiq.evals import CrashWindowEvidence, InvocationObservation
from aiq.trace import build_causal_trace
from aiq.tools import function_tool

from tests.test_model_loop_policy import define, get_weather, run


class InjectedProcessCrash(RuntimeError):
    pass


class CrashBeforeCommitSQLiteEventStore(SQLiteEventStore):
    crash_result_type: str | None = None

    def arm(self, result_event_type: str) -> None:
        self.crash_result_type = result_event_type

    async def commit_subscription_batch(self, **kwargs):
        events = kwargs["events"]
        if self.crash_result_type is not None and any(
            event.event_type == self.crash_result_type for event in events
        ):
            self.crash_result_type = None
            raise InjectedProcessCrash("after invocation, before atomic result commit")
        return await super().commit_subscription_batch(**kwargs)


@dataclass
class RuntimeGeneration:
    agent: object
    runtime: object
    reactions: DurableDispatcher
    effects: DurableEffectDispatcher


class LoggingProvider:
    def __init__(self, generation: int, log: list[InvocationObservation]) -> None:
        self.generation = generation
        self.log = log

    async def complete(self, request, *, operation_id):
        self.log.append(InvocationObservation("model", operation_id, self.generation))
        if request.messages[-1].role == "tool":
            return ModelResponse(ModelMessage("assistant", "23 C"))
        return ModelResponse(
            ModelMessage("assistant", "Checking"),
            (ToolCall("weather-1", "get_weather", {"city": "Tbilisi"}),),
        )


class LoggingTool:
    def __init__(self, generation: int, log: list[InvocationObservation]) -> None:
        self.definition = function_tool(get_weather).definition
        self.generation = generation
        self.log = log

    async def execute(self, arguments, *, operation_id):
        self.log.append(InvocationObservation("tool", operation_id, self.generation))
        return {"city": arguments["city"], "temperature": 23}


class CrashWindowHarness:
    def __init__(self, path: Path) -> None:
        self.store = run(CrashBeforeCommitSQLiteEventStore.open(path))
        self.stream_id = run_stream_id("assistant", "crash-window")
        self.invocations: list[InvocationObservation] = []

    def build_generation(self, generation: int) -> RuntimeGeneration:
        tools = ToolRegistry()
        tools.register(LoggingTool(generation, self.invocations))
        agent, _ = define(tools)
        runtime = agent.build_runtime(
            context={
                "model": LoggingProvider(generation, self.invocations),
                "tools": tools,
            }
        )
        owner = partial(agent_owns_stream, "assistant")
        return RuntimeGeneration(
            agent,
            runtime,
            DurableDispatcher(
                agent=runtime.agent,
                store=self.store,
                subscription_name="assistant:1:reactions",
                owns_stream=owner,
            ),
            DurableEffectDispatcher(
                agent=runtime.agent,
                store=self.store,
                effects=runtime.effects,
                context=runtime.context,
                subscription_name="assistant:1:effects",
                owns_stream=owner,
            ),
        )

    def initialize(self, generation: RuntimeGeneration) -> None:
        run(
            self.store.append(
                self.stream_id,
                -1,
                (
                    Event(
                        "RunCreated", {"agent": "assistant", "definition_version": "1"}
                    ),
                    generation.agent.handle_command("message", {"text": "weather"})[0],
                ),
            )
        )

    def run_boundary(self, kind: str) -> CrashWindowEvidence:
        first = self.build_generation(1)
        self.initialize(first)
        request_type = "ModelCallRequested" if kind == "model" else "ToolCallRequested"
        result_type = "ModelCallSucceeded" if kind == "model" else "ToolCallSucceeded"

        for _ in range(30):
            run(first.reactions.run_once())
            history = run(self.store.load(self.stream_id))
            if history[-1].event.event_type == request_type:
                break
            run(first.effects.run_once())
        else:
            raise AssertionError(f"request boundary not reached: {request_type}")

        request = run(self.store.load(self.stream_id))[-1]
        self.store.arm(result_type)
        crashed = False
        for _ in range(20):
            try:
                run(first.effects.run_once())
            except InjectedProcessCrash:
                crashed = True
                break
        if not crashed:
            raise AssertionError("fault injector did not crash before commit")

        checkpoint_after_crash = run(
            self.store.load_checkpoint("assistant:1:effects")
        )
        after_crash = run(self.store.load(self.stream_id))
        if any(item.event.event_type == result_type for item in after_crash):
            raise AssertionError("result became visible despite pre-commit crash")

        fresh = self.build_generation(2)
        for _ in range(30):
            run(fresh.effects.run_once())
            history = run(self.store.load(self.stream_id))
            if any(item.event.event_type == result_type for item in history):
                break
            run(fresh.reactions.run_once())
        else:
            raise AssertionError("fresh runtime did not retry and commit result")

        history = run(self.store.load(self.stream_id))
        trace = build_causal_trace(
            agent_name="assistant",
            run_id="crash-window",
            agent=fresh.runtime.agent,
            history=history,
        )
        boundary_invocations = tuple(
            invocation
            for invocation in self.invocations
            if invocation.kind == kind
        )
        return CrashWindowEvidence(
            kind=kind,
            trace=trace,
            request_event_id=str(request.event.event_id),
            request_global_position=request.global_position,
            checkpoint_after_crash=checkpoint_after_crash,
            result_event_type=result_type,
            invocations=boundary_invocations,
        )
