"""Demonstrates embedding AIQ into an *existing* host application --
AIQ does not own the whole app. The host has its own route (`/health`)
and its own lifespan; AIQ is mounted under `/api` and its lifespan is
composed with the host's via `compose_lifespans`, not substituted for it.

Run:

    PYTHONPATH=src python3 examples/embedded_fastapi/main.py

Then:

    curl http://127.0.0.1:8000/health
    curl -X POST http://127.0.0.1:8000/api/agents/energy-assistant/runs \\
        -H 'Content-Type: application/json' \\
        -d '{"message": "Pressure for A-17"}'
    curl http://127.0.0.1:8000/api/agents/energy-assistant/runs/<run_id>/trace

Uses fake LLM/MCP adapters only -- no network, no API keys.
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from aiq import (
    AgentDefinition,
    EffectContext,
    EffectRegistry,
    Event,
    SQLiteEventStore,
    effect_request,
)
from aiq.fastapi import AIQ, AgentRuntime, compose_lifespans


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    completed: bool = False


class FakeLLM:
    def __init__(self) -> None:
        self._calls = 0

    async def respond(self, messages: tuple[str, ...], operation_id: str) -> dict:
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
        return {"well_id": "A-17", "pressure": 152.4, "unit": "bar"}


def define_agent() -> AgentDefinition[ChatState]:
    agent = AgentDefinition(
        "energy-assistant",
        initial_state=ChatState,
        terminal_event_types={"RunCompleted"},
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
        return [effect_request("ModelCallRequested", {"call_id": "model-call-1"})]

    @agent.react("ModelCallSucceeded")
    def interpret_model(event: Event, state: ChatState):
        response = event.data["response"]
        if response["type"] == "tool_call":
            tool_call = response["tool_call"]
            return [
                effect_request(
                    "ToolCallRequested",
                    {
                        "call_id": tool_call["id"],
                        "tool": tool_call["name"],
                        "arguments": tool_call["arguments"],
                    },
                )
            ]
        return [
            Event("AnswerProduced", {"text": response["text"]}),
            Event("RunCompleted", {}),
        ]

    @agent.react("ToolCallSucceeded")
    def continue_after_tool(event: Event, state: ChatState):
        return [effect_request("ModelCallRequested", {"call_id": "model-call-2"})]

    return agent


def define_effects() -> EffectRegistry[ChatState]:
    effects = EffectRegistry[ChatState]()

    @effects.effect("ModelCallRequested")
    async def call_model(event: Event, state: ChatState, context: EffectContext):
        response = await context.require("llm").respond(
            state.messages, operation_id=str(event.event_id)
        )
        return [Event("ModelCallSucceeded", {"call_id": event.data["call_id"], "response": response})]

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
            )
        ]

    return effects


async def build_app(database: Path) -> FastAPI:
    store = await SQLiteEventStore.open(database)
    runtime = AgentRuntime(
        agent=define_agent(),
        effects=define_effects(),
        context=EffectContext({"llm": FakeLLM(), "mcp": FakeMCP()}),
    )
    aiq = AIQ(store=store, runtimes={"energy-assistant": runtime})

    # The host application's own lifespan -- proves AIQ composes with
    # an existing one instead of replacing it.
    @asynccontextmanager
    async def host_lifespan(app: FastAPI):
        print("host application starting up")
        try:
            yield
        finally:
            print("host application shutting down")

    app = FastAPI(lifespan=compose_lifespans(host_lifespan, aiq.lifespan))

    # A route that belongs entirely to the host, not to AIQ.
    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # AIQ owns everything under /api/agents/... ; the host owns the rest.
    app.include_router(aiq.router, prefix="/api")

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "database",
        type=Path,
        nargs="?",
        default=Path("aiq-embedded-demo.db"),
    )
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()

    import asyncio

    app = asyncio.run(build_app(arguments.database))
    uvicorn.run(app, host="127.0.0.1", port=arguments.port)


if __name__ == "__main__":
    main()
