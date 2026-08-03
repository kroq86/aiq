from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

from aiq import (
    AgentDefinition,
    DurableDispatcher,
    DurableEffectDispatcher,
    EffectContext,
    EffectRegistry,
    Event,
    SQLiteEventStore,
    effect_request,
    run_stream_id,
)


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    completed: bool = False


class FakeLLM:
    def __init__(self) -> None:
        self._calls = 0

    async def respond(
        self,
        messages: tuple[str, ...],
        operation_id: str,
    ) -> dict:
        print("LLM call:", f"operation_id={operation_id}")
        self._calls += 1
        if self._calls == 1:
            return {
                "type": "tool_call",
                "tool_call": {
                    "id": "pressure-A-17-24h",
                    "name": "get_well_pressure",
                    "arguments": {"well_id": "A-17", "hours": 24},
                },
            }
        return {
            "type": "answer",
            "text": f"A-17 pressure is available ({len(messages)} context messages).",
        }


class FakeMCP:
    async def call(
        self,
        *,
        tool: str,
        arguments: dict,
        operation_id: str,
    ) -> dict:
        print(
            "MCP call:",
            tool,
            arguments,
            f"operation_id={operation_id}",
        )
        return {"well_id": "A-17", "pressure": 152.4, "unit": "bar"}


def define_agent() -> AgentDefinition[ChatState]:
    agent = AgentDefinition(
        "energy-assistant",
        initial_state=ChatState,
        terminal_event_types={
            "RunCompleted",
            "RunFailed",
            "RunCancelled",
        },
    )

    @agent.reducer
    def evolve(state: ChatState, event: Event) -> ChatState:
        if event.event_type == "UserMessageAdded":
            return replace(
                state,
                messages=state.messages + (f"user:{event.data['text']}",),
            )
        if event.event_type == "ToolCallSucceeded":
            return replace(
                state,
                messages=state.messages + (f"tool:{event.data['result']}",),
            )
        if event.event_type == "AnswerProduced":
            return replace(state, answer=str(event.data["text"]))
        if event.event_type == "RunCompleted":
            return replace(state, completed=True)
        return state

    @agent.react("UserMessageAdded")
    def request_model(event: Event, state: ChatState):
        return [
            effect_request(
                "ModelCallRequested",
                {"call_id": "model-call-1"},
                {"causation_id": str(event.event_id)},
            )
        ]

    @agent.react("ModelCallSucceeded")
    def interpret_model(event: Event, state: ChatState):
        response = event.data["response"]
        if response["type"] not in {"tool_call", "answer"}:
            return [
                Event(
                    "ModelOutputRejected",
                    {
                        "reason": "unsupported response type",
                        "response_type": response["type"],
                    },
                    {"causation_id": str(event.event_id)},
                ),
                Event(
                    "AnswerProduced",
                    {"text": "The model returned an unsupported response."},
                    {"causation_id": str(event.event_id)},
                ),
                Event(
                    "RunCompleted",
                    {},
                    {"causation_id": str(event.event_id)},
                ),
            ]
        if response["type"] == "tool_call":
            tool_call = response["tool_call"]
            if tool_call["name"] != "get_well_pressure":
                return [
                    Event(
                        "ToolCallRejected",
                        {
                            "tool": tool_call["name"],
                            "reason": "unknown tool",
                        },
                        {"causation_id": str(event.event_id)},
                    ),
                    Event(
                        "AnswerProduced",
                        {"text": "The requested tool is not available."},
                        {"causation_id": str(event.event_id)},
                    ),
                    Event(
                        "RunCompleted",
                        {},
                        {"causation_id": str(event.event_id)},
                    ),
                ]
            return [
                effect_request(
                    "ToolCallRequested",
                    {
                        "call_id": tool_call["id"],
                        "tool": tool_call["name"],
                        "arguments": tool_call["arguments"],
                    },
                    {"causation_id": str(event.event_id)},
                )
            ]
        return [
            Event(
                "AnswerProduced",
                {"text": response["text"]},
                {"causation_id": str(event.event_id)},
            ),
            Event(
                "RunCompleted",
                {},
                {"causation_id": str(event.event_id)},
            ),
        ]

    @agent.react("ToolCallSucceeded")
    def continue_after_tool(event: Event, state: ChatState):
        return [
            effect_request(
                "ModelCallRequested",
                {"call_id": "model-call-2"},
                {"causation_id": str(event.event_id)},
            )
        ]

    return agent


def define_effects() -> EffectRegistry[ChatState]:
    effects = EffectRegistry[ChatState]()

    @effects.effect("ModelCallRequested")
    async def call_model(event: Event, state: ChatState, context: EffectContext):
        response = await context.require("llm").respond(
            state.messages,
            operation_id=str(event.event_id),
        )
        return [
            Event(
                "ModelCallSucceeded",
                {
                    "call_id": event.data["call_id"],
                    "response": response,
                },
                {"causation_id": str(event.event_id)},
            )
        ]

    @effects.effect("ToolCallRequested")
    async def call_tool(event: Event, state: ChatState, context: EffectContext):
        result = await context.require("mcp").call(
            tool=event.data["tool"],
            arguments=dict(event.data["arguments"]),
            operation_id=str(event.event_id),
        )
        return [
            Event(
                "ToolCallSucceeded",
                {
                    "call_id": event.data["call_id"],
                    "tool": event.data["tool"],
                    "result": result,
                },
                {"causation_id": str(event.event_id)},
            )
        ]

    return effects


async def main(database: Path) -> None:
    store = await SQLiteEventStore.open(database)
    agent = define_agent()
    reactions = DurableDispatcher(
        agent=agent,
        store=store,
        subscription_name="energy-assistant:reactions",
    )
    effects = DurableEffectDispatcher(
        agent=agent,
        store=store,
        effects=define_effects(),
        context=EffectContext({"llm": FakeLLM(), "mcp": FakeMCP()}),
        subscription_name="energy-assistant:effects",
    )

    stream_id = run_stream_id("energy-assistant", "energy-demo-run")
    if not await store.load(stream_id):
        await store.append(
            stream_id,
            -1,
            [Event("UserMessageAdded", {"text": "Pressure for A-17"})],
        )

    for _ in range(30):
        reaction_progress = await reactions.run_once()
        effect_progress = await effects.run_once()
        if not reaction_progress and not effect_progress:
            break
    else:
        raise RuntimeError("workers did not reach a stable checkpoint")

    history = await store.load(stream_id)
    for envelope in history:
        print(
            envelope.global_position,
            envelope.stream_version,
            envelope.event.event_type,
        )
    print("Final state:", agent.rebuild(history))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "database",
        type=Path,
        nargs="?",
        default=Path("aiq-chat-demo.db"),
    )
    arguments = parser.parse_args()
    asyncio.run(main(arguments.database))
