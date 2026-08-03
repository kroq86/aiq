"""Executable acceptance scenario for the 0.2 single-tool policy."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, replace
from functools import partial

from aiq import (
    AgentDefinition,
    DurableDispatcher,
    DurableEffectDispatcher,
    EffectContext,
    EffectRegistry,
    Event,
    FunctionTool,
    InMemoryEventStore,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    agent_owns_stream,
    run_stream_id,
)
from aiq.llm import (
    MODEL_CALL_FAILED,
    MODEL_CALL_REJECTED,
    MODEL_CALL_REQUESTED,
    MODEL_CALL_SUCCEEDED,
    MODEL_OUTPUT_REJECTED,
    TOOL_CALL_FAILED,
    TOOL_CALL_REJECTED,
    TOOL_CALL_REQUESTED,
    TOOL_CALL_SUCCEEDED,
    execute_model_call,
    execute_tool_call,
    read_model_response,
    request_model_call,
    request_tool_call,
    single_tool_call,
    tool_result_message,
)


def run(coro):
    return asyncio.run(coro)


@dataclass(frozen=True)
class State:
    messages: tuple[ModelMessage, ...] = ()
    model_steps: int = 0
    tool_calls: int = 0
    pending_tool_call_id: str | None = None
    completed_tool_call_ids: tuple[str, ...] = ()
    answer: str | None = None
    failure_reason: str | None = None


WEATHER = ToolDefinition(
    "weather",
    "Current weather",
    {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
)


class StatelessProvider:
    """The persisted request, not provider memory, selects the next step."""

    async def complete(self, request, *, operation_id):
        del operation_id
        if request.messages[-1].role == "tool":
            return ModelResponse(ModelMessage("assistant", "It is 23 C in Tbilisi."))
        return ModelResponse(
            ModelMessage("assistant", "I will check."),
            (ToolCall("weather-1", "weather", {"city": "Tbilisi"}),),
        )


def build_runtime(store, executions):
    agent = AgentDefinition(
        "assistant",
        initial_state=State,
        terminal_event_types={"RunCompleted", "RunFailed"},
        definition_version="0.2",
    )

    @agent.reducer
    def reduce(state: State, event: Event) -> State:
        if event.event_type == "UserMessageAdded":
            return replace(
                state,
                messages=state.messages + (ModelMessage("user", str(event.data["text"])),),
            )
        if event.event_type == MODEL_CALL_REQUESTED:
            return replace(state, model_steps=int(event.data["model_step"]))
        if event.event_type == MODEL_CALL_SUCCEEDED:
            response = read_model_response(event)
            return replace(state, messages=state.messages + (response.message,))
        if event.event_type == TOOL_CALL_REQUESTED:
            call = event.data["tool_call"]
            return replace(
                state,
                tool_calls=state.tool_calls + 1,
                pending_tool_call_id=str(call["call_id"]),
            )
        if event.event_type == TOOL_CALL_SUCCEEDED:
            return replace(
                state,
                messages=state.messages + (tool_result_message(event),),
                pending_tool_call_id=None,
                completed_tool_call_ids=(
                    state.completed_tool_call_ids + (str(event.data["call_id"]),)
                ),
            )
        if event.event_type == "AnswerProduced":
            return replace(state, answer=str(event.data["text"]))
        if event.event_type in {
            MODEL_CALL_REJECTED,
            MODEL_CALL_FAILED,
            MODEL_OUTPUT_REJECTED,
            TOOL_CALL_REJECTED,
            TOOL_CALL_FAILED,
            "AgentStepLimitExceeded",
        }:
            return replace(state, failure_reason=str(event.data["reason"]))
        return state

    def next_model_request(event: Event, state: State):
        if state.model_steps >= 3:
            return (
                Event(
                    "AgentStepLimitExceeded",
                    {"reason": "maximum model steps exceeded"},
                    {"causation_id": str(event.event_id)},
                ),
            )
        return (
            request_model_call(
                ModelRequest(state.messages, (WEATHER,)),
                model_step=state.model_steps + 1,
                causation_id=str(event.event_id),
            ),
        )

    agent.react("UserMessageAdded")(next_model_request)
    agent.react(TOOL_CALL_SUCCEEDED)(next_model_request)

    @agent.react(MODEL_CALL_SUCCEEDED)
    def interpret_model(event: Event, state: State):
        del state
        try:
            response = read_model_response(event)
            call = single_tool_call(response)
        except ValueError as error:
            return (
                Event(
                    MODEL_OUTPUT_REJECTED,
                    {"reason": str(error)},
                    {"causation_id": str(event.event_id)},
                ),
            )
        if call is not None:
            return (request_tool_call(call, causation_id=str(event.event_id)),)
        return (
            Event(
                "AnswerProduced",
                {"text": response.message.content},
                {"causation_id": str(event.event_id)},
            ),
            Event("RunCompleted", {}, {"causation_id": str(event.event_id)}),
        )

    for failed_type in (
        MODEL_CALL_REJECTED,
        MODEL_CALL_FAILED,
        MODEL_OUTPUT_REJECTED,
        TOOL_CALL_REJECTED,
        TOOL_CALL_FAILED,
        "AgentStepLimitExceeded",
    ):
        agent.react(failed_type)(
            lambda event, state: (
                Event(
                    "RunFailed",
                    {"reason": str(event.data["reason"])},
                    {"causation_id": str(event.event_id)},
                ),
            )
        )

    registry = ToolRegistry()

    async def weather(city):
        executions.append(city)
        return {"city": city, "temperature": 23, "unit": "C"}

    registry.register(FunctionTool(WEATHER, weather))
    effects = EffectRegistry()

    @effects.effect(MODEL_CALL_REQUESTED)
    async def model_effect(event, state, context):
        del state
        return await execute_model_call(event, context.require("provider"))

    @effects.effect(TOOL_CALL_REQUESTED)
    async def tool_effect(event, state, context):
        del state
        return await execute_tool_call(event, context.require("tools"))

    owner = partial(agent_owns_stream, "assistant")
    return (
        agent,
        DurableDispatcher(
            agent=agent,
            store=store,
            subscription_name="assistant:0.2:reactions",
            owns_stream=owner,
        ),
        DurableEffectDispatcher(
            agent=agent,
            store=store,
            effects=effects,
            context=EffectContext({"provider": StatelessProvider(), "tools": registry}),
            subscription_name="assistant:0.2:effects",
            owns_stream=owner,
        ),
    )


class DurableSingleToolAcceptanceTests(unittest.TestCase):
    def test_restart_after_committed_tool_result_resumes_without_reexecution(self) -> None:
        store = InMemoryEventStore()
        executions = []
        stream_id = run_stream_id("assistant", "restart")
        run(
            store.append(
                stream_id,
                -1,
                (
                    Event(
                        "RunCreated",
                        {"agent": "assistant", "definition_version": "0.2"},
                    ),
                    Event("UserMessageAdded", {"text": "weather"}),
                ),
            )
        )
        agent, reactions, effects = build_runtime(store, executions)

        # Drive through the first committed tool result and then discard all
        # runtime/provider/registry objects to model a process restart.
        for _ in range(20):
            run(reactions.run_once())
            run(effects.run_once())
            history = run(store.load(stream_id))
            if history[-1].event.event_type == TOOL_CALL_SUCCEEDED:
                break
        else:
            self.fail("tool result was not committed")
        self.assertEqual(executions, ["Tbilisi"])

        del agent, reactions, effects
        agent, reactions, effects = build_runtime(store, executions)
        for _ in range(20):
            progressed = run(reactions.run_once()) | run(effects.run_once())
            if not progressed:
                break

        history = run(store.load(stream_id))
        event_types = [item.event.event_type for item in history]
        self.assertEqual(
            event_types,
            [
                "RunCreated",
                "UserMessageAdded",
                MODEL_CALL_REQUESTED,
                MODEL_CALL_SUCCEEDED,
                TOOL_CALL_REQUESTED,
                TOOL_CALL_SUCCEEDED,
                MODEL_CALL_REQUESTED,
                MODEL_CALL_SUCCEEDED,
                "AnswerProduced",
                "RunCompleted",
            ],
        )
        self.assertEqual(executions, ["Tbilisi"])
        self.assertEqual(agent.rebuild(history).answer, "It is 23 C in Tbilisi.")
        self.assertEqual(sum(name == "RunCompleted" for name in event_types), 1)

        by_type = {item.event.event_type: item.event for item in history}
        self.assertEqual(
            by_type[TOOL_CALL_REQUESTED].metadata["causation_id"],
            str(history[3].event.event_id),
        )
        self.assertEqual(
            history[5].event.metadata["causation_id"],
            str(history[4].event.event_id),
        )


if __name__ == "__main__":
    unittest.main()
