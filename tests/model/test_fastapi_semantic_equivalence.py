from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentlog import InMemoryEventStore, ToolRegistry
from agentlog.fastapi import AgentlogApplication
from tests.test_model_loop_policy import Provider, define, get_weather, run

from .normalization import normalize_history
from .runtime_harness import RuntimeHarness


def collect_sse(response, *, stop_at: str) -> list[str]:
    result = []
    for line in response.iter_lines():
        if line.startswith("event: "):
            event_type = line.removeprefix("event: ")
            result.append(event_type)
            if event_type == stop_at:
                return result
    return result


class FastAPISemanticEquivalenceTests(unittest.TestCase):
    def test_http_is_a_projection_of_direct_runtime_semantics(self) -> None:
        direct = RuntimeHarness.create()
        for _ in range(30):
            direct.dispatch("reaction")
            direct.dispatch("effect")
        direct_history = normalize_history(direct.history())

        store = InMemoryEventStore()
        tools = ToolRegistry.from_functions(get_weather)
        agent, loop = define(tools)
        application = AgentlogApplication(store=store, poll_interval_seconds=0.01)
        application.register(
            agent, resources={"model": Provider(), "tools": tools}
        )
        app = FastAPI(lifespan=application.lifespan)
        app.include_router(application.router)

        with TestClient(app) as client:
            run_id = client.post("/agents/assistant/runs").json()["run_id"]
            response = client.post(
                f"/agents/assistant/runs/{run_id}/commands/message",
                json={"text": "weather"},
            )
            self.assertEqual(response.status_code, 200)
            with client.stream(
                "GET", f"/agents/assistant/runs/{run_id}/stream"
            ) as stream:
                event_types = collect_sse(
                    stream, stop_at=loop.events.RunCompleted.__name__
                )

            http_envelopes = run(store.load(f"assistant:{run_id}"))
            http_history = normalize_history(http_envelopes)
            self.assertEqual(http_history, direct_history)
            self.assertEqual(event_types, [event.event_type for event in direct_history])

            state = client.get(f"/agents/assistant/runs/{run_id}").json()["state"]
            self.assertEqual(state["answer"], direct.runtime.agent.rebuild(direct.history()).answer)
            trace = client.get(f"/agents/assistant/runs/{run_id}/trace").json()
            expected_edges = sum(event.causation is not None for event in direct_history)
            self.assertEqual(len(trace["edges"]), expected_edges)

            before_reconnect = tuple(http_envelopes)
            with client.stream(
                "GET",
                f"/agents/assistant/runs/{run_id}/stream",
                headers={"Last-Event-ID": "4"},
            ) as stream:
                replayed = collect_sse(
                    stream, stop_at=loop.events.RunCompleted.__name__
                )
            self.assertEqual(replayed, event_types[5:])
            self.assertEqual(run(store.load(f"assistant:{run_id}")), before_reconnect)


if __name__ == "__main__":
    unittest.main()
