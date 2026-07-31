from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path

import uvicorn

from agentlog import (
    AgentDefinition,
    EffectContext,
    EffectRegistry,
    Event,
    SQLiteEventStore,
    effect_request,
)
from agentlog.http import AgentRuntime, create_app


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    completed: bool = False


class FakeLLM:
    def __init__(self) -> None:
        self._calls = 0

    async def respond(self, messages: tuple[str, ...], operation_id: str) -> dict:
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
    async def call(self, *, tool: str, arguments: dict, operation_id: str) -> dict:
        print("MCP call:", tool, arguments, f"operation_id={operation_id}")
        return {"well_id": "A-17", "pressure": 152.4, "unit": "bar"}


def define_agent() -> AgentDefinition[ChatState]:
    agent = AgentDefinition(
        "energy-assistant",
        initial_state=ChatState,
        terminal_event_types={"RunCompleted", "RunFailed", "RunCancelled"},
    )

    @agent.reducer
    def evolve(state: ChatState, event: Event) -> ChatState:
        if event.event_type == "UserMessageAdded":
            return replace(state, messages=state.messages + (f"user:{event.data['text']}",))
        if event.event_type == "ToolCallSucceeded":
            return replace(state, messages=state.messages + (f"tool:{event.data['result']}",))
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
                    {"reason": "unsupported response type", "response_type": response["type"]},
                    {"causation_id": str(event.event_id)},
                ),
                Event(
                    "AnswerProduced",
                    {"text": "The model returned an unsupported response."},
                    {"causation_id": str(event.event_id)},
                ),
                Event("RunCompleted", {}, {"causation_id": str(event.event_id)}),
            ]
        if response["type"] == "tool_call":
            tool_call = response["tool_call"]
            if tool_call["name"] != "get_well_pressure":
                return [
                    Event(
                        "ToolCallRejected",
                        {"tool": tool_call["name"], "reason": "unknown tool"},
                        {"causation_id": str(event.event_id)},
                    ),
                    Event(
                        "AnswerProduced",
                        {"text": "The requested tool is not available."},
                        {"causation_id": str(event.event_id)},
                    ),
                    Event("RunCompleted", {}, {"causation_id": str(event.event_id)}),
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
            Event("AnswerProduced", {"text": response["text"]}, {"causation_id": str(event.event_id)}),
            Event("RunCompleted", {}, {"causation_id": str(event.event_id)}),
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
                {"call_id": event.data["call_id"], "response": response},
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
                {"call_id": event.data["call_id"], "tool": event.data["tool"], "result": result},
                {"causation_id": str(event.event_id)},
            )
        ]

    return effects


async def build_app(database: Path):
    store = await SQLiteEventStore.open(database)
    runtime = AgentRuntime(
        agent=define_agent(),
        effects=define_effects(),
        context=EffectContext({"llm": FakeLLM(), "mcp": FakeMCP()}),
    )
    return create_app(store=store, runtimes={"energy-assistant": runtime})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "database",
        type=Path,
        nargs="?",
        default=Path("agentlog-http-demo.db"),
    )
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()

    import asyncio

    app = asyncio.run(build_app(arguments.database))
    uvicorn.run(app, host="127.0.0.1", port=arguments.port)
