from __future__ import annotations

import asyncio
import os
import subprocess
import sys
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

from agentlog import (
    AgentDefinition,
    EffectContext,
    EffectRegistry,
    Event,
    InMemoryEffectAttemptStore,
    InMemoryEventStore,
)
from agentlog.fastapi import AgentRuntime, Agentlog, compose_lifespans
from agentlog.http import create_app


def _runtime(name: str = "assistant") -> AgentRuntime:
    agent = AgentDefinition(name, initial_state=lambda: ())

    @agent.reducer
    def evolve(state: tuple[str, ...], event: Event) -> tuple[str, ...]:
        return state + (event.event_type,)

    return AgentRuntime(
        agent=agent,
        effects=EffectRegistry(),
        context=EffectContext({}),
    )


class FastAPIEmbeddingContractTests(unittest.TestCase):
    def test_attempt_store_is_forwarded_only_to_effect_dispatchers(
        self,
    ) -> None:
        attempt_store = InMemoryEffectAttemptStore()
        integration = Agentlog(
            store=InMemoryEventStore(),
            runtimes={"assistant": _runtime()},
            attempt_store=attempt_store,
        )

        self.assertEqual(len(integration._effect_dispatchers), 1)
        self.assertIs(
            integration._effect_dispatchers[0]._attempt_store,
            attempt_store,
        )
        self.assertFalse(
            hasattr(integration._reaction_dispatchers[0], "_attempt_store")
        )

    def test_core_package_import_does_not_import_fastapi(self) -> None:
        script = """
import builtins
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "fastapi" or name.startswith("fastapi."):
        raise RuntimeError("FastAPI was imported transitively")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import agentlog
assert agentlog.InMemoryEventStore
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

    def test_fastapi_module_names_the_installation_extra_when_dependency_is_missing(
        self,
    ) -> None:
        script = """
import builtins
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "fastapi" or name.startswith("fastapi."):
        raise ModuleNotFoundError("blocked for contract test", name="fastapi")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
try:
    import agentlog.fastapi
except ImportError as error:
    assert "agentlog[fastapi]" in str(error), str(error)
else:
    raise AssertionError("agentlog.fastapi imported without FastAPI")
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

    def test_two_embedded_instances_with_same_agent_name_are_isolated(self) -> None:
        first_store = InMemoryEventStore()
        second_store = InMemoryEventStore()
        first = Agentlog(store=first_store, runtimes={"assistant": _runtime()})
        second = Agentlog(store=second_store, runtimes={"assistant": _runtime()})
        first_app = FastAPI(lifespan=first.lifespan)
        second_app = FastAPI(lifespan=second.lifespan)
        first_app.include_router(first.router)
        second_app.include_router(second.router)

        with TestClient(first_app) as first_client, TestClient(second_app) as second_client:
            response = first_client.post(
                "/agents/assistant/runs",
                json={"message": "only in the first store"},
            )
            self.assertEqual(response.status_code, 200)
            run_id = response.json()["run_id"]
            self.assertEqual(
                second_client.get(f"/agents/assistant/runs/{run_id}").status_code,
                404,
            )

        self.assertEqual(asyncio.run(second_store.load_global()), ())

    def test_router_can_be_embedded_under_host_prefix(self) -> None:
        integration = Agentlog(
            store=InMemoryEventStore(),
            runtimes={"assistant": _runtime()},
        )
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router, prefix="/api")

        with TestClient(app) as client:
            response = client.post(
                "/api/agents/assistant/runs",
                json={"message": "embedded"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                client.post(
                    "/agents/assistant/runs",
                    json={"message": "wrong prefix"},
                ).status_code,
                404,
            )

    def test_create_app_routes_use_the_canonical_fastapi_implementation(self) -> None:
        app = create_app(
            store=InMemoryEventStore(),
            runtimes={"assistant": _runtime()},
        )
        included_routes = [
            nested
            for route in app.routes
            for nested in getattr(getattr(route, "original_router", None), "routes", ())
        ]
        endpoint_modules = {
            route.endpoint.__module__
            for route in included_routes
            if getattr(route, "path", "").startswith("/agents/")
        }
        self.assertEqual(endpoint_modules, {"agentlog.fastapi"})

    def test_mapping_key_must_match_agent_definition_name(self) -> None:
        with self.assertRaises(ValueError):
            Agentlog(
                store=InMemoryEventStore(),
                runtimes={"public-name": _runtime("different-name")},
            )


class FastAPIEmbeddingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequential_lifespan_entries_restart_without_orphan_task(self) -> None:
        integration = Agentlog(
            store=InMemoryEventStore(),
            runtimes={},
            poll_interval_seconds=60,
        )
        app = FastAPI()

        first_task: asyncio.Task[None]
        async with integration.lifespan(app):
            first_task = integration._task  # type: ignore[assignment]
            self.assertIsNotNone(first_task)
            self.assertFalse(first_task.done())
        self.assertTrue(first_task.done())
        self.assertIsNone(integration._task)

        async with integration.lifespan(app):
            second_task = integration._task
            self.assertIsNot(first_task, second_task)
            self.assertIsNotNone(second_task)
            self.assertFalse(second_task.done())
        self.assertTrue(second_task.done())
        self.assertIsNone(integration._task)

    async def test_duplicate_start_is_rejected_without_creating_second_worker(self) -> None:
        integration = Agentlog(
            store=InMemoryEventStore(),
            runtimes={},
            poll_interval_seconds=60,
        )
        await integration.start()
        first_task = integration._task
        try:
            with self.assertRaisesRegex(RuntimeError, "already started"):
                await integration.start()
            self.assertIs(integration._task, first_task)
        finally:
            await integration.stop()
        self.assertTrue(first_task.done())

    async def test_composed_lifespan_has_deterministic_reverse_cleanup(self) -> None:
        events: list[str] = []

        @asynccontextmanager
        async def host_lifespan(app: FastAPI):
            events.append("host-start")
            try:
                yield
            finally:
                events.append("host-stop")

        integration = Agentlog(
            store=InMemoryEventStore(),
            runtimes={},
            poll_interval_seconds=60,
        )

        @asynccontextmanager
        async def observed_agentlog_lifespan(app: FastAPI):
            events.append("agentlog-start")
            async with integration.lifespan(app):
                yield
            events.append("agentlog-stop")

        app = FastAPI()
        lifespan = compose_lifespans(host_lifespan, observed_agentlog_lifespan)

        async with lifespan(app):
            events.append("request-window")
            self.assertIsNotNone(integration._task)

        self.assertEqual(
            events,
            [
                "host-start",
                "agentlog-start",
                "request-window",
                "agentlog-stop",
                "host-stop",
            ],
        )
        self.assertIsNone(integration._task)


if __name__ == "__main__":
    unittest.main()
