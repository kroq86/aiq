import asyncio
import math
import unittest
from dataclasses import dataclass, replace
from uuid import uuid4

from aiq import (
    CheckpointConflictError,
    DuplicateEventError,
    Event,
    InMemoryEventStore,
    InMemorySubscriptionCheckpoints,
    VersionConflictError,
    replay,
)


def run(coro):
    return asyncio.run(coro)


class EventStoreTests(unittest.TestCase):
    def test_append_versions_and_replay(self) -> None:
        store = InMemoryEventStore()
        first = Event("RunCreated", {"prompt": "Pressure for A-17"})
        second = Event("AnswerProduced", {"text": "Stable"})

        run(store.append("run-1", -1, [first]))
        run(store.append("run-1", 0, [second]))
        history = run(store.load("run-1"))

        @dataclass(frozen=True)
        class State:
            answer: str | None = None

        def evolve(state: State, event: Event) -> State:
            if event.event_type == "AnswerProduced":
                return replace(state, answer=str(event.data["text"]))
            return state

        self.assertEqual([item.stream_version for item in history], [0, 1])
        self.assertEqual(replay(State(), history, evolve), State(answer="Stable"))

    def test_version_conflict_does_not_append(self) -> None:
        store = InMemoryEventStore()
        run(store.append("run-1", -1, [Event("RunCreated", {})]))

        with self.assertRaises(VersionConflictError):
            run(store.append("run-1", -1, [Event("AnswerProduced", {"text": "late"})]))

        self.assertEqual(len(run(store.load("run-1"))), 1)

    def test_duplicate_id_rejects_whole_batch(self) -> None:
        store = InMemoryEventStore()
        event_id = uuid4()
        run(store.append("run-1", -1, [Event("RunCreated", {}, event_id=event_id)]))

        with self.assertRaises(DuplicateEventError):
            run(
                store.append(
                    "run-2",
                    -1,
                    [
                        Event("RunCreated", {}),
                        Event("UserMessageAdded", {"text": "hello"}, event_id=event_id),
                    ],
                )
            )

        self.assertEqual(run(store.load("run-2")), ())

    def test_event_payload_is_deeply_immutable(self) -> None:
        source = {"items": [{"value": 1}]}
        event = Event("ToolCallSucceeded", source)
        source["items"][0]["value"] = 2

        self.assertEqual(event.data["items"][0]["value"], 1)
        with self.assertRaises(TypeError):
            event.data["new"] = "value"

    def test_event_rejects_non_string_object_keys_without_losing_facts(self) -> None:
        with self.assertRaisesRegex(TypeError, "keys must be strings"):
            Event("Invalid", {1: "integer", "1": "string"})

    def test_event_rejects_non_finite_json_numbers(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite JSON values"):
                    Event("Invalid", {"value": value})

    def test_event_id_is_always_an_explicit_uuid_value(self) -> None:
        generated = Event("RunCreated", {})
        supplied = uuid4()

        self.assertIsInstance(generated.event_id, type(supplied))
        self.assertEqual(Event("RunCreated", {}, event_id=supplied).event_id, supplied)
        with self.assertRaisesRegex(TypeError, "event_id must be a UUID"):
            Event("RunCreated", {}, event_id=None)

    def test_global_log_is_ordered_across_streams_and_limited(self) -> None:
        store = InMemoryEventStore()
        run(store.append("run-1", -1, [Event("First", {})]))
        run(store.append("run-2", -1, [Event("Second", {})]))
        run(store.append("run-1", 0, [Event("Third", {})]))

        page = run(store.load_global(after_position=1, limit=1))

        self.assertEqual([item.event.event_type for item in page], ["Second"])
        self.assertEqual(page[0].global_position, 2)

    def test_current_version_contract(self) -> None:
        store = InMemoryEventStore()

        self.assertEqual(run(store.current_version("missing")), -1)
        run(
            store.append(
                "run-1",
                -1,
                [Event("First", {}), Event("Second", {})],
            )
        )
        self.assertEqual(run(store.current_version("run-1")), 1)

    def test_load_stream_after_global_position(self) -> None:
        store = InMemoryEventStore()
        first = run(store.append("run-1", -1, [Event("One", {})]))[0]
        run(store.append("run-2", -1, [Event("Other", {})]))
        third = run(store.append("run-1", 0, [Event("Two", {})]))[0]

        tail = run(
            store.load_stream_after_position(
                "run-1",
                after_position=first.global_position,
            )
        )

        self.assertEqual(tail, (third,))


class SubscriptionCheckpointTests(unittest.TestCase):
    def test_checkpoint_compare_and_set(self) -> None:
        checkpoints = InMemorySubscriptionCheckpoints()

        self.assertEqual(run(checkpoints.load("worker")), 0)
        self.assertEqual(
            run(checkpoints.save("worker", 10, expected_position=0)),
            10,
        )
        with self.assertRaises(CheckpointConflictError):
            run(checkpoints.save("worker", 11, expected_position=0))
        self.assertEqual(run(checkpoints.load("worker")), 10)


if __name__ == "__main__":
    unittest.main()
