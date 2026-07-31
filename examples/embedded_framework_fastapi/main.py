from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from agentlog import InMemoryEventStore
from agentlog.fastapi import AgentlogApplication
from agentlog.framework import Agent


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None


def define_agent() -> Agent:
    agent = Agent(name="assistant", initial_state=ChatState())

    @agent.event
    @dataclass(frozen=True)
    class UserMessageAdded:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class ModelCallRequested:
        prompt: str

    @agent.event
    @dataclass(frozen=True)
    class AnswerProduced:
        text: str

    @agent.reduce(UserMessageAdded)
    def add_message(state: ChatState, event: UserMessageAdded) -> ChatState:
        return replace(state, messages=state.messages + (event.text,))

    @agent.reduce(AnswerProduced)
    def add_answer(state: ChatState, event: AnswerProduced) -> ChatState:
        return replace(state, answer=event.text)

    @agent.react(UserMessageAdded)
    def request_model(state: ChatState, event: UserMessageAdded) -> ModelCallRequested:
        return ModelCallRequested(prompt=event.text)

    @agent.effect(ModelCallRequested)
    async def fake_model(
        event: ModelCallRequested,
        context: object,
    ) -> AnswerProduced:
        return AnswerProduced(text=f"answer:{event.prompt}")

    @agent.command("message")
    def message(payload: dict) -> UserMessageAdded:
        return UserMessageAdded(text=str(payload["text"]))

    return agent


def build_app() -> FastAPI:
    definition = define_agent()
    application = AgentlogApplication(
        store=InMemoryEventStore(),
        poll_interval_seconds=0.01,
    )
    application.register(definition)

    app = FastAPI(lifespan=application.lifespan)

    @app.get("/host-health")
    async def host_health() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(application.router, prefix="/api")
    app.state.agents = application
    return app


async def read_sse_history(
    app: FastAPI,
    *,
    run_id: str,
    through_event: str,
) -> list[str]:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {"type": "http", "method": "GET", "path": "/", "headers": []},
        receive,
    )
    endpoint = next(
        route.endpoint
        for route in app.state.agents.router.routes
        if route.name == "agentlog:stream_run"
    )
    response = await endpoint("assistant", run_id, request)
    iterator = response.body_iterator.__aiter__()
    event_types: list[str] = []
    try:
        while through_event not in event_types:
            chunk = await iterator.__anext__()
            for line in chunk.splitlines():
                if line.startswith("event: "):
                    event_types.append(line.removeprefix("event: "))
    finally:
        await iterator.aclose()
    return event_types


def main() -> None:
    app = build_app()
    with TestClient(app) as client:
        created = client.post(
            "/api/agents/assistant/runs",
            json={},
        )
        created.raise_for_status()
        run_id = created.json()["run_id"]

        accepted = client.post(
            f"/api/agents/assistant/runs/{run_id}/commands/message",
            json={"text": "What is the weather?"},
        )
        accepted.raise_for_status()
        acknowledgement = accepted.json()

        deadline = time.monotonic() + 2
        while True:
            state = client.get(
                f"/api/agents/assistant/runs/{run_id}"
            ).json()["state"]
            if state["answer"] is not None:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("fake effect did not finish")
            time.sleep(0.001)

        trace = client.get(f"/api/agents/assistant/runs/{run_id}/trace").json()
        assert client.get("/host-health").json() == {"ok": True}

    sse_events = asyncio.run(
        read_sse_history(
            app,
            run_id=run_id,
            through_event="AnswerProduced",
        )
    )
    trace_events = [node["event_type"] for node in trace["nodes"]]

    assert acknowledgement["accepted"] is True
    assert acknowledgement["events"][0]["event_type"] == "UserMessageAdded"
    assert state["answer"] == "answer:What is the weather?"
    assert "UserMessageAdded" in sse_events and "AnswerProduced" in sse_events
    assert "UserMessageAdded" in trace_events and "AnswerProduced" in trace_events

    print("Command acknowledgement:", acknowledgement)
    print("State:", state)
    print("SSE events:", sse_events)
    print("Trace events:", trace_events)


if __name__ == "__main__":
    main()
