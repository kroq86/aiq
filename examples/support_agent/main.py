"""The one reference app for Agentlog's public API -- run it, kill it,
restart it, and see the run continue and its causal trace, without opening
`agentlog/runtime.py`.

    python examples/support_agent/main.py support-agent.db

Only imports from the public surface documented in README.md's "Public
API" section:

    from agentlog import Agent, CommandRejected, EffectFailed, SQLiteEventStore
    from agentlog.fastapi import AgentlogApplication

See examples/support_agent/README.md for the exact create/command/kill/
restart/trace walkthrough.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from agentlog import Agent, CommandRejected, EffectFailed, SQLiteEventStore
from agentlog.fastapi import AgentlogApplication


@dataclass(frozen=True)
class SupportState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    failure_reason: str | None = None


class FakeModel:
    """Deterministic stand-in for a real LLM -- no network, no API key.
    Send the text "fail" to exercise the EffectFailed -> RunFailed path."""

    async def complete(self, text: str) -> str:
        if text.strip().lower() == "fail":
            raise RuntimeError("simulated model outage")
        return f"echo: {text}"


@dataclass(frozen=True)
class SupportContext:
    model: FakeModel


def define_agent() -> Agent:
    agent = Agent(name="support", version="1", initial_state=SupportState())

    @agent.event
    @dataclass(frozen=True)
    class UserMessageAdded:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class ModelCallRequested:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class ModelCallSucceeded:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class AnswerProduced:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class RunCompleted:
        pass

    @agent.event
    @dataclass(frozen=True)
    class RunFailed:
        reason: str

    @agent.reduce(UserMessageAdded)
    def add_message(state: SupportState, event: UserMessageAdded) -> SupportState:
        return replace(state, messages=state.messages + (event.text,))

    @agent.reduce(AnswerProduced)
    def add_answer(state: SupportState, event: AnswerProduced) -> SupportState:
        return replace(state, answer=event.text)

    @agent.reduce(RunFailed)
    def add_failure(state: SupportState, event: RunFailed) -> SupportState:
        return replace(state, failure_reason=event.reason)

    @agent.react(UserMessageAdded)
    def request_model(state: SupportState, event: UserMessageAdded) -> ModelCallRequested:
        return ModelCallRequested(text=event.text)

    @agent.effect(ModelCallRequested)
    async def call_model(effect: ModelCallRequested, context: SupportContext) -> ModelCallSucceeded:
        try:
            answer = await context.model.complete(effect.text)
        except RuntimeError as error:
            raise EffectFailed(str(error)) from error
        return ModelCallSucceeded(text=answer)

    @agent.react(ModelCallSucceeded)
    def produce_answer(state: SupportState, event: ModelCallSucceeded) -> list:
        return [AnswerProduced(text=event.text), RunCompleted()]

    @agent.react(agent.event_type("EffectFailed"))
    def on_effect_failed(state: SupportState, event):
        return RunFailed(reason=event.reason)

    @agent.command("message")
    def message(payload: dict | None) -> UserMessageAdded:
        text = (payload or {}).get("text")
        if not text:
            raise CommandRejected("text must not be empty")
        return UserMessageAdded(text=text)

    agent.terminal(RunCompleted, status="completed")
    agent.terminal(RunFailed, status="failed")

    return agent


async def build_app(database: Path) -> FastAPI:
    store = await SQLiteEventStore.open(database)
    application = AgentlogApplication(store=store, poll_interval_seconds=0.2)
    application.register(define_agent(), context=SupportContext(model=FakeModel()))

    app = FastAPI(lifespan=application.lifespan)
    app.include_router(application.router)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database",
        type=Path,
        nargs="?",
        default=Path("support-agent.db"),
        help="SQLite file -- reuse the same path across restarts to see continuation",
    )
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()

    app = asyncio.run(build_app(arguments.database))
    uvicorn.run(app, host="127.0.0.1", port=arguments.port)
