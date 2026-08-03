"""Deterministic tests for worker-failure and bounded-shutdown lifecycle
hardening. No `time.sleep` and no long real timeouts: "stuck worker"
scenarios use a controlled `asyncio.Event` the test releases explicitly,
and timeout-path tests use a small but real `shutdown_timeout_seconds`
(e.g. 0.05s) to exercise the actual `asyncio.wait_for` timeout rather than
guessing with sleeps.

Fake dispatchers are injected by replacing `AIQ._reaction_dispatchers`
/ `_effect_dispatchers` after construction -- the same kind of white-box
seam `tests/test_fastapi_embedding_contract.py` already uses (`integration._task`)
for this module, since there is no public API for injecting synthetic
dispatcher behavior and none is warranted for production use.
"""

from __future__ import annotations

import asyncio
import time
import unittest
import warnings
from contextlib import asynccontextmanager

from starlette.exceptions import StarletteDeprecationWarning

from fastapi import FastAPI

# Scoped, not global -- see test_fastapi_embedding.py for why.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"Using `httpx` with `starlette\.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient

from aiq import InMemoryEventStore
from aiq.fastapi import AIQ, compose_lifespans
from aiq.http import create_app


class _IdleDispatcher:
    """Never has anything to do -- the ordinary quiescent case."""

    async def run_once(self) -> bool:
        return False


class _RaisingDispatcher:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    async def run_once(self) -> bool:
        self.calls += 1
        raise self._error


class _HangingDispatcher:
    """Blocks on a real (but test-controlled) event until released or
    cancelled -- models a stuck effect handler awaiting something that
    never completes."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.released = asyncio.Event()

    async def run_once(self) -> bool:
        self.started.set()
        try:
            await self.released.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return False


def _bare_integration(**kwargs) -> AIQ:
    return AIQ(store=InMemoryEventStore(), runtimes={}, **kwargs)


def _with_dispatcher(integration: AIQ, dispatcher) -> None:
    integration._reaction_dispatchers = [dispatcher]
    integration._effect_dispatchers = []


class ConstructorValidationTests(unittest.TestCase):
    def test_shutdown_timeout_seconds_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            _bare_integration(shutdown_timeout_seconds=0)
        with self.assertRaises(ValueError):
            _bare_integration(shutdown_timeout_seconds=-1)

    def test_poll_interval_seconds_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            _bare_integration(poll_interval_seconds=0)
        with self.assertRaises(ValueError):
            _bare_integration(poll_interval_seconds=-1)


class BasicHealthTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_constructor_starts_no_task(self) -> None:
        integration = _bare_integration()
        self.assertIsNone(integration._task)
        self.assertEqual(integration.health.status, "stopped")
        self.assertTrue(integration.is_healthy)

    async def test_normal_start_transitions_to_running(self) -> None:
        integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(integration, _IdleDispatcher())
        await integration.start()
        try:
            self.assertEqual(integration.health.status, "running")
            self.assertTrue(integration.is_healthy)
        finally:
            await integration.stop()

    async def test_normal_stop_transitions_to_stopped(self) -> None:
        integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(integration, _IdleDispatcher())
        await integration.start()
        await integration.stop()
        self.assertEqual(integration.health.status, "stopped")
        self.assertIsNone(integration.health.worker_error)
        self.assertTrue(integration.is_healthy)

    async def test_duplicate_start_while_running_raises(self) -> None:
        integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(integration, _IdleDispatcher())
        await integration.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "already started"):
                await integration.start()
        finally:
            await integration.stop()

    async def test_stop_is_idempotent(self) -> None:
        integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(integration, _IdleDispatcher())
        await integration.start()
        await integration.stop()
        await integration.stop()  # must not raise or hang
        self.assertEqual(integration.health.status, "stopped")


class WorkerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatcher_exception_transitions_to_unhealthy(self) -> None:
        integration = _bare_integration(poll_interval_seconds=60)
        dispatcher = _RaisingDispatcher(RuntimeError("dispatcher failed"))
        _with_dispatcher(integration, dispatcher)
        await integration.start()

        for _ in range(200):
            if integration.health.status == "unhealthy":
                break
            await asyncio.sleep(0)  # yield to the worker task, not a timed wait
        else:
            self.fail("worker never reported failure")

        self.assertFalse(integration.is_healthy)
        # Class name only -- never the raw exception message (which may
        # contain secrets the application put into it).
        self.assertEqual(integration.health.worker_error, "RuntimeError")
        await integration.stop()

    async def test_worker_exception_is_observable_before_stop(self) -> None:
        """Requirement 5: the failure must be visible *before* stop() is
        ever called -- proving it isn't stop() that discovers it."""
        integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(
            integration, _RaisingDispatcher(ValueError("boom"))
        )
        await integration.start()
        for _ in range(200):
            if not integration.is_healthy:
                break
            await asyncio.sleep(0)
        else:
            self.fail("worker never reported failure")
        # stop() has not been called yet at this point.
        self.assertEqual(integration.health.status, "unhealthy")
        await integration.stop()

    async def test_start_while_unhealthy_raises_distinct_error(self) -> None:
        integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(integration, _RaisingDispatcher(RuntimeError("boom")))
        await integration.start()
        for _ in range(200):
            if not integration.is_healthy:
                break
            await asyncio.sleep(0)
        else:
            self.fail("worker never reported failure")

        with self.assertRaisesRegex(RuntimeError, "unhealthy"):
            await integration.start()

        await integration.stop()
        # After stop(), the slate is clean and start() works again.
        _with_dispatcher(integration, _IdleDispatcher())
        await integration.start()
        self.assertEqual(integration.health.status, "running")
        await integration.stop()

    async def test_stop_after_worker_failure_completes(self) -> None:
        integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(integration, _RaisingDispatcher(RuntimeError("boom")))
        await integration.start()
        for _ in range(200):
            if not integration.is_healthy:
                break
            await asyncio.sleep(0)
        else:
            self.fail("worker never reported failure")

        await asyncio.wait_for(integration.stop(), timeout=5)
        # status is "stopped" (nothing is running), but the diagnostic is
        # retained -- stop() doesn't erase it, only the next start() does.
        self.assertEqual(integration.health.status, "stopped")
        self.assertEqual(integration.health.worker_error, "RuntimeError")

    async def test_restart_after_complete_cleanup_works(self) -> None:
        integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(integration, _RaisingDispatcher(RuntimeError("boom")))
        await integration.start()
        for _ in range(200):
            if not integration.is_healthy:
                break
            await asyncio.sleep(0)
        else:
            self.fail("worker never reported failure")
        await integration.stop()

        _with_dispatcher(integration, _IdleDispatcher())
        await integration.start()
        self.assertEqual(integration.health.status, "running")
        self.assertTrue(integration.is_healthy)
        await integration.stop()
        self.assertEqual(integration.health.status, "stopped")


class ShutdownTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooperative_shutdown_completes_before_timeout(self) -> None:
        integration = _bare_integration(
            poll_interval_seconds=60, shutdown_timeout_seconds=30
        )
        _with_dispatcher(integration, _IdleDispatcher())
        await integration.start()

        started = time.monotonic()
        await integration.stop()
        elapsed = time.monotonic() - started

        self.assertEqual(integration.health.status, "stopped")
        # A cooperative worker notices stop_event immediately; must not
        # wait anywhere near the configured 30s timeout.
        self.assertLess(elapsed, 2.0)

    async def test_stuck_dispatcher_triggers_timeout_and_is_cancelled(self) -> None:
        integration = _bare_integration(
            poll_interval_seconds=60, shutdown_timeout_seconds=0.05
        )
        dispatcher = _HangingDispatcher()
        _with_dispatcher(integration, dispatcher)
        await integration.start()
        await asyncio.wait_for(dispatcher.started.wait(), timeout=5)

        started = time.monotonic()
        await asyncio.wait_for(integration.stop(), timeout=5)
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.05)
        self.assertLess(elapsed, 3.0)
        self.assertTrue(dispatcher.cancelled)
        # No AIQ task remains after forced cancellation.
        self.assertIsNone(integration._task)
        # Forced cancellation during our own shutdown is not a failure.
        self.assertEqual(integration.health.status, "stopped")
        self.assertTrue(integration.is_healthy)
        self.assertIsNone(integration.health.worker_error)

    async def test_create_app_forwards_shutdown_timeout_seconds(self) -> None:
        """Requirement 22, exercised end to end through the public
        create_app() API and a real stuck effect handler -- not by
        reaching into a private attribute. create_app() returns only a
        FastAPI app, so the app's own ASGI lifespan (the exact object
        `AIQ.lifespan` produced) is driven directly, the same way
        `asgi-lifespan`-style test helpers do, instead of going through
        TestClient's separate thread (which would require cross-thread
        asyncio.Event signalling)."""
        from aiq import AgentDefinition, EffectContext, EffectRegistry, effect_request
        from aiq.fastapi import AgentRuntime

        started = asyncio.Event()
        release = asyncio.Event()

        agent = AgentDefinition("stuck-agent", initial_state=lambda: None)

        @agent.reducer
        def evolve(state, event):
            return state

        effects = EffectRegistry()

        @effects.effect("DoSomething")
        async def handle(event, state, context):
            started.set()
            await release.wait()
            return []

        store = InMemoryEventStore()
        await store.append("stuck-agent:run-1", -1, [effect_request("DoSomething", {})])
        runtime = AgentRuntime(agent=agent, effects=effects, context=EffectContext({}))
        app = create_app(
            store=store,
            runtimes={"stuck-agent": runtime},
            shutdown_timeout_seconds=0.05,
        )

        lifespan_cm = app.router.lifespan_context(app)
        await lifespan_cm.__aenter__()
        await asyncio.wait_for(started.wait(), timeout=5)

        started_at = time.monotonic()
        await asyncio.wait_for(lifespan_cm.__aexit__(None, None, None), timeout=5)
        elapsed = time.monotonic() - started_at

        # Bounded by create_app's forwarded 0.05s, not the 10s default.
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertLess(elapsed, 3.0)


class HostCleanupSurvivesWorkerProblemsTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_cleanup_runs_after_forced_cancellation(self) -> None:
        events: list[str] = []

        @asynccontextmanager
        async def host_lifespan(app: FastAPI):
            events.append("host-start")
            try:
                yield
            finally:
                events.append("host-stop")

        integration = _bare_integration(
            poll_interval_seconds=60, shutdown_timeout_seconds=0.05
        )
        dispatcher = _HangingDispatcher()
        _with_dispatcher(integration, dispatcher)

        app = FastAPI()
        lifespan = compose_lifespans(host_lifespan, integration.lifespan)

        async with lifespan(app):
            await asyncio.wait_for(dispatcher.started.wait(), timeout=5)
            events.append("request-window")

        self.assertEqual(
            events, ["host-start", "request-window", "host-stop"]
        )
        self.assertTrue(dispatcher.cancelled)
        self.assertIsNone(integration._task)

    async def test_host_cleanup_runs_after_worker_failure(self) -> None:
        events: list[str] = []

        @asynccontextmanager
        async def host_lifespan(app: FastAPI):
            events.append("host-start")
            try:
                yield
            finally:
                events.append("host-stop")

        integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(integration, _RaisingDispatcher(RuntimeError("boom")))

        app = FastAPI()
        lifespan = compose_lifespans(host_lifespan, integration.lifespan)

        async with lifespan(app):
            for _ in range(200):
                if not integration.is_healthy:
                    break
                await asyncio.sleep(0)
            else:
                self.fail("worker never reported failure")
            events.append("request-window")

        self.assertEqual(
            events, ["host-start", "request-window", "host-stop"]
        )


class HealthEndpointTests(unittest.TestCase):
    def test_health_endpoint_reports_running(self) -> None:
        integration = AIQ(store=InMemoryEventStore(), runtimes={})
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router)

        with TestClient(app) as client:
            response = client.get("/agents/_health")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["status"], "running")
            self.assertTrue(body["healthy"])
            self.assertIsNone(body["worker_error"])

    def test_health_endpoint_returns_503_while_unhealthy_without_traceback(self) -> None:
        integration = AIQ(
            store=InMemoryEventStore(), runtimes={}, poll_interval_seconds=60
        )
        # Wire the failing dispatcher in *before* the lifespan starts the
        # worker, so it fails on the worker's very first loop iteration
        # instead of racing the (much longer) idle poll_interval_seconds.
        _with_dispatcher(
            integration,
            _RaisingDispatcher(RuntimeError("dispatcher exploded: secret_token=xyz")),
        )
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router)

        with TestClient(app) as client:

            async def wait_for_unhealthy() -> None:
                for _ in range(200):
                    if not integration.is_healthy:
                        return
                    await asyncio.sleep(0)
                raise AssertionError("worker never reported failure")

            asyncio.run(asyncio.wait_for(wait_for_unhealthy(), timeout=5))

            response = client.get("/agents/_health")
            self.assertEqual(response.status_code, 503)
            body = response.json()
            self.assertEqual(body["status"], "unhealthy")
            self.assertFalse(body["healthy"])
            self.assertIn("RuntimeError", body["worker_error"])
            # Sanitized: class name + message only, never a traceback.
            self.assertNotIn("Traceback", body["worker_error"])
            self.assertNotIn(".py", body["worker_error"])
            self.assertNotIn("line ", body["worker_error"])


class InstanceIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_instances_keep_independent_health_and_one_failure_does_not_spread(
        self,
    ) -> None:
        healthy_integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(healthy_integration, _IdleDispatcher())

        unhealthy_integration = _bare_integration(poll_interval_seconds=60)
        _with_dispatcher(
            unhealthy_integration, _RaisingDispatcher(RuntimeError("boom"))
        )

        await healthy_integration.start()
        await unhealthy_integration.start()
        try:
            for _ in range(200):
                if not unhealthy_integration.is_healthy:
                    break
                await asyncio.sleep(0)
            else:
                self.fail("worker never reported failure")

            self.assertFalse(unhealthy_integration.is_healthy)
            self.assertTrue(healthy_integration.is_healthy)
            self.assertEqual(healthy_integration.health.status, "running")
        finally:
            await healthy_integration.stop()
            await unhealthy_integration.stop()


if __name__ == "__main__":
    unittest.main()
