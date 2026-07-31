"""FastAPI + Agentlog + Ollama + Python-tool reference for the 0.2 policy."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from agentlog import (
    Agent,
    CommandRejected,
    DurableModelLoop,
    ModelLoopLimits,
    ModelMessage,
    ModelRequest,
    OllamaProvider,
    SQLiteEventStore,
    ToolRegistry,
)
from agentlog.fastapi import AgentlogApplication


@dataclass(frozen=True)
class SupportState:
    answer: str | None = None
    failure_reason: str | None = None


def get_weather(city: str) -> dict:
    """Return the current demonstration weather for a city."""
    return {"city": city, "temperature": 23, "unit": "C"}


def define_agent(tools: ToolRegistry) -> Agent:
    agent = Agent(name="assistant", version="1", initial_state=SupportState)

    @agent.event
    @dataclass(frozen=True)
    class UserMessageAdded:
        text: str

    loop = DurableModelLoop(
        start_on=UserMessageAdded,
        build_request=lambda state, event, definitions: ModelRequest(
            messages=(ModelMessage("user", event.text),),
            tools=definitions,
        ),
        tool_definitions=tools.definitions(),
        provider="ollama",
        tools="default",
        limits=ModelLoopLimits(max_model_steps=8, max_tool_calls=8),
    )
    loop.install(agent)

    @agent.reduce(loop.events.AnswerProduced)
    def store_answer(state: SupportState, event) -> SupportState:
        return replace(state, answer=event.answer)

    @agent.reduce(loop.events.RunFailed)
    def store_failure(state: SupportState, event) -> SupportState:
        return replace(state, failure_reason=event.reason)

    @agent.command("message")
    def message(payload: dict | None):
        text = str((payload or {}).get("text", "")).strip()
        if not text:
            raise CommandRejected("Message must not be empty")
        return UserMessageAdded(text)

    return agent


async def build_app(database: Path, *, model: str) -> FastAPI:
    store = await SQLiteEventStore.open(database)
    tools = ToolRegistry.from_functions(get_weather)
    provider = OllamaProvider(model=model)
    application = AgentlogApplication(store=store, poll_interval_seconds=0.05)
    application.register(
        define_agent(tools),
        resources={"ollama": provider, "default": tools},
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            async with application.lifespan(app):
                yield
        finally:
            await provider.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(application.router, prefix="/api")
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, nargs="?", default=Path("model-loop.db"))
    parser.add_argument("--model", default="llama3.2:1b")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    api = asyncio.run(build_app(arguments.database, model=arguments.model))
    uvicorn.run(api, host="127.0.0.1", port=arguments.port)
