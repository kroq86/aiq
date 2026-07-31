import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

import httpx

from agentlog import (
    AgentDefinition,
    EffectContext,
    EffectRegistry,
    Event,
    SQLiteEventStore,
    effect_request,
    run_stream_id,
)
from agentlog.http import AgentRuntime, create_app


@dataclass(frozen=True)
class State:
    pass


class TraceHttpContractTests(unittest.TestCase):
    def test_trace_read_does_not_invoke_external_adapter(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                store = await SQLiteEventStore.open(
                    Path(temp_dir) / "events.db"
                )
                request = effect_request("ExternalRequested", {})
                await store.append(
                    run_stream_id("agent-a", "run-1"),
                    -1,
                    [
                        request,
                        Event(
                            "RunCompleted",
                            {},
                            {"causation_id": str(request.event_id)},
                        ),
                    ],
                )
                agent = AgentDefinition(
                    "agent-a",
                    initial_state=State,
                    terminal_event_types={"RunCompleted"},
                )

                @agent.reducer
                def evolve(state: State, event: Event) -> State:
                    return state

                effects = EffectRegistry[State]()
                calls = 0

                @effects.effect("ExternalRequested")
                async def execute(
                    event: Event,
                    state: State,
                    context: EffectContext,
                ):
                    nonlocal calls
                    calls += 1
                    return [Event("ExternalSucceeded", {})]

                app = create_app(
                    store=store,
                    runtimes={
                        "agent-a": AgentRuntime(
                            agent=agent,
                            effects=effects,
                            context=EffectContext({}),
                        )
                    },
                )
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test",
                ) as client:
                    response = await client.get(
                        "/agents/agent-a/runs/run-1/trace"
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json()["graph_kind"],
                    "domain-event-history",
                )
                self.assertEqual(calls, 0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
