from __future__ import annotations

import asyncio
import unittest

from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from aiq import (
    AgentDefinition,
    EffectContext,
    EffectRegistry,
    Event,
    InMemoryEventStore,
)
from aiq.fastapi import AgentRuntime, AIQ
from aiq.streams import run_stream_id


def _runtime(*, terminal: bool) -> AgentRuntime:
    agent = AgentDefinition(
        "assistant",
        initial_state=lambda: (),
        terminal_event_types={"RunCompleted"} if terminal else set(),
    )

    @agent.reducer
    def evolve(state: tuple[str, ...], event: Event) -> tuple[str, ...]:
        return state + (event.event_type,)

    return AgentRuntime(
        agent=agent,
        effects=EffectRegistry(),
        context=EffectContext({}),
    )


async def _request(last_event_id: str | None = None) -> Request:
    headers = []
    if last_event_id is not None:
        headers.append((b"last-event-id", last_event_id.encode()))

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
        },
        receive,
    )


def _stream_endpoint(integration: AIQ):
    return next(
        route.endpoint
        for route in integration.router.routes
        if getattr(route, "name", "") == "aiq:stream_run"
    )


class SSECursorContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_cursor_equal_latest_closes_without_waiting(self) -> None:
        store = InMemoryEventStore()
        integration = AIQ(
            store=store,
            runtimes={"assistant": _runtime(terminal=True)},
        )
        run_id = "completed"
        await store.append(
            run_stream_id("assistant", run_id),
            -1,
            [Event("RunCreated", {}), Event("RunCompleted", {})],
        )

        response = await _stream_endpoint(integration)(
            "assistant",
            run_id,
            await _request("1"),
        )
        iterator = response.body_iterator.__aiter__()
        with self.assertRaises(StopAsyncIteration):
            await iterator.__anext__()

    async def test_cursor_greater_than_latest_is_rejected(self) -> None:
        store = InMemoryEventStore()
        integration = AIQ(
            store=store,
            runtimes={"assistant": _runtime(terminal=True)},
        )
        run_id = "completed"
        await store.append(
            run_stream_id("assistant", run_id),
            -1,
            [Event("RunCreated", {}), Event("RunCompleted", {})],
        )

        with self.assertRaises(HTTPException) as caught:
            await _stream_endpoint(integration)(
                "assistant",
                run_id,
                await _request("2"),
            )
        self.assertEqual(caught.exception.status_code, 400)

    async def test_active_cursor_equal_latest_waits_then_emits_future_event(self) -> None:
        store = InMemoryEventStore()
        integration = AIQ(
            store=store,
            runtimes={"assistant": _runtime(terminal=False)},
            poll_interval_seconds=60,
        )
        run_id = "active"
        stream_id = run_stream_id("assistant", run_id)
        await store.append(stream_id, -1, [Event("RunCreated", {})])

        response = await _stream_endpoint(integration)(
            "assistant",
            run_id,
            await _request("0"),
        )
        iterator = response.body_iterator.__aiter__()
        next_event = asyncio.create_task(iterator.__anext__())
        await asyncio.sleep(0)
        self.assertFalse(next_event.done())

        await store.append(stream_id, 0, [Event("UserMessageAdded", {"text": "next"})])
        await integration._broadcaster.notify()
        rendered = await next_event
        self.assertIn("id: 1\n", rendered)
        await iterator.aclose()

    async def test_interleaved_global_positions_do_not_change_cursor_validation(
        self,
    ) -> None:
        store = InMemoryEventStore()
        integration = AIQ(
            store=store,
            runtimes={"assistant": _runtime(terminal=True)},
        )
        stream_id = run_stream_id("assistant", "target")
        await store.append(stream_id, -1, [Event("RunCreated", {})])
        await store.append("unrelated:one", -1, [Event("Other", {})])
        await store.append("unrelated:two", -1, [Event("Other", {})])
        await store.append(stream_id, 0, [Event("RunCompleted", {})])

        response = await _stream_endpoint(integration)(
            "assistant",
            "target",
            await _request("1"),
        )
        with self.assertRaises(StopAsyncIteration):
            await response.body_iterator.__aiter__().__anext__()

        with self.assertRaises(HTTPException) as caught:
            await _stream_endpoint(integration)(
                "assistant",
                "target",
                await _request("2"),
            )
        self.assertEqual(caught.exception.status_code, 400)


class OpenAPINamingContractTests(unittest.TestCase):
    def test_aiq_names_and_operation_ids_do_not_collide_with_host(self) -> None:
        integration = AIQ(
            store=InMemoryEventStore(),
            runtimes={"assistant": _runtime(terminal=False)},
        )
        app = FastAPI()

        @app.post("/host/runs", name="create_run")
        async def create_run() -> dict[str, bool]:
            return {"ok": True}

        @app.get("/host/health", name="health_check")
        async def health_check() -> dict[str, bool]:
            return {"ok": True}

        app.include_router(integration.router)
        schema = app.openapi()
        operation_ids = [
            operation["operationId"]
            for path in schema["paths"].values()
            for operation in path.values()
        ]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        self.assertEqual(
            {
                route.name
                for route in integration.router.routes
            },
            {
                "aiq:health",
                "aiq:create_run",
                "aiq:command",
                "aiq:read_run",
                "aiq:get_trace",
                "aiq:stream_run",
            },
        )


if __name__ == "__main__":
    unittest.main()
