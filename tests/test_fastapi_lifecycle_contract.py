from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aiq import InMemoryEventStore
from aiq.fastapi import AIQ, compose_lifespans


class _ControlledFailureDispatcher:
    def __init__(self, error: Exception) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.error = error

    async def run_once(self) -> bool:
        self.entered.set()
        await self.release.wait()
        raise self.error


class _CancellableDispatcher:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run_once(self) -> bool:
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _integration(*, shutdown_timeout_seconds: float = 0.01) -> AIQ:
    return AIQ(
        store=InMemoryEventStore(),
        runtimes={},
        poll_interval_seconds=60,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )


def _install_dispatcher(integration: AIQ, dispatcher: object) -> None:
    integration._reaction_dispatchers = [dispatcher]
    integration._effect_dispatchers = []


class LifecycleFailureContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_response_does_not_expose_exception_message_secrets(
        self,
    ) -> None:
        dispatcher = _ControlledFailureDispatcher(
            RuntimeError("upstream failed; api_key=super-secret")
        )
        integration = _integration()
        _install_dispatcher(integration, dispatcher)

        await integration.start()
        await dispatcher.entered.wait()
        dispatcher.release.set()
        assert integration._task is not None
        await integration._task

        health_route = next(
            route
            for route in integration.router.routes
            if getattr(route, "path", "") == "/agents/_health"
        )
        response = await health_route.endpoint()
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body["status"], "unhealthy")
        self.assertNotIn("super-secret", response.body.decode())

        await integration.stop()

    async def test_stop_after_observed_failure_reaches_stopped_without_losing_error(
        self,
    ) -> None:
        error = RuntimeError("worker failed")
        dispatcher = _ControlledFailureDispatcher(error)
        integration = _integration()
        _install_dispatcher(integration, dispatcher)

        await integration.start()
        await dispatcher.entered.wait()
        dispatcher.release.set()
        assert integration._task is not None
        await integration._task
        self.assertEqual(integration.health.status, "unhealthy")
        self.assertIs(integration._worker_error, error)

        await integration.stop()

        self.assertEqual(integration.health.status, "stopped")
        self.assertIs(integration._worker_error, error)
        self.assertIsNone(integration._task)

    async def test_failure_racing_with_stop_is_retained_after_cleanup(self) -> None:
        error = ValueError("failure during shutdown")
        dispatcher = _ControlledFailureDispatcher(error)
        integration = _integration(shutdown_timeout_seconds=1)
        _install_dispatcher(integration, dispatcher)

        await integration.start()
        await dispatcher.entered.wait()
        stopping = asyncio.create_task(integration.stop())
        await asyncio.sleep(0)
        self.assertEqual(integration.health.status, "stopping")
        dispatcher.release.set()
        await stopping

        self.assertEqual(integration.health.status, "stopped")
        self.assertIs(integration._worker_error, error)
        self.assertIsNone(integration._task)

    async def test_forced_cancellation_is_awaited_and_stop_remains_idempotent(
        self,
    ) -> None:
        dispatcher = _CancellableDispatcher()
        integration = _integration()
        _install_dispatcher(integration, dispatcher)

        await integration.start()
        worker = integration._task
        assert worker is not None
        await dispatcher.entered.wait()
        await integration.stop()

        self.assertTrue(dispatcher.cancelled.is_set())
        self.assertTrue(worker.done())
        self.assertTrue(worker.cancelled())
        self.assertIsNone(integration._task)
        await integration.stop()
        self.assertEqual(integration.health.status, "stopped")

    async def test_concurrent_duplicate_stop_leaves_one_clean_stopped_state(
        self,
    ) -> None:
        dispatcher = _CancellableDispatcher()
        integration = _integration()
        _install_dispatcher(integration, dispatcher)

        await integration.start()
        await dispatcher.entered.wait()
        await asyncio.gather(integration.stop(), integration.stop())

        self.assertEqual(integration.health.status, "stopped")
        self.assertIsNone(integration._task)
        self.assertIsNone(integration._stop_event)
        self.assertTrue(dispatcher.cancelled.is_set())

    async def test_host_cleanup_runs_after_forced_cancellation(self) -> None:
        events: list[str] = []
        dispatcher = _CancellableDispatcher()
        integration = _integration()
        _install_dispatcher(integration, dispatcher)

        @asynccontextmanager
        async def host_lifespan(app: FastAPI):
            events.append("host-start")
            try:
                yield
            finally:
                events.append("host-stop")

        app = FastAPI()
        lifespan = compose_lifespans(host_lifespan, integration.lifespan)
        async with lifespan(app):
            await dispatcher.entered.wait()
            events.append("request-window")

        self.assertEqual(events, ["host-start", "request-window", "host-stop"])
        self.assertTrue(dispatcher.cancelled.is_set())
        self.assertIsNone(integration._task)

    async def test_two_instances_cancel_only_their_own_worker(self) -> None:
        first_dispatcher = _CancellableDispatcher()
        second_dispatcher = _CancellableDispatcher()
        first = _integration()
        second = _integration(shutdown_timeout_seconds=1)
        _install_dispatcher(first, first_dispatcher)
        _install_dispatcher(second, second_dispatcher)

        await first.start()
        await second.start()
        await first_dispatcher.entered.wait()
        await second_dispatcher.entered.wait()
        await first.stop()

        self.assertTrue(first_dispatcher.cancelled.is_set())
        self.assertFalse(second_dispatcher.cancelled.is_set())
        self.assertIsNotNone(second._task)
        self.assertFalse(second._task.done())

        await second.stop()


if __name__ == "__main__":
    unittest.main()
