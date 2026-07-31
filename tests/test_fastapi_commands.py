from __future__ import annotations

import unittest
import time
import asyncio
import os
import subprocess
import sys
import warnings
from dataclasses import dataclass, replace

from starlette.exceptions import StarletteDeprecationWarning
from starlette.requests import Request

# Scoped, not global -- see test_fastapi_embedding.py for why.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"Using `httpx` with `starlette\.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient

from agentlog import AgentDefinition, EffectContext, EffectRegistry, Event, InMemoryEventStore
from agentlog.fastapi import AgentRuntime, Agentlog, AgentlogApplication
from agentlog.framework import Agent


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None


def _framework_agent(name: str = "assistant") -> Agent:
    agent = Agent(name=name, initial_state=ChatState())

    @agent.event
    @dataclass(frozen=True)
    class MessageAdded:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class ModelRequested:
        prompt: str

    @agent.event
    @dataclass(frozen=True)
    class AnswerProduced:
        text: str

    @agent.reduce(MessageAdded)
    def add_message(state: ChatState, event: MessageAdded) -> ChatState:
        return replace(state, messages=state.messages + (event.text,))

    @agent.reduce(AnswerProduced)
    def add_answer(state: ChatState, event: AnswerProduced) -> ChatState:
        return replace(state, answer=event.text)

    @agent.react(MessageAdded)
    def request_model(state: ChatState, event: MessageAdded) -> ModelRequested:
        return ModelRequested(prompt=event.text)

    @agent.effect(ModelRequested)
    async def call_model(
        event: ModelRequested,
        context: object,
    ) -> AnswerProduced:
        return AnswerProduced(text=f"answer:{event.prompt}")

    @agent.command("message")
    def message(payload: dict) -> MessageAdded:
        return MessageAdded(text=str(payload["text"]))

    return agent


def _framework_runtime(name: str = "assistant") -> AgentRuntime:
    return _framework_agent(name).build_runtime()


def _app(store: InMemoryEventStore, runtimes: dict[str, AgentRuntime]):
    integration = Agentlog(
        store=store,
        runtimes=runtimes,
        poll_interval_seconds=0.01,
    )
    from fastapi import FastAPI

    app = FastAPI(lifespan=integration.lifespan)
    app.include_router(integration.router)
    app.state.test_agentlog = integration
    return app


def _create_run(client: TestClient, agent_name: str = "assistant") -> str:
    response = client.post(
        f"/agents/{agent_name}/runs",
        json={},
    )
    assert response.status_code == 200
    return str(response.json()["run_id"])


class _OneConflictStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.injected = False

    async def append(self, stream_id, expected_version, events):
        if (
            not self.injected
            and events
            and events[0].event_type == "MessageAdded"
        ):
            self.injected = True
            await super().append(
                stream_id,
                expected_version,
                [Event("ConcurrentFact", {})],
            )
        return await super().append(stream_id, expected_version, events)


class FastAPICommandTests(unittest.TestCase):
    def test_framework_build_runtime_imports_without_fastapi(self) -> None:
        script = """
import builtins
from dataclasses import dataclass
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "fastapi" or name.startswith("fastapi."):
        raise RuntimeError("FastAPI imported by framework core")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from agentlog.framework import Agent
agent = Agent(name="core", initial_state=lambda: None)
@agent.event
@dataclass(frozen=True)
class Created:
    pass
runtime = agent.build_runtime()
assert runtime.agent.name == "core"
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, ("src", environment.get("PYTHONPATH", "")))
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.getcwd(),
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_application_registers_two_agents_without_runtime_mapping(self) -> None:
        application = AgentlogApplication(
            store=InMemoryEventStore(),
            poll_interval_seconds=0.01,
        )
        first_agent = _framework_agent("assistant-a")
        application.register(first_agent)
        application.register(_framework_agent("assistant-b"))

        from fastapi import FastAPI

        app = FastAPI(lifespan=application.lifespan)
        app.include_router(application.router)
        with TestClient(app) as client:
            first = client.post(
                "/agents/assistant-a/runs",
                json={},
            )
            second = client.post(
                "/agents/assistant-b/runs",
                json={},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first_agent.name, "assistant-a")

    def test_application_rejects_duplicate_and_late_registration(self) -> None:
        application = AgentlogApplication(store=InMemoryEventStore())
        agent = _framework_agent("assistant")
        application.register(agent)
        with self.assertRaises(ValueError):
            application.register(agent)

        _ = application.router
        with self.assertRaises(RuntimeError):
            application.register(_framework_agent("late"))

    def test_command_append_drives_effect_state_sse_and_trace(self) -> None:
        store = InMemoryEventStore()
        app = _app(store, {"assistant": _framework_runtime()})
        with TestClient(app) as client:
            run_id = _create_run(client)
            response = client.post(
                f"/agents/assistant/runs/{run_id}/commands/message",
                json={"text": "weather"},
            )
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertTrue(body["accepted"])
            self.assertEqual(body["events"][0]["event_type"], "MessageAdded")

            deadline = time.monotonic() + 2
            while True:
                state = client.get(
                    f"/agents/assistant/runs/{run_id}"
                ).json()["state"]
                if state["answer"] is not None:
                    break
                if time.monotonic() >= deadline:
                    self.fail("effect did not update state")
                time.sleep(0.001)
            self.assertEqual(state["answer"], "answer:weather")
            trace = client.get(
                f"/agents/assistant/runs/{run_id}/trace"
            ).json()
            event_types = [node["event_type"] for node in trace["nodes"]]
            self.assertIn("MessageAdded", event_types)
            self.assertIn("AnswerProduced", event_types)
            command_node = next(
                node for node in trace["nodes"] if node["event_type"] == "MessageAdded"
            )
            self.assertEqual(command_node["correlation_id"], run_id)
            self.assertIsNone(command_node["causation_id"])
            self.assertTrue(command_node["metadata"]["command_id"])

        async def read_persisted_sse() -> list[str]:
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(
                {"type": "http", "method": "GET", "path": "/", "headers": []},
                receive,
            )
            endpoint = next(
                route.endpoint
                for route in app.state.test_agentlog.router.routes
                if route.name == "agentlog:stream_run"
            )
            response = await endpoint("assistant", run_id, request)
            seen: list[str] = []
            iterator = response.body_iterator.__aiter__()
            try:
                while "AnswerProduced" not in seen:
                    chunk = await iterator.__anext__()
                    for line in chunk.splitlines():
                        if line.startswith("event: "):
                            seen.append(line.removeprefix("event: "))
            finally:
                await iterator.aclose()
            return seen

        sse_event_types = asyncio.run(read_persisted_sse())
        self.assertIn("MessageAdded", sse_event_types)
        self.assertIn("AnswerProduced", sse_event_types)

    def test_create_run_is_generic_and_command_appends_first_domain_event(self) -> None:
        """The canonical create-run route knows nothing about any agent's
        domain events -- only `RunCreated` -- and a command is the only way
        an HTTP caller adds one."""
        with TestClient(
            _app(InMemoryEventStore(), {"assistant": _framework_runtime()})
        ) as client:
            run_id = _create_run(client)
            history = client.get(f"/agents/assistant/runs/{run_id}/trace").json()
            self.assertEqual(
                [node["event_type"] for node in history["nodes"]], ["RunCreated"]
            )

            response = client.post(
                f"/agents/assistant/runs/{run_id}/commands/message",
                json={"text": "hi"},
            )
            self.assertEqual(response.json()["events"][0]["event_type"], "MessageAdded")

    def test_unknown_command_returns_404(self) -> None:
        with TestClient(
            _app(InMemoryEventStore(), {"assistant": _framework_runtime()})
        ) as client:
            run_id = _create_run(client)
            response = client.post(
                f"/agents/assistant/runs/{run_id}/commands/missing",
                json={},
            )
            self.assertEqual(response.status_code, 404)

    def test_completed_run_rejects_command(self) -> None:
        definition = AgentDefinition(
            "assistant",
            initial_state=lambda: (),
            terminal_event_types={"RunCompleted"},
        )

        @definition.reducer
        def evolve(state, event):
            return state

        def handle_command(name, payload):
            if name == "complete":
                return (Event("RunCompleted", {}),)
            if name == "message":
                return (Event("MessageAdded", payload),)
            raise KeyError(f"unknown command: {name!r}")

        runtime = AgentRuntime(
            agent=definition,
            effects=EffectRegistry(),
            context=EffectContext({}),
            command_handler=handle_command,
        )
        store = InMemoryEventStore()
        with TestClient(_app(store, {"assistant": runtime})) as client:
            run_id = _create_run(client)
            completed = client.post(
                f"/agents/assistant/runs/{run_id}/commands/complete",
                json={},
            )
            self.assertEqual(completed.status_code, 200)
            response = client.post(
                f"/agents/assistant/runs/{run_id}/commands/message",
                json={"text": "late"},
            )
            self.assertEqual(response.status_code, 409)

    def test_cross_agent_run_is_not_disclosed(self) -> None:
        runtimes = {
            "assistant-a": _framework_runtime("assistant-a"),
            "assistant-b": _framework_runtime("assistant-b"),
        }
        with TestClient(_app(InMemoryEventStore(), runtimes)) as client:
            run_id = _create_run(client, "assistant-a")
            response = client.post(
                f"/agents/assistant-b/runs/{run_id}/commands/message",
                json={"text": "foreign"},
            )
            self.assertEqual(response.status_code, 404)

    def test_version_conflict_is_retried_with_the_same_command_events(self) -> None:
        store = _OneConflictStore()
        with TestClient(_app(store, {"assistant": _framework_runtime()})) as client:
            run_id = _create_run(client)
            response = client.post(
                f"/agents/assistant/runs/{run_id}/commands/message",
                json={"text": "retry"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(store.injected)
            self.assertEqual(response.json()["events"][0]["stream_version"], 2)


if __name__ == "__main__":
    unittest.main()
