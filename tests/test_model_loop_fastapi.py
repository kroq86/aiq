from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from agentlog import OllamaProvider, SQLiteEventStore, ToolRegistry
from agentlog.fastapi import AgentlogApplication
from tests.test_model_loop_policy import define, get_weather, run


def collect_events(response, *, stop_at: str) -> list[str]:
    event_types = []
    for line in response.iter_lines():
        if line.startswith("event: "):
            event_type = line.removeprefix("event: ")
            event_types.append(event_type)
            if event_type == stop_at:
                return event_types
    return event_types


def ollama_client(*, block_continuation: bool) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["messages"][-1]["role"] == "tool":
            if block_continuation:
                await asyncio.Event().wait()
            return httpx.Response(
                200, json={"message": {"role": "assistant", "content": "23 C"}}
            )
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "Checking",
                    "tool_calls": [
                        {
                            "id": "weather-1",
                            "function": {
                                "name": "get_weather",
                                "arguments": {"city": "Tbilisi"},
                            },
                        }
                    ],
                }
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class ModelLoopFastAPIAcceptanceTests(unittest.TestCase):
    def test_http_tool_loop_resumes_after_application_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = run(SQLiteEventStore.open(Path(directory) / "model-loop.db"))
            tools = ToolRegistry.from_functions(get_weather)
            first_agent, first_loop = define(tools)
            first_client = ollama_client(block_continuation=True)
            first_provider = OllamaProvider(first_client, model="llama")
            first = AgentlogApplication(
                store=store,
                poll_interval_seconds=0.01,
                shutdown_timeout_seconds=0.05,
            )
            first.register(
                first_agent,
                resources={"model": first_provider, "tools": tools},
            )
            first_app = FastAPI(lifespan=first.lifespan)
            first_app.include_router(first.router)

            with TestClient(first_app) as client:
                run_id = client.post("/agents/assistant/runs").json()["run_id"]
                response = client.post(
                    f"/agents/assistant/runs/{run_id}/commands/message",
                    json={"text": "weather"},
                )
                self.assertEqual(response.status_code, 200)
                for _ in range(200):
                    history = run(store.load(f"assistant:{run_id}"))
                    before_restart = [item.event.event_type for item in history]
                    if first_loop.events.ToolCallSucceeded.__name__ in before_restart:
                        break
                    time.sleep(0.01)
                else:
                    self.fail("tool result was not persisted through FastAPI application")
                self.assertNotIn(first_loop.events.RunCompleted.__name__, before_restart)
            run(first_client.aclose())

            fresh_tools = ToolRegistry.from_functions(get_weather)
            fresh_agent, fresh_loop = define(fresh_tools)
            second_client = ollama_client(block_continuation=False)
            second_provider = OllamaProvider(second_client, model="llama")
            second = AgentlogApplication(store=store, poll_interval_seconds=0.01)
            second.register(
                fresh_agent,
                resources={"model": second_provider, "tools": fresh_tools},
            )
            second_app = FastAPI(lifespan=second.lifespan)
            second_app.include_router(second.router)

            with TestClient(second_app) as client:
                with client.stream(
                    "GET", f"/agents/assistant/runs/{run_id}/stream"
                ) as stream:
                    after_restart = collect_events(
                        stream, stop_at=fresh_loop.events.RunCompleted.__name__
                    )
                self.assertEqual(after_restart[-1], fresh_loop.events.RunCompleted.__name__)
                state = client.get(f"/agents/assistant/runs/{run_id}").json()["state"]
                self.assertEqual(state["answer"], "23 C")
                trace = client.get(
                    f"/agents/assistant/runs/{run_id}/trace"
                ).json()
                self.assertEqual(trace["terminal_status"], "completed")
                self.assertGreaterEqual(len(trace["edges"]), 5)
            run(second_client.aclose())


if __name__ == "__main__":
    unittest.main()
