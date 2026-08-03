"""End-to-end scenarios through the real FastAPI surface
(`aiq.framework.Agent` + `aiq.fastapi.AIQApplication`), not
hand-built `AgentDefinition`/`AgentRuntime` fixtures. These four scenarios
were chosen to prove the product's actual guarantees, not to maximize test
count:

1. completed happy path (create run -> command -> reaction -> effect ->
   reaction -> terminal, observed through state/SSE/trace, then a rejected
   post-terminal command);
2. EffectFailed -> RunFailed (one agent's tool failure does not take down
   a different agent's run);
3. terminal conflict (a single reaction returning two terminal event types
   in one batch is a definition bug -- TerminalEventConflictError trips
   the worker unhealthy, and neither terminal event is committed);
4. crash/restart (a run started by one generation of Agent/context/store
   objects is finished by a second, independent generation, using nothing
   but the persisted SQLite file).
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
import warnings
from dataclasses import dataclass, replace
from pathlib import Path

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

from aiq import DurableDispatcher, Event, SQLiteEventStore, run_stream_id
from aiq.fastapi import AIQApplication
from aiq.framework import Agent, CommandRejected, EffectFailed


def run(coro):
    return asyncio.run(coro)


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    failure_reason: str | None = None


def build_agent_and_context(
    *, name: str = "assistant", fail_effect: bool = False, version: str | None = None
):
    """A fresh `Agent` and a fresh resource object every call -- required
    for scenario 4, where two independent generations must share no
    Python object beyond the on-disk SQLite file."""
    agent = Agent(name=name, initial_state=ChatState(), version=version)

    @agent.event
    @dataclass(frozen=True)
    class UserMessageAdded:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class ModelCallRequested:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class ModelCallSucceeded:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class AnswerProduced:
        text: str

    @agent.event
    @dataclass(frozen=True)
    class RunCompleted:
        pass

    @agent.event
    @dataclass(frozen=True)
    class RunFailed:
        reason: str

    @agent.reduce(UserMessageAdded)
    def on_message(state, event):
        return replace(state, messages=state.messages + (event.text,))

    @agent.reduce(AnswerProduced)
    def on_answer(state, event):
        return replace(state, answer=event.text)

    @agent.reduce(RunFailed)
    def on_run_failed(state, event):
        return replace(state, failure_reason=event.reason)

    @agent.react(UserMessageAdded)
    def request_model(state, event):
        return ModelCallRequested(text=event.text)

    @agent.effect(ModelCallRequested)
    async def call_model(effect, context):
        if context.should_fail:
            raise EffectFailed("model backend unavailable")
        return ModelCallSucceeded(text=f"echo: {effect.text}")

    @agent.react(ModelCallSucceeded)
    def produce_answer(state, event):
        return [AnswerProduced(text=event.text), RunCompleted()]

    @agent.react(agent.event_type("EffectFailed"))
    def on_effect_failed(state, event):
        return RunFailed(reason=event.reason)

    @agent.command("message")
    def message(payload):
        text = (payload or {}).get("text")
        if not text:
            raise CommandRejected("text must not be empty")
        return UserMessageAdded(text=text)

    agent.terminal(RunCompleted, status="completed")
    agent.terminal(RunFailed, status="failed")

    class Resource:
        def __init__(self, should_fail: bool) -> None:
            self.should_fail = should_fail

    return agent, Resource(fail_effect)


def build_terminal_conflict_agent(*, name: str = "buggy") -> Agent:
    """Deliberately buggy agent: one reaction returns two terminal event
    types in a single output batch -- a definition bug, not a domain
    failure."""
    agent = Agent(name=name, initial_state=lambda: None)

    @agent.event
    @dataclass(frozen=True)
    class Started:
        pass

    @agent.event
    @dataclass(frozen=True)
    class RunCompleted:
        pass

    @agent.event
    @dataclass(frozen=True)
    class RunFailed:
        pass

    @agent.reduce(Started)
    def on_started(state, event):
        return state

    @agent.react(Started)
    def buggy_reaction(state, event):
        return [RunCompleted(), RunFailed()]

    @agent.command("start")
    def start(payload):
        return Started()

    agent.terminal(RunCompleted, status="completed")
    agent.terminal(RunFailed, status="failed")
    return agent


def collect_sse_events(response, *, stop_at: str | None = None) -> list[str]:
    event_types: list[str] = []
    for line in response.iter_lines():
        if line.startswith("event: "):
            event_type = line.removeprefix("event: ")
            event_types.append(event_type)
            if stop_at is not None and event_type == stop_at:
                return event_types
    return event_types


class HappyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "happy-path.db"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_completed_happy_path_through_create_run_command_sse_state_trace(self) -> None:
        integration = AIQApplication(
            store=run(SQLiteEventStore.open(self.path)),
            poll_interval_seconds=0.05,
        )
        agent, context = build_agent_and_context(fail_effect=False)
        integration.register(agent, context=context)
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router)

        with TestClient(app) as client:
            run_id = client.post("/agents/assistant/runs").json()["run_id"]
            response = client.post(
                f"/agents/assistant/runs/{run_id}/commands/message",
                json={"text": "hi"},
            )
            self.assertEqual(response.status_code, 200)

            with client.stream(
                "GET", f"/agents/assistant/runs/{run_id}/stream"
            ) as stream:
                event_types = collect_sse_events(stream, stop_at="RunCompleted")
            self.assertEqual(
                event_types,
                [
                    "RunCreated",
                    "UserMessageAdded",
                    "ModelCallRequested",
                    "ModelCallSucceeded",
                    "AnswerProduced",
                    "RunCompleted",
                ],
            )

            state = client.get(f"/agents/assistant/runs/{run_id}").json()["state"]
            self.assertEqual(state["answer"], "echo: hi")

            trace = client.get(f"/agents/assistant/runs/{run_id}/trace").json()
            self.assertEqual(trace["terminal_status"], "completed")
            self.assertGreaterEqual(len(trace["edges"]), 1)

            rejected = client.post(
                f"/agents/assistant/runs/{run_id}/commands/message",
                json={"text": "too late"},
            )
            self.assertEqual(rejected.status_code, 409)


class EffectFailedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "effect-failed.db"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_one_agents_effect_failure_does_not_affect_a_different_run(self) -> None:
        integration = AIQApplication(
            store=run(SQLiteEventStore.open(self.path)),
            poll_interval_seconds=0.05,
        )
        flaky_agent, flaky_context = build_agent_and_context(
            name="flaky", fail_effect=True
        )
        reliable_agent, reliable_context = build_agent_and_context(
            name="reliable", fail_effect=False
        )
        integration.register(flaky_agent, context=flaky_context)
        integration.register(reliable_agent, context=reliable_context)
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router)

        with TestClient(app) as client:
            flaky_run_id = client.post("/agents/flaky/runs").json()["run_id"]
            client.post(
                f"/agents/flaky/runs/{flaky_run_id}/commands/message",
                json={"text": "hi"},
            )
            with client.stream(
                "GET", f"/agents/flaky/runs/{flaky_run_id}/stream"
            ) as stream:
                flaky_events = collect_sse_events(stream, stop_at="RunFailed")
            self.assertEqual(
                flaky_events,
                [
                    "RunCreated",
                    "UserMessageAdded",
                    "ModelCallRequested",
                    "EffectFailed",
                    "RunFailed",
                ],
            )

            health = client.get("/agents/_health").json()
            self.assertTrue(health["healthy"])

            flaky_trace = client.get(
                f"/agents/flaky/runs/{flaky_run_id}/trace"
            ).json()
            self.assertEqual(flaky_trace["terminal_status"], "failed")

            reliable_run_id = client.post("/agents/reliable/runs").json()["run_id"]
            client.post(
                f"/agents/reliable/runs/{reliable_run_id}/commands/message",
                json={"text": "hi"},
            )
            with client.stream(
                "GET", f"/agents/reliable/runs/{reliable_run_id}/stream"
            ) as stream:
                reliable_events = collect_sse_events(stream, stop_at="RunCompleted")
            self.assertEqual(
                reliable_events,
                [
                    "RunCreated",
                    "UserMessageAdded",
                    "ModelCallRequested",
                    "ModelCallSucceeded",
                    "AnswerProduced",
                    "RunCompleted",
                ],
            )


class TerminalConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "terminal-conflict.db"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_two_terminal_events_in_one_batch_fails_the_worker_and_commits_neither(
        self,
    ) -> None:
        store = run(SQLiteEventStore.open(self.path))
        integration = AIQApplication(store=store, poll_interval_seconds=0.05)
        integration.register(build_terminal_conflict_agent())
        app = FastAPI(lifespan=integration.lifespan)
        app.include_router(integration.router)

        with TestClient(app) as client:
            run_id = client.post("/agents/buggy/runs").json()["run_id"]
            response = client.post(
                f"/agents/buggy/runs/{run_id}/commands/start", json={}
            )
            self.assertEqual(response.status_code, 200)

            health = None
            for _ in range(200):
                health = client.get("/agents/_health").json()
                if not health["healthy"]:
                    break
                time.sleep(0.02)
            else:
                self.fail("worker never became unhealthy")
            self.assertEqual(health["status"], "unhealthy")

        history = run(store.load(run_stream_id("buggy", run_id)))
        terminal_types = {"RunCompleted", "RunFailed"}
        committed_terminal = [
            envelope.event.event_type
            for envelope in history
            if envelope.event.event_type in terminal_types
        ]
        self.assertEqual(committed_terminal, [])


class CrashRestartTests(unittest.TestCase):
    def test_a_run_started_by_one_generation_finishes_under_a_completely_fresh_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "e2e-restart.db"

            # Generation 1: create the run and append UserMessageAdded via
            # the real command handler, then drive only the reaction (not
            # the effect) by hand -- deliberately not through a running
            # AIQApplication/TestClient background worker, to avoid
            # racing a real poll loop against this setup step.
            agent1, context1 = build_agent_and_context(fail_effect=False)
            runtime1 = agent1.build_runtime(context=context1)
            stream_id = run_stream_id("assistant", "run-1")

            async def start(agent, runtime):
                store = await SQLiteEventStore.open(path)
                run_created = Event("RunCreated", {"agent": "assistant"})
                await store.append(stream_id, -1, [run_created])
                produced = agent.handle_command("message", {"text": "hi"})
                await store.append(stream_id, 0, produced)
                reactions = DurableDispatcher(
                    agent=runtime.agent,
                    store=store,
                    subscription_name="assistant:reactions",
                )
                for _ in range(10):
                    if not await reactions.run_once():
                        break

            run(start(agent1, runtime1))
            del agent1, runtime1, context1

            # Generation 2: brand new Agent, brand new resource instance,
            # a real AIQApplication/FastAPI app over a freshly opened
            # store pointed at the same file. It must finish the run using
            # nothing else.
            store2 = run(SQLiteEventStore.open(path))
            integration = AIQApplication(store=store2, poll_interval_seconds=0.05)
            agent2, context2 = build_agent_and_context(fail_effect=False)
            integration.register(agent2, context=context2)
            app = FastAPI(lifespan=integration.lifespan)
            app.include_router(integration.router)

            with TestClient(app) as client:
                with client.stream(
                    "GET", "/agents/assistant/runs/run-1/stream"
                ) as stream:
                    event_types = collect_sse_events(stream, stop_at="RunCompleted")
                self.assertEqual(
                    event_types,
                    [
                        "RunCreated",
                        "UserMessageAdded",
                        "ModelCallRequested",
                        "ModelCallSucceeded",
                        "AnswerProduced",
                        "RunCompleted",
                    ],
                )
                state = client.get("/agents/assistant/runs/run-1").json()["state"]
                self.assertEqual(state["answer"], "echo: hi")


class DefinitionVersionIsolationTests(unittest.TestCase):
    """A run created under one `Agent(version=...)` must not be silently
    read, rebuilt, or continued under a later-deployed different version --
    Run = (Definition, History) is a pair, not just the history."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "definition-version.db"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_reading_or_commanding_a_run_under_a_different_version_is_409(self) -> None:
        store_v1 = run(SQLiteEventStore.open(self.path))
        integration_v1 = AIQApplication(store=store_v1, poll_interval_seconds=0.05)
        agent_v1, context_v1 = build_agent_and_context(version="v1")
        integration_v1.register(agent_v1, context=context_v1)
        app_v1 = FastAPI(lifespan=integration_v1.lifespan)
        app_v1.include_router(integration_v1.router)

        with TestClient(app_v1) as client_v1:
            run_id = client_v1.post("/agents/assistant/runs").json()["run_id"]
            response = client_v1.post(
                f"/agents/assistant/runs/{run_id}/commands/message",
                json={"text": "hi"},
            )
            self.assertEqual(response.status_code, 200)

        # A second, independent generation -- different Agent instance,
        # different version -- reopens the same file.
        store_v2 = run(SQLiteEventStore.open(self.path))
        integration_v2 = AIQApplication(
            store=store_v2, poll_interval_seconds=60
        )
        agent_v2, context_v2 = build_agent_and_context(version="v2")
        integration_v2.register(agent_v2, context=context_v2)
        app_v2 = FastAPI(lifespan=integration_v2.lifespan)
        app_v2.include_router(integration_v2.router)

        with TestClient(app_v2) as client_v2:
            read_response = client_v2.get(f"/agents/assistant/runs/{run_id}")
            self.assertEqual(read_response.status_code, 409)

            command_response = client_v2.post(
                f"/agents/assistant/runs/{run_id}/commands/message",
                json={"text": "too late"},
            )
            self.assertEqual(command_response.status_code, 409)

    def test_a_stale_run_does_not_block_a_valid_runs_progress(self) -> None:
        """Mismatch(r_old) must not imply Unavailable(r_new): a leftover
        in-flight run from before a deploy sits at a lower global position
        than a run created after the deploy, so the background worker
        reaches it first. It must be skipped, not crash the worker."""
        store_v1 = run(SQLiteEventStore.open(self.path))
        integration_v1 = AIQApplication(
            store=store_v1, poll_interval_seconds=60
        )
        agent_v1, context_v1 = build_agent_and_context(version="v1")
        integration_v1.register(agent_v1, context=context_v1)
        app_v1 = FastAPI(lifespan=integration_v1.lifespan)
        app_v1.include_router(integration_v1.router)

        with TestClient(app_v1) as client_v1:
            old_run_id = client_v1.post("/agents/assistant/runs").json()["run_id"]
            client_v1.post(
                f"/agents/assistant/runs/{old_run_id}/commands/message",
                json={"text": "old"},
            )
            # poll_interval_seconds=60 keeps this app's own worker from
            # processing it -- old_run_id is left at just RunCreated +
            # UserMessageAdded, i.e. genuinely in-flight/incomplete.

        store_v2 = run(SQLiteEventStore.open(self.path))
        integration_v2 = AIQApplication(
            store=store_v2, poll_interval_seconds=0.05
        )
        agent_v2, context_v2 = build_agent_and_context(version="v2")
        integration_v2.register(agent_v2, context=context_v2)
        app_v2 = FastAPI(lifespan=integration_v2.lifespan)
        app_v2.include_router(integration_v2.router)

        with TestClient(app_v2) as client_v2:
            new_run_id = client_v2.post("/agents/assistant/runs").json()["run_id"]
            client_v2.post(
                f"/agents/assistant/runs/{new_run_id}/commands/message",
                json={"text": "new"},
            )

            with client_v2.stream(
                "GET", f"/agents/assistant/runs/{new_run_id}/stream"
            ) as stream:
                event_types = collect_sse_events(stream, stop_at="RunCompleted")

            self.assertEqual(
                event_types,
                [
                    "RunCreated",
                    "UserMessageAdded",
                    "ModelCallRequested",
                    "ModelCallSucceeded",
                    "AnswerProduced",
                    "RunCompleted",
                ],
            )
            self.assertTrue(client_v2.get("/agents/_health").json()["healthy"])

            # The stale run is still blocked, not silently upgraded to v2.
            stale_read = client_v2.get(f"/agents/assistant/runs/{old_run_id}")
            self.assertEqual(stale_read.status_code, 409)

    def test_a_versions_dispatcher_skip_does_not_cost_another_version_the_stream(
        self,
    ) -> None:
        """Skip_v2(e) must not imply Skip_v1(e): v2's dispatcher skipping a
        v1 stream advances only *v2's own* checkpoint. When v1 comes back
        later (e.g. a rollback), it must still be able to see and finish
        that same stream -- checkpoint identity belongs to
        (agent_name, definition_version, dispatcher_kind), not just
        agent_name."""
        store_v1a = run(SQLiteEventStore.open(self.path))
        integration_v1a = AIQApplication(
            store=store_v1a, poll_interval_seconds=60
        )
        agent_v1a, context_v1a = build_agent_and_context(version="v1")
        integration_v1a.register(agent_v1a, context=context_v1a)
        app_v1a = FastAPI(lifespan=integration_v1a.lifespan)
        app_v1a.include_router(integration_v1a.router)

        with TestClient(app_v1a) as client:
            old_run_id = client.post("/agents/assistant/runs").json()["run_id"]
            client.post(
                f"/agents/assistant/runs/{old_run_id}/commands/message",
                json={"text": "old"},
            )
            # poll_interval_seconds=60 -- left incomplete on purpose.

        # Only a v2 worker runs for a while: it encounters run-old and
        # skips it.
        store_v2 = run(SQLiteEventStore.open(self.path))
        integration_v2 = AIQApplication(
            store=store_v2, poll_interval_seconds=0.05
        )
        agent_v2, context_v2 = build_agent_and_context(version="v2")
        integration_v2.register(agent_v2, context=context_v2)
        app_v2 = FastAPI(lifespan=integration_v2.lifespan)
        app_v2.include_router(integration_v2.router)

        with TestClient(app_v2) as client:
            time.sleep(0.3)
            self.assertTrue(client.get("/agents/_health").json()["healthy"])

        # v1 comes back (e.g. a rollback) on the same file.
        store_v1b = run(SQLiteEventStore.open(self.path))
        integration_v1b = AIQApplication(
            store=store_v1b, poll_interval_seconds=0.05
        )
        agent_v1b, context_v1b = build_agent_and_context(version="v1")
        integration_v1b.register(agent_v1b, context=context_v1b)
        app_v1b = FastAPI(lifespan=integration_v1b.lifespan)
        app_v1b.include_router(integration_v1b.router)

        with TestClient(app_v1b) as client:
            deadline = time.monotonic() + 3
            state = None
            while time.monotonic() < deadline:
                state = client.get(f"/agents/assistant/runs/{old_run_id}").json()[
                    "state"
                ]
                if state.get("answer") is not None:
                    break
                time.sleep(0.05)
            self.assertEqual(state["answer"], "echo: old")


if __name__ == "__main__":
    unittest.main()
