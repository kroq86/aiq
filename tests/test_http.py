import asyncio
import tempfile
import time
import unittest
import warnings
from dataclasses import dataclass, replace
from pathlib import Path

from starlette.exceptions import StarletteDeprecationWarning

# Scoped, not global -- see test_fastapi_embedding.py for why.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"Using `httpx` with `starlette\.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient

from aiq import (
    AgentDefinition,
    EffectContext,
    EffectRegistry,
    Event,
    SQLiteEventStore,
    effect_request,
)
from aiq.fastapi import POLL_INTERVAL_SECONDS, _Broadcaster
from aiq.http import AgentRuntime, create_app


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    completed: bool = False


class DelayedFakeLLM:
    """Sleeps briefly so the SSE stream must live-wait, not just replay a finished run."""

    def __init__(self) -> None:
        self.calls = 0

    async def respond(self, messages: tuple[str, ...], operation_id: str) -> dict:
        await asyncio.sleep(0.02)
        self.calls += 1
        if self.calls == 1:
            return {
                "type": "tool_call",
                "tool_call": {
                    "id": "call-1",
                    "name": "get_well_pressure",
                    "arguments": {"well_id": "A-17"},
                },
            }
        return {"type": "answer", "text": "done"}


class DelayedFakeMCP:
    async def call(self, *, tool: str, arguments: dict, operation_id: str) -> dict:
        await asyncio.sleep(0.02)
        return {"pressure": 1.0}


def build_chat_agent() -> AgentDefinition[ChatState]:
    agent = AgentDefinition(
        "energy-assistant",
        initial_state=ChatState,
        terminal_event_types={"RunCompleted"},
    )

    @agent.reducer
    def evolve(state: ChatState, event: Event) -> ChatState:
        if event.event_type == "UserMessageAdded":
            return replace(state, messages=state.messages + (str(event.data["text"]),))
        if event.event_type == "ToolCallSucceeded":
            return replace(state, messages=state.messages + ("tool-result",))
        if event.event_type == "AnswerProduced":
            return replace(state, answer=str(event.data["text"]))
        if event.event_type == "RunCompleted":
            return replace(state, completed=True)
        return state

    @agent.react("UserMessageAdded")
    def request_model(event: Event, state: ChatState):
        return [effect_request("ModelCallRequested", {"call_id": "m1"})]

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
        return [Event("AnswerProduced", {"text": response["text"]}), Event("RunCompleted", {})]

    @agent.react("ToolCallSucceeded")
    def continue_after_tool(event: Event, state: ChatState):
        return [effect_request("ModelCallRequested", {"call_id": "m2"})]

    return agent


def build_effects() -> EffectRegistry[ChatState]:
    effects = EffectRegistry[ChatState]()

    @effects.effect("ModelCallRequested")
    async def call_model(event: Event, state: ChatState, context: EffectContext):
        response = await context.require("llm").respond(
            state.messages,
            operation_id=str(event.event_id),
        )
        return [Event("ModelCallSucceeded", {"response": response})]

    @effects.effect("ToolCallRequested")
    async def call_tool(event: Event, state: ChatState, context: EffectContext):
        result = await context.require("mcp").call(
            tool=event.data["tool"],
            arguments=event.data["arguments"],
            operation_id=str(event.event_id),
        )
        return [Event("ToolCallSucceeded", {"result": result})]

    return effects


def build_idle_agent() -> AgentDefinition[ChatState]:
    """A second registered agent with no reactions, used to prove cross-agent runs 404."""
    agent = AgentDefinition("idle-assistant", initial_state=ChatState)

    @agent.reducer
    def evolve(state: ChatState, event: Event) -> ChatState:
        return state

    return agent


def make_app(path: Path) -> object:
    async def _build():
        store = await SQLiteEventStore.open(path)
        chat_runtime = AgentRuntime(
            agent=build_chat_agent(),
            effects=build_effects(),
            context=EffectContext({"llm": DelayedFakeLLM(), "mcp": DelayedFakeMCP()}),
            definition_version="v1",
        )
        idle_runtime = AgentRuntime(
            agent=build_idle_agent(),
            effects=EffectRegistry(),
            context=EffectContext({}),
        )
        return create_app(
            store=store,
            runtimes={
                "energy-assistant": chat_runtime,
                "idle-assistant": idle_runtime,
            },
        )

    return asyncio.run(_build())


def collect_sse_events(response, *, stop_at: str | None = None) -> list[tuple[int, str]]:
    events: list[tuple[int, str]] = []
    pending_id: int | None = None
    for line in response.iter_lines():
        if line.startswith("id: "):
            pending_id = int(line.removeprefix("id: "))
        elif line.startswith("event: "):
            event_type = line.removeprefix("event: ")
            assert pending_id is not None
            events.append((pending_id, event_type))
            if stop_at is not None and event_type == stop_at:
                return events
    return events


class HttpAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "events.db"
        self.app = make_app(self.path)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_create_run_appends_run_created_and_user_message(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            )
            self.assertEqual(response.status_code, 200)
            run_id = response.json()["run_id"]
            self.assertTrue(run_id)

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                events = collect_sse_events(stream, stop_at="UserMessageAdded")
            self.assertEqual(
                [event_type for _, event_type in events],
                ["RunCreated", "UserMessageAdded"],
            )

            trace = client.get(
                f"/agents/energy-assistant/runs/{run_id}/trace"
            ).json()
            run_created_node = trace["nodes"][0]
            self.assertEqual(run_created_node["event_type"], "RunCreated")
            self.assertEqual(
                run_created_node["data"]["definition_version"], "v1"
            )

    def test_stream_without_last_event_id_replays_full_public_history(self) -> None:
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                events = collect_sse_events(stream, stop_at="RunCompleted")

            self.assertEqual(
                [event_type for _, event_type in events],
                [
                    "RunCreated",
                    "UserMessageAdded",
                    "ModelCallRequested",
                    "ModelCallSucceeded",
                    "ToolCallRequested",
                    "ToolCallSucceeded",
                    "ModelCallRequested",
                    "ModelCallSucceeded",
                    "AnswerProduced",
                    "RunCompleted",
                ],
            )
            self.assertEqual(
                [event_id for event_id, _ in events],
                list(range(len(events))),
            )

    def test_sse_ids_are_stream_versions_when_global_positions_are_interleaved(
        self,
    ) -> None:
        async def arrange() -> None:
            store = await SQLiteEventStore.open(self.path)
            await store.append(
                "energy-assistant:interleaved",
                -1,
                [Event("RunCreated", {"agent": "energy-assistant"})],
            )
            await store.append(
                "idle-assistant:other",
                -1,
                [Event("RunCreated", {"agent": "idle-assistant"})],
            )
            await store.append(
                "energy-assistant:interleaved",
                0,
                [Event("OtherFact", {}), Event("RunCompleted", {})],
            )

        asyncio.run(arrange())

        with TestClient(self.app) as client:
            with client.stream(
                "GET",
                "/agents/energy-assistant/runs/interleaved/stream",
            ) as stream:
                full = collect_sse_events(stream)

            self.assertEqual(
                full,
                [
                    (0, "RunCreated"),
                    (1, "OtherFact"),
                    (2, "RunCompleted"),
                ],
            )

            with client.stream(
                "GET",
                "/agents/energy-assistant/runs/interleaved/stream",
                headers={"Last-Event-ID": "0"},
            ) as stream:
                tail = collect_sse_events(stream)

            self.assertEqual(
                tail,
                [(1, "OtherFact"), (2, "RunCompleted")],
            )

    def test_stream_with_last_event_id_returns_only_tail(self) -> None:
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                full = collect_sse_events(stream, stop_at="RunCompleted")

            cutoff_id, _ = full[2]  # ModelCallRequested

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
                headers={"Last-Event-ID": str(cutoff_id)},
            ) as stream:
                tail = collect_sse_events(stream, stop_at="RunCompleted")

            self.assertEqual(
                [event_type for _, event_type in tail],
                [event_type for _, event_type in full[3:]],
            )

    def test_disconnect_after_tool_call_succeeded_then_reconnect_reads_remaining_events(
        self,
    ) -> None:
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                up_to_tool_result = collect_sse_events(stream, stop_at="ToolCallSucceeded")
            # Simulated disconnect: the `with` block above closes the response here.

            last_id, _ = up_to_tool_result[-1]
            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
                headers={"Last-Event-ID": str(last_id)},
            ) as stream:
                remaining = collect_sse_events(stream, stop_at="RunCompleted")

            self.assertEqual(
                [event_type for _, event_type in remaining],
                [
                    "ModelCallRequested",
                    "ModelCallSucceeded",
                    "AnswerProduced",
                    "RunCompleted",
                ],
            )

    def test_read_run_returns_serialized_state(self) -> None:
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                collect_sse_events(stream, stop_at="RunCompleted")

            response = client.get(f"/agents/energy-assistant/runs/{run_id}")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["run_id"], run_id)
            self.assertIs(body["state"]["completed"], True)
            self.assertEqual(body["state"]["answer"], "done")

    def test_unknown_run_returns_404(self) -> None:
        with TestClient(self.app) as client:
            response = client.get("/agents/energy-assistant/runs/does-not-exist")
            self.assertEqual(response.status_code, 404)

    def test_unknown_agent_returns_404(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                "/agents/no-such-agent/runs/chat",
                json={"message": "hi"},
            )
            self.assertEqual(response.status_code, 404)

    def test_invalid_last_event_id_returns_400(self) -> None:
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            response = client.get(
                f"/agents/energy-assistant/runs/{run_id}/stream",
                headers={"Last-Event-ID": "not-an-integer"},
            )
            self.assertEqual(response.status_code, 400)

    def test_empty_message_is_rejected(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": ""},
            )
            self.assertEqual(response.status_code, 422)

    def test_run_id_under_a_different_agent_is_not_disclosed(self) -> None:
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            response = client.get(f"/agents/idle-assistant/runs/{run_id}")
            self.assertEqual(response.status_code, 404)

    def test_stream_to_an_already_completed_run_replays_and_closes_on_its_own(
        self,
    ) -> None:
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                collect_sse_events(stream, stop_at="RunCompleted")

            # Reconnect from scratch to a run that is already terminal. The
            # generator must replay full history and end on its own -- not rely
            # on the client giving up -- so we drain iter_lines() to exhaustion
            # instead of breaking early, and bound the wall-clock time so a
            # regression that falls back to the poll timeout fails loudly
            # instead of just making the suite slow.
            started = time.monotonic()
            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                events = collect_sse_events(stream)  # no stop_at: let it end itself
            elapsed = time.monotonic() - started

            self.assertEqual(events[-1][1], "RunCompleted")
            self.assertLess(elapsed, POLL_INTERVAL_SECONDS)

    def test_full_http_only_vertical_slice(self) -> None:
        """POST -> drain SSE to terminal -> GET state -> reconnect mid-stream -> tail.

        Exercises only the HTTP surface (never calling dispatchers directly) so it
        catches wiring gaps between lifespan, the catch-up task, the store, state
        projection, serialization, and SSE -- exactly the kind of bug the earlier
        `dict[str, Any]` regression slipped through because no test walked the
        full lifecycle end to end.
        """
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                full = collect_sse_events(stream, stop_at="RunCompleted")
            self.assertEqual(full[-1][1], "RunCompleted")

            state = client.get(f"/agents/energy-assistant/runs/{run_id}").json()["state"]
            self.assertIs(state["completed"], True)
            self.assertEqual(state["answer"], "done")

            mid_id, _ = full[len(full) // 2]
            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
                headers={"Last-Event-ID": str(mid_id)},
            ) as stream:
                tail = collect_sse_events(stream, stop_at="RunCompleted")

            expected_tail = [
                (event_id, event_type)
                for event_id, event_type in full
                if event_id > mid_id
            ]
            self.assertEqual(tail, expected_tail)
            self.assertEqual(tail[-1][1], "RunCompleted")


    def test_trace_endpoint_returns_full_causal_trace(self) -> None:
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                collect_sse_events(stream, stop_at="RunCompleted")

            response = client.get(f"/agents/energy-assistant/runs/{run_id}/trace")
            self.assertEqual(response.status_code, 200)
            body = response.json()

            self.assertEqual(body["agent_name"], "energy-assistant")
            self.assertEqual(body["run_id"], run_id)
            self.assertEqual(body["schema_version"], 1)
            self.assertEqual(body["graph_kind"], "domain-event-history")
            self.assertEqual(body["terminal_status"], "completed")
            self.assertEqual(
                [event["event_type"] for event in body["nodes"]],
                [
                    "RunCreated",
                    "UserMessageAdded",
                    "ModelCallRequested",
                    "ModelCallSucceeded",
                    "ToolCallRequested",
                    "ToolCallSucceeded",
                    "ModelCallRequested",
                    "ModelCallSucceeded",
                    "AnswerProduced",
                    "RunCompleted",
                ],
            )
            self.assertEqual(
                set(body),
                {
                    "schema_version",
                    "graph_kind",
                    "agent_name",
                    "run_id",
                    "terminal_status",
                    "latest_stream_version",
                    "roots",
                    "nodes",
                    "edges",
                    "timeline",
                    "dangling_causation",
                },
            )
            self.assertEqual(body["latest_stream_version"], 9)
            self.assertNotEqual(body["latest_stream_version"], len(body["nodes"]))

            # UserMessageAdded is caused by RunCreated (see create_run in
            # http.py), so at least one non-adjacency-coincidental edge exists.
            self.assertIn(
                {
                    "source_event_id": body["nodes"][0]["event_id"],
                    "target_event_id": body["nodes"][1]["event_id"],
                    "kind": "caused",
                },
                body["edges"],
            )
            for edge in body["edges"]:
                self.assertEqual(
                    set(edge), {"source_event_id", "target_event_id", "kind"}
                )
                self.assertNotIn("cause_event_id", edge)
                self.assertNotIn("effect_event_id", edge)
                self.assertEqual(edge["kind"], "caused")
            # RunCreated has no cause: it's a root. UserMessageAdded names
            # RunCreated as its cause, so it must not also show up as a root.
            self.assertIn(body["nodes"][0]["event_id"], body["roots"])
            self.assertNotIn(body["nodes"][1]["event_id"], body["roots"])
            self.assertEqual(body["dangling_causation"], [])

            # timeline: objects, not bare event_id strings, ordered by
            # stream_version (which the reference flow keeps contiguous even
            # though global_position is offset by the RunCreated/UserMessageAdded
            # pair sharing one append call -- covered non-contiguously in
            # test_trace_wire.py's synthetic fixture).
            self.assertEqual(
                body["timeline"],
                [
                    {
                        "event_id": node["event_id"],
                        "stream_version": node["stream_version"],
                        "global_position": node["global_position"],
                    }
                    for node in body["nodes"]
                ],
            )
            self.assertEqual(
                [entry["stream_version"] for entry in body["timeline"]],
                sorted(entry["stream_version"] for entry in body["timeline"]),
            )

    def test_trace_endpoint_matches_trace_to_json_directly(self) -> None:
        """The HTTP boundary must not diverge from the library-level contract:
        no separate serialization path, no HTTP-only fields."""
        from aiq.trace import TraceService, trace_to_json

        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                collect_sse_events(stream, stop_at="RunCompleted")

            http_body = client.get(
                f"/agents/energy-assistant/runs/{run_id}/trace"
            ).json()

        async def export_directly() -> dict:
            store = await SQLiteEventStore.open(self.path)
            service = TraceService(
                store=store,
                agents={"energy-assistant": build_chat_agent()},
            )
            trace = await service.export("energy-assistant", run_id)
            return trace_to_json(trace)

        direct_body = asyncio.run(export_directly())
        self.assertEqual(http_body, direct_body)

    def test_trace_endpoint_unknown_run_returns_404(self) -> None:
        with TestClient(self.app) as client:
            response = client.get("/agents/energy-assistant/runs/does-not-exist/trace")
            self.assertEqual(response.status_code, 404)

    def test_trace_endpoint_unknown_agent_returns_404(self) -> None:
        with TestClient(self.app) as client:
            response = client.get("/agents/no-such-agent/runs/whatever/trace")
            self.assertEqual(response.status_code, 404)

    def test_trace_endpoint_cross_agent_run_returns_404(self) -> None:
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            response = client.get(f"/agents/idle-assistant/runs/{run_id}/trace")
            self.assertEqual(response.status_code, 404)

    def test_trace_endpoint_reflects_persisted_history_after_reopen(self) -> None:
        with TestClient(self.app) as client:
            run_id = client.post(
                "/agents/energy-assistant/runs/chat",
                json={"message": "Pressure for A-17"},
            ).json()["run_id"]

            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
            ) as stream:
                collect_sse_events(stream, stop_at="RunCompleted")

            trace_before = client.get(
                f"/agents/energy-assistant/runs/{run_id}/trace"
            ).json()
        # `with` block above exits: this TestClient/app/lifespan is fully torn
        # down, so nothing but the SQLite file on disk carries state forward.

        reopened_app = make_app(self.path)
        with TestClient(reopened_app) as reopened_client:
            trace_after = reopened_client.get(
                f"/agents/energy-assistant/runs/{run_id}/trace"
            ).json()

        self.assertEqual(trace_before, trace_after)


class BroadcasterTests(unittest.TestCase):
    """Isolated proof that a notify() landing before wait_for_change_since()
    is called is not silently absorbed as the new baseline -- the bug the
    `wait_for_change(timeout=...)` API shape made easy to write by accident,
    since it always took its baseline snapshot at call time instead of at the
    time the caller last checked the store."""

    def test_notify_before_wait_is_registered_is_not_missed(self) -> None:
        async def scenario() -> None:
            broadcaster = _Broadcaster()
            baseline = await broadcaster.generation()
            await broadcaster.notify()  # lands in the caller's "read/send" window
            # Must return almost immediately: the generation already moved past
            # `baseline`. A regression to a call-time snapshot would instead
            # block for the full internal timeout below.
            await broadcaster.wait_for_change_since(baseline, timeout=5.0)

        asyncio.run(asyncio.wait_for(scenario(), timeout=0.5))


if __name__ == "__main__":
    unittest.main()
