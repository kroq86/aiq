"""Embedding-specific tests that complement tests/test_http.py (create_app)
and tests/test_fastapi_embedding_contract.py (isolation/lifecycle/prefix).

This file focuses on what those don't cover: unrelated host routes
continuing to work, host-cleanup-survives-AIQ-shutdown-failure,
SSE/trace/state through an *embedded* router (not just create_app's own
app), and double-mounting one router under two prefixes.
"""

from __future__ import annotations

import unittest
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from starlette.exceptions import StarletteDeprecationWarning

from fastapi import FastAPI

# Scoped, not global: a module-level warnings.filterwarnings(...) call would
# mutate process-wide filter state for the rest of the test run, making
# other test files' pass/fail depend on import order. catch_warnings()
# restores the filter list on exit, and the warning only ever fires once
# per process anyway (fastapi.testclient/starlette.testclient are cached in
# sys.modules after the first import).
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
    InMemoryEventStore,
    effect_request,
)
from aiq.fastapi import AIQ, AgentRuntime, compose_lifespans
from aiq.http import create_app


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    completed: bool = False


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
        if event.event_type == "AnswerProduced":
            return replace(state, answer=str(event.data["text"]))
        if event.event_type == "RunCompleted":
            return replace(state, completed=True)
        return state

    @agent.react("UserMessageAdded")
    def request_model(event: Event, state: ChatState):
        return [effect_request("ModelCallRequested", {})]

    return agent


def handle_message_command(command_name: str, payload: dict) -> tuple[Event, ...]:
    """The only way an HTTP caller now adds `UserMessageAdded`: through the
    generic command endpoint, not through create-run. Mirrors what the old
    hardcoded create-run body used to do, just moved behind a command."""
    if command_name != "message":
        raise KeyError(f"unknown command: {command_name!r}")
    return (Event("UserMessageAdded", {"text": payload["text"]}),)


def create_and_send_message(
    client: TestClient, *, agent_name: str = "energy-assistant", text: str = "hi"
) -> str:
    """Generic create-run followed by the `message` command -- replaces the
    old one-shot `POST /runs {"message": ...}` for tests that exercise the
    *embedded* router directly (not the `create_app`-only legacy endpoint)."""
    run_id = client.post(f"/agents/{agent_name}/runs").json()["run_id"]
    response = client.post(
        f"/agents/{agent_name}/runs/{run_id}/commands/message",
        json={"text": text},
    )
    assert response.status_code == 200, response.text
    return run_id


def build_effects() -> EffectRegistry[ChatState]:
    effects = EffectRegistry[ChatState]()

    @effects.effect("ModelCallRequested")
    async def call_model(event: Event, state: ChatState, context: EffectContext):
        return [
            Event("AnswerProduced", {"text": "done"}),
            Event("RunCompleted", {}),
        ]

    return effects


def build_idle_agent() -> AgentDefinition[ChatState]:
    agent = AgentDefinition("idle-assistant", initial_state=ChatState)

    @agent.reducer
    def evolve(state: ChatState, event: Event) -> ChatState:
        return state

    return agent


def make_integration() -> AIQ:
    return AIQ(
        store=InMemoryEventStore(),
        runtimes={
            "energy-assistant": AgentRuntime(
                agent=build_chat_agent(),
                effects=build_effects(),
                context=EffectContext({}),
                command_handler=handle_message_command,
            ),
            "idle-assistant": AgentRuntime(
                agent=build_idle_agent(),
                effects=EffectRegistry(),
                context=EffectContext({}),
            ),
        },
        poll_interval_seconds=0.05,
    )


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


class UnrelatedHostRoutesTests(unittest.TestCase):
    def test_host_routes_continue_working_alongside_embedded_router(self) -> None:
        """Requirement 4: the host app owns routes AIQ knows nothing
        about, and mounting AIQ must not disturb them."""
        integration = make_integration()
        app = FastAPI(lifespan=integration.lifespan)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        app.include_router(integration.router, prefix="/api")

        with TestClient(app) as client:
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            response = client.post("/api/agents/energy-assistant/runs")
            self.assertEqual(response.status_code, 200)
            # And the host route still works after AIQ handled a request.
            self.assertEqual(client.get("/health").json(), {"status": "ok"})


class BasicEmbeddingTests(unittest.TestCase):
    def test_basic_embedding_post_run_works(self) -> None:
        """Requirement 2: the exact minimal embedding shape from the spec."""
        integration = make_integration()
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router)

        with TestClient(app) as client:
            response = client.post("/agents/energy-assistant/runs")
            self.assertEqual(response.status_code, 200)
            self.assertIn("run_id", response.json())

    def test_unknown_agent_unknown_run_and_cross_agent_are_404_through_embedded_router(
        self,
    ) -> None:
        """Requirement 14, exercised through the embedded router (not
        create_app) specifically."""
        integration = make_integration()
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router)

        with TestClient(app) as client:
            run_id = client.post("/agents/energy-assistant/runs").json()["run_id"]

            self.assertEqual(
                client.get("/agents/no-such-agent/runs/whatever").status_code, 404
            )
            self.assertEqual(
                client.get("/agents/energy-assistant/runs/does-not-exist").status_code,
                404,
            )
            self.assertEqual(
                client.get(f"/agents/idle-assistant/runs/{run_id}").status_code, 404
            )
            self.assertEqual(
                client.get(f"/agents/idle-assistant/runs/{run_id}/trace").status_code,
                404,
            )


class EmbeddedRunLifecycleTests(unittest.TestCase):
    """Requirements 15-19: SSE replay, Last-Event-ID tail, completed-closes,
    /trace, and /state -- all through the *embedded* router."""

    def _client(self) -> TestClient:
        integration = make_integration()
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router)
        return TestClient(app)

    def test_sse_full_replay_through_embedded_router(self) -> None:
        with self._client() as client:
            run_id = create_and_send_message(client)

            with client.stream(
                "GET", f"/agents/energy-assistant/runs/{run_id}/stream"
            ) as stream:
                events = collect_sse_events(stream, stop_at="RunCompleted")

            self.assertEqual(
                [event_type for _, event_type in events],
                [
                    "RunCreated",
                    "UserMessageAdded",
                    "ModelCallRequested",
                    "AnswerProduced",
                    "RunCompleted",
                ],
            )

    def test_sse_last_event_id_tail_replay_through_embedded_router(self) -> None:
        with self._client() as client:
            run_id = create_and_send_message(client)

            with client.stream(
                "GET", f"/agents/energy-assistant/runs/{run_id}/stream"
            ) as stream:
                full = collect_sse_events(stream, stop_at="RunCompleted")

            cutoff_id, _ = full[0]  # RunCreated
            with client.stream(
                "GET",
                f"/agents/energy-assistant/runs/{run_id}/stream",
                headers={"Last-Event-ID": str(cutoff_id)},
            ) as stream:
                tail = collect_sse_events(stream, stop_at="RunCompleted")

            self.assertEqual(
                [event_type for _, event_type in tail],
                [event_type for _, event_type in full[1:]],
            )

    def test_completed_sse_closes_through_embedded_router(self) -> None:
        import time

        with self._client() as client:
            run_id = create_and_send_message(client)

            with client.stream(
                "GET", f"/agents/energy-assistant/runs/{run_id}/stream"
            ) as stream:
                collect_sse_events(stream, stop_at="RunCompleted")

            started = time.monotonic()
            with client.stream(
                "GET", f"/agents/energy-assistant/runs/{run_id}/stream"
            ) as stream:
                events = collect_sse_events(stream)  # no stop_at: must end itself
            elapsed = time.monotonic() - started

            self.assertEqual(events[-1][1], "RunCompleted")
            self.assertLess(elapsed, 2.0)

    def test_trace_endpoint_through_embedded_router(self) -> None:
        with self._client() as client:
            run_id = create_and_send_message(client)

            with client.stream(
                "GET", f"/agents/energy-assistant/runs/{run_id}/stream"
            ) as stream:
                collect_sse_events(stream, stop_at="RunCompleted")

            response = client.get(f"/agents/energy-assistant/runs/{run_id}/trace")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["schema_version"], 1)
            self.assertEqual(body["graph_kind"], "domain-event-history")
            self.assertEqual(body["terminal_status"], "completed")

    def test_state_endpoint_through_embedded_router(self) -> None:
        with self._client() as client:
            run_id = create_and_send_message(client)

            with client.stream(
                "GET", f"/agents/energy-assistant/runs/{run_id}/stream"
            ) as stream:
                collect_sse_events(stream, stop_at="RunCompleted")

            response = client.get(f"/agents/energy-assistant/runs/{run_id}")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["run_id"], run_id)
            self.assertIs(body["state"]["completed"], True)
            self.assertEqual(body["state"]["answer"], "done")


class InitAndRepeatedStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_starts_no_background_task(self) -> None:
        """Requirement 6, checked explicitly before any lifespan/start call."""
        integration = AIQ(store=InMemoryEventStore(), runtimes={})
        self.assertIsNone(integration._task)

    async def test_repeated_stop_is_safe(self) -> None:
        integration = AIQ(
            store=InMemoryEventStore(), runtimes={}, poll_interval_seconds=60
        )
        await integration.start()
        await integration.stop()
        # Second stop on an already-stopped instance must not raise.
        await integration.stop()
        self.assertIsNone(integration._task)

    async def test_no_task_remains_after_testclient_exits(self) -> None:
        """Requirement 9, via a real TestClient (not a raw `async with`)."""
        integration = make_integration()
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router)

        with TestClient(app) as client:
            client.post("/agents/energy-assistant/runs")
            self.assertIsNotNone(integration._task)

        self.assertIsNone(integration._task)


class HostCleanupSurvivesAIQFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_cleanup_runs_even_if_aiq_shutdown_raises(self) -> None:
        """Requirement 12: AsyncExitStack-based composition must still run
        the host's cleanup even when AIQ's own shutdown raises."""
        events: list[str] = []

        @asynccontextmanager
        async def host_lifespan(app: FastAPI):
            events.append("host-start")
            try:
                yield
            finally:
                events.append("host-stop")

        @asynccontextmanager
        async def failing_aiq_lifespan(app: FastAPI):
            events.append("aiq-start")
            try:
                yield
            finally:
                events.append("aiq-stop")
                raise RuntimeError("simulated shutdown failure")

        app = FastAPI()
        lifespan = compose_lifespans(host_lifespan, failing_aiq_lifespan)

        with self.assertRaisesRegex(RuntimeError, "simulated shutdown failure"):
            async with lifespan(app):
                events.append("request-window")

        self.assertEqual(
            events,
            ["host-start", "aiq-start", "request-window", "aiq-stop", "host-stop"],
        )


class RouteEquivalenceTests(unittest.TestCase):
    def test_create_app_and_embedded_aiq_behave_equivalently(self) -> None:
        """Requirement 21: same request against both styles, same result
        (modulo run_id), since both go through the one canonical
        implementation in aiq.fastapi. The canonical `POST /runs` is
        generic (RunCreated only) in both hosting styles -- `create_app`'s
        extra `/runs/chat` compatibility endpoint is deliberately not part
        of this equivalence, since it only exists on `create_app`'s app."""
        standalone_app = create_app(
            store=InMemoryEventStore(),
            runtimes={
                "energy-assistant": AgentRuntime(
                    agent=build_chat_agent(),
                    effects=build_effects(),
                    context=EffectContext({}),
                )
            },
        )
        integration = make_integration()
        embedded_app = FastAPI(lifespan=integration.lifespan)
        embedded_app.include_router(integration.router)

        with TestClient(standalone_app) as standalone_client, TestClient(
            embedded_app
        ) as embedded_client:
            standalone_run_id = standalone_client.post(
                "/agents/energy-assistant/runs"
            ).json()["run_id"]
            embedded_run_id = embedded_client.post(
                "/agents/energy-assistant/runs"
            ).json()["run_id"]

            # /trace (not /stream): a non-terminal run's SSE stream would
            # otherwise block waiting for a live update, which isn't this
            # test's concern -- it only needs the persisted history.
            standalone_trace = standalone_client.get(
                f"/agents/energy-assistant/runs/{standalone_run_id}/trace"
            ).json()
            embedded_trace = embedded_client.get(
                f"/agents/energy-assistant/runs/{embedded_run_id}/trace"
            ).json()
            standalone_events = [node["event_type"] for node in standalone_trace["nodes"]]
            embedded_events = [node["event_type"] for node in embedded_trace["nodes"]]

            self.assertEqual(standalone_events, embedded_events)
            self.assertEqual(standalone_events, ["RunCreated"])


class DoubleMountTests(unittest.TestCase):
    def test_same_router_mounted_twice_with_distinct_prefixes_both_work(self) -> None:
        """Requirement 23: mounting the same APIRouter object twice under
        two distinct prefixes on one app works for both."""
        integration = make_integration()
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router, prefix="/api")
        app.include_router(integration.router, prefix="/v2")

        with TestClient(app) as client:
            first = client.post("/api/agents/energy-assistant/runs")
            second = client.post("/v2/agents/energy-assistant/runs")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            # Same underlying store: both run_ids are readable from either prefix.
            first_run_id = first.json()["run_id"]
            self.assertEqual(
                client.get(f"/v2/agents/energy-assistant/runs/{first_run_id}").status_code,
                200,
            )


if __name__ == "__main__":
    unittest.main()
