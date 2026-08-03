import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from aiq import (
    AgentDefinition,
    DurableEffectDispatcher,
    EffectContext,
    EffectMetadataError,
    EffectRegistry,
    Event,
    SQLiteEventStore,
    TraceService,
    effect_request,
    run_stream_id,
)


def run(coro):
    return asyncio.run(coro)


@dataclass(frozen=True)
class State:
    pass


def agent() -> AgentDefinition[State]:
    definition = AgentDefinition("agent-a", initial_state=State)

    @definition.reducer
    def evolve(state: State, event: Event) -> State:
        return state

    return definition


class EffectMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "events.db"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_request_and_result_persist_same_operation_id_after_reopen(self) -> None:
        async def scenario() -> None:
            store = await SQLiteEventStore.open(self.path)
            request = effect_request(
                "ExternalRequested",
                {},
                {"custom_request": "kept"},
            )
            await store.append(
                run_stream_id("agent-a", "run-1"),
                -1,
                [request],
            )
            effects = EffectRegistry[State]()
            calls: list[str] = []

            @effects.effect("ExternalRequested")
            async def execute(
                event: Event,
                state: State,
                context: EffectContext,
            ):
                calls.append(str(event.event_id))
                return [
                    Event(
                        "ExternalSucceeded",
                        {},
                        {"custom_result": "kept"},
                    )
                ]

            worker = DurableEffectDispatcher(
                agent=agent(),
                store=store,
                effects=effects,
                context=EffectContext({}),
                subscription_name="agent-a:effects",
            )
            self.assertIs(await worker.run_once(), True)

            reopened = await SQLiteEventStore.open(self.path)
            history = await reopened.load(
                run_stream_id("agent-a", "run-1")
            )
            request_event, result_event = [item.event for item in history]
            expected = str(request.event_id)
            self.assertEqual(calls, [expected])
            self.assertEqual(request_event.metadata["operation_id"], expected)
            self.assertEqual(result_event.metadata["operation_id"], expected)
            self.assertEqual(result_event.metadata["causation_id"], expected)
            self.assertEqual(
                request_event.metadata["custom_request"],
                "kept",
            )
            self.assertEqual(
                result_event.metadata["custom_result"],
                "kept",
            )

            trace = await TraceService(
                store=reopened,
                agents={"agent-a": agent()},
            ).export("agent-a", "run-1")
            self.assertEqual(
                [item.operation_id for item in trace.events],
                [expected, expected],
            )

        run(scenario())

    def test_failure_and_rejection_results_receive_operation_identity(self) -> None:
        async def execute_result(result_type: str) -> None:
            path = self.path.with_name(f"{result_type}.db")
            store = await SQLiteEventStore.open(path)
            request = effect_request("ExternalRequested", {})
            stream_id = run_stream_id("agent-a", "run-1")
            await store.append(stream_id, -1, [request])
            effects = EffectRegistry[State]()

            @effects.effect("ExternalRequested")
            async def execute(
                event: Event,
                state: State,
                context: EffectContext,
            ):
                return [Event(result_type, {})]

            worker = DurableEffectDispatcher(
                agent=agent(),
                store=store,
                effects=effects,
                context=EffectContext({}),
                subscription_name="effects",
            )
            await worker.run_once()
            result = (await store.load(stream_id))[-1].event
            expected = str(request.event_id)
            self.assertEqual(result.metadata["operation_id"], expected)
            self.assertEqual(result.metadata["causation_id"], expected)

        for result_type in ("ExternalFailed", "ExternalRejected"):
            with self.subTest(result_type=result_type):
                run(execute_result(result_type))

    def test_conflicting_request_operation_id_is_rejected(self) -> None:
        request_id = UUID("00000000-0000-4000-8000-000000000001")
        with self.assertRaisesRegex(
            EffectMetadataError,
            "operation_id must equal its event_id",
        ):
            effect_request(
                "ExternalRequested",
                {},
                {"operation_id": "different"},
                event_id=request_id,
            )

    def test_missing_request_operation_id_fails_before_external_io(self) -> None:
        async def scenario() -> None:
            store = await SQLiteEventStore.open(self.path)
            stream_id = run_stream_id("agent-a", "run-1")
            await store.append(
                stream_id,
                -1,
                [Event("ExternalRequested", {})],
            )
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

            worker = DurableEffectDispatcher(
                agent=agent(),
                store=store,
                effects=effects,
                context=EffectContext({}),
                subscription_name="effects",
            )
            with self.assertRaisesRegex(
                EffectMetadataError,
                "effect_request",
            ):
                await worker.run_once()
            self.assertEqual(calls, 0)
            self.assertEqual(await store.load_checkpoint("effects"), 0)

        run(scenario())

    def test_conflicting_result_metadata_is_not_overwritten(self) -> None:
        async def scenario() -> None:
            store = await SQLiteEventStore.open(self.path)
            request = effect_request("ExternalRequested", {})
            stream_id = run_stream_id("agent-a", "run-1")
            await store.append(stream_id, -1, [request])
            effects = EffectRegistry[State]()

            @effects.effect("ExternalRequested")
            async def execute(
                event: Event,
                state: State,
                context: EffectContext,
            ):
                return [
                    Event(
                        "ExternalSucceeded",
                        {},
                        {"operation_id": "conflict"},
                    )
                ]

            worker = DurableEffectDispatcher(
                agent=agent(),
                store=store,
                effects=effects,
                context=EffectContext({}),
                subscription_name="effects",
            )
            with self.assertRaisesRegex(
                EffectMetadataError,
                "conflicts",
            ):
                await worker.run_once()
            self.assertEqual(
                [item.event.event_type for item in await store.load(stream_id)],
                ["ExternalRequested"],
            )
            self.assertEqual(await store.load_checkpoint("effects"), 0)

        run(scenario())


if __name__ == "__main__":
    unittest.main()
