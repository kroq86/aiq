"""Real-LLM integration reference: the same declarative Agent API as
examples/support_agent, but the effect handler makes a real HTTP call to a
local Ollama instance instead of a deterministic fake. This is what proves
(or disproves) that the durable-effect contract survives a genuinely slow,
non-deterministic external call -- not just an instant fake one.

Requires a local Ollama with a small chat model pulled, e.g.:

    ollama pull llama3.2:1b

Run:

    python examples/ollama_chat_agent/main.py ollama-chat.db --model llama3.2:1b
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from aiq import Agent, CommandRejected, EffectFailed, SQLiteEventStore
from aiq.fastapi import AIQApplication

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class ChatState:
    messages: tuple[Message, ...] = ()
    failure_reason: str | None = None


@dataclass(frozen=True)
class OllamaContext:
    model: str
    client: httpx.AsyncClient


def define_agent() -> Agent:
    agent = Agent(name="ollama-chat", version="1", initial_state=ChatState())

    @agent.event
    @dataclass(frozen=True)
    class UserMessageAdded:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class ModelCallRequested:
        # Flattened, JSON-plain history snapshot at request time -- an
        # effect_request event must be immutable JSON, not carry live
        # Message objects.
        history: tuple[tuple[str, str], ...]

    @agent.event
    @dataclass(frozen=True)
    class ModelCallSucceeded:
        content: str

    @agent.event
    @dataclass(frozen=True)
    class RunCompleted:
        pass

    @agent.event
    @dataclass(frozen=True)
    class RunFailed:
        reason: str

    @agent.reduce(UserMessageAdded)
    def add_user_message(state: ChatState, event: UserMessageAdded) -> ChatState:
        return replace(state, messages=state.messages + (Message("user", event.text),))

    @agent.reduce(ModelCallSucceeded)
    def add_assistant_message(state: ChatState, event: ModelCallSucceeded) -> ChatState:
        return replace(state, messages=state.messages + (Message("assistant", event.content),))

    @agent.reduce(RunFailed)
    def add_failure(state: ChatState, event: RunFailed) -> ChatState:
        return replace(state, failure_reason=event.reason)

    @agent.react(UserMessageAdded)
    def request_model(state: ChatState, event: UserMessageAdded) -> "ModelCallRequested":
        return ModelCallRequested(
            history=tuple((m.role, m.content) for m in state.messages)
        )

    @agent.effect(ModelCallRequested)
    async def call_ollama(effect: ModelCallRequested, context: OllamaContext) -> ModelCallSucceeded:
        try:
            response = await context.client.post(
                OLLAMA_URL,
                json={
                    "model": context.model,
                    "messages": [
                        {"role": role, "content": content}
                        for role, content in effect.history
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as error:
            raise EffectFailed(f"Ollama request failed: {error}") from error

        content = payload.get("message", {}).get("content")
        if not content:
            raise EffectFailed("Ollama returned an empty response")
        return ModelCallSucceeded(content=content)

    @agent.react(ModelCallSucceeded)
    def finish(state: ChatState, event: ModelCallSucceeded) -> "RunCompleted":
        return RunCompleted()

    @agent.react(agent.event_type("EffectFailed"))
    def on_effect_failed(state: ChatState, event):
        return RunFailed(reason=event.reason)

    @agent.command("message")
    def message(payload: dict | None) -> UserMessageAdded:
        text = (payload or {}).get("text", "").strip()
        if not text:
            raise CommandRejected("Message must not be empty")
        return UserMessageAdded(text=text)

    agent.terminal(RunCompleted, status="completed")
    agent.terminal(RunFailed, status="failed")

    return agent


async def build_app(database: Path, *, model: str, request_timeout: float) -> FastAPI:
    store = await SQLiteEventStore.open(database)
    application = AIQApplication(store=store, poll_interval_seconds=0.2)
    client = httpx.AsyncClient(timeout=request_timeout)
    application.register(define_agent(), context=OllamaContext(model=model, client=client))

    app = FastAPI(lifespan=application.lifespan)
    app.include_router(application.router)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, nargs="?", default=Path("ollama-chat.db"))
    parser.add_argument("--model", default="llama3.2:1b")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=120.0)
    arguments = parser.parse_args()

    app = asyncio.run(
        build_app(arguments.database, model=arguments.model, request_timeout=arguments.timeout)
    )
    uvicorn.run(app, host="127.0.0.1", port=arguments.port)
