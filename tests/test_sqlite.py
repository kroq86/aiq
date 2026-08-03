import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from aiq import (
    CheckpointConflictError,
    DuplicateEventError,
    Event,
    SQLiteEventStore,
    SQLiteSubscriptionCheckpoints,
    VersionConflictError,
)


def run(coro):
    return asyncio.run(coro)


class SQLiteEventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "events.db"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_history_survives_reopen(self) -> None:
        first_store = run(SQLiteEventStore.open(self.path))
        original = Event(
            "ToolCallSucceeded",
            {"result": {"pressure": 152.4}, "tags": ["well", "A-17"]},
            {"model": "test-model"},
        )
        appended = run(first_store.append("run-1", -1, [original]))

        reopened_store = run(SQLiteEventStore.open(self.path))
        history = run(reopened_store.load("run-1"))

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].event.event_id, original.event_id)
        self.assertEqual(history[0].event.data["result"]["pressure"], 152.4)
        self.assertEqual(history[0].event.data["tags"], ("well", "A-17"))
        self.assertEqual(history[0].global_position, appended[0].global_position)

    def test_committed_history_survives_abrupt_process_exit(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        script = """
import asyncio
import os
import sys
from aiq import Event, SQLiteEventStore

async def write():
    store = await SQLiteEventStore.open(sys.argv[1])
    await store.append("run-crash", -1, [Event("RunCreated", {"committed": True})])

asyncio.run(write())
os._exit(23)
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(source_root)
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.path)],
            env=environment,
            check=False,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 23)
        reopened = run(SQLiteEventStore.open(self.path))
        history = run(reopened.load("run-crash"))
        self.assertEqual(len(history), 1)
        self.assertIs(history[0].event.data["committed"], True)

    def test_version_conflict_keeps_batch_atomic(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        run(store.append("run-1", -1, [Event("RunCreated", {})]))

        with self.assertRaises(VersionConflictError):
            run(
                store.append(
                    "run-1",
                    -1,
                    [
                        Event("UserMessageAdded", {"text": "one"}),
                        Event("UserMessageAdded", {"text": "two"}),
                    ],
                )
            )

        self.assertEqual(len(run(store.load("run-1"))), 1)

    def test_commit_subscription_batch_with_empty_events_ignores_stream_version(
        self,
    ) -> None:
        """A checkpoint-only advance (events=()) asserts nothing about
        stream content, so a stale/wrong expected_stream_version must not
        conflict -- this is what lets a dispatcher processing an event with
        no reaction/effect output avoid spuriously racing a concurrent
        append to the same stream from an unrelated writer (e.g. an HTTP
        command)."""
        store = run(SQLiteEventStore.open(self.path))
        run(store.append("run-1", -1, [Event("RunCreated", {})]))

        run(
            store.commit_subscription_batch(
                subscription_name="reactions",
                expected_checkpoint=0,
                stream_id="run-1",
                expected_stream_version=999,
                events=(),
                new_checkpoint=1,
            )
        )

        self.assertEqual(run(store.load_checkpoint("reactions")), 1)
        self.assertEqual(len(run(store.load("run-1"))), 1)

    def test_duplicate_event_id_keeps_batch_atomic(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        event_id = uuid4()
        run(store.append("run-1", -1, [Event("RunCreated", {}, event_id=event_id)]))

        with self.assertRaises(DuplicateEventError):
            run(
                store.append(
                    "run-2",
                    -1,
                    [
                        Event("RunCreated", {}),
                        Event("UserMessageAdded", {}, event_id=event_id),
                    ],
                )
            )

        self.assertEqual(run(store.load("run-2")), ())

    def test_database_rejects_update_and_delete(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        run(store.append("run-1", -1, [Event("RunCreated", {})]))

        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE events SET event_type = 'Changed' WHERE stream_id = 'run-1'"
                )

            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("DELETE FROM events WHERE stream_id = 'run-1'")

        self.assertEqual(len(run(store.load("run-1"))), 1)

    def test_load_after_version_returns_only_tail(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        run(
            store.append(
                "run-1",
                -1,
                [
                    Event("RunCreated", {}),
                    Event("UserMessageAdded", {"text": "hello"}),
                    Event("AnswerProduced", {"text": "world"}),
                ],
            )
        )

        tail = run(store.load("run-1", after_version=0))

        self.assertEqual(
            [envelope.event.event_type for envelope in tail],
            ["UserMessageAdded", "AnswerProduced"],
        )

    def test_current_version_without_loading_stream_history(self) -> None:
        store = run(SQLiteEventStore.open(self.path))

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
        store = run(SQLiteEventStore.open(self.path))
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

    def test_concurrent_appends_allow_one_winner(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        run(store.append("run-1", -1, [Event("RunCreated", {})]))

        async def race():
            return await asyncio.gather(
                store.append("run-1", 0, [Event("AnswerProduced", {"text": "one"})]),
                store.append("run-1", 0, [Event("AnswerProduced", {"text": "two"})]),
                return_exceptions=True,
            )

        results = run(race())
        failures = [result for result in results if isinstance(result, Exception)]

        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], VersionConflictError)
        self.assertEqual(len(run(store.load("run-1"))), 2)

    def test_global_log_pages_across_streams(self) -> None:
        store = run(SQLiteEventStore.open(self.path))
        run(store.append("run-1", -1, [Event("First", {})]))
        run(store.append("run-2", -1, [Event("Second", {})]))
        run(store.append("run-1", 0, [Event("Third", {})]))

        first_page = run(store.load_global(after_position=0, limit=2))
        second_page = run(
            store.load_global(
                after_position=first_page[-1].global_position,
                limit=2,
            )
        )

        self.assertEqual(
            [item.event.event_type for item in first_page],
            ["First", "Second"],
        )
        self.assertEqual(
            [item.event.event_type for item in second_page],
            ["Third"],
        )

    def test_subscription_checkpoint_survives_reopen_and_rejects_stale_save(self) -> None:
        checkpoints = run(SQLiteSubscriptionCheckpoints.open(self.path))
        self.assertEqual(run(checkpoints.load("mcp-executor")), 0)
        run(checkpoints.save("mcp-executor", 7, expected_position=0))

        reopened = run(SQLiteSubscriptionCheckpoints.open(self.path))
        self.assertEqual(run(reopened.load("mcp-executor")), 7)
        with self.assertRaises(CheckpointConflictError):
            run(reopened.save("mcp-executor", 8, expected_position=0))
        self.assertEqual(run(reopened.load("mcp-executor")), 7)

    def test_concurrent_checkpoint_saves_allow_one_winner(self) -> None:
        checkpoints = run(SQLiteSubscriptionCheckpoints.open(self.path))

        async def race():
            return await asyncio.gather(
                checkpoints.save("worker", 1, expected_position=0),
                checkpoints.save("worker", 2, expected_position=0),
                return_exceptions=True,
            )

        results = run(race())
        failures = [result for result in results if isinstance(result, Exception)]

        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], CheckpointConflictError)
        self.assertIn(run(checkpoints.load("worker")), {1, 2})


if __name__ == "__main__":
    unittest.main()
