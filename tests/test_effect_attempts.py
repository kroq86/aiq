import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentlog import (
    Event,
    InMemoryEffectAttemptStore,
    SQLiteEffectAttemptStore,
    SQLiteEventStore,
    build_effect_attempt_metrics,
)


def run(coro):
    return asyncio.run(coro)


async def record(
    store,
    operation_id: str,
    *,
    stream_id: str = "agent:run-1",
    event_type: str = "ToolCallRequested",
    global_position: int = 1,
):
    return await store.record_start(
        operation_id=operation_id,
        stream_id=stream_id,
        request_event_type=event_type,
        request_global_position=global_position,
        subscription_name="agent:v1:effects",
    )


class EffectAttemptStoreTests(unittest.TestCase):
    def test_in_memory_store_numbers_attempts_per_operation(self) -> None:
        store = InMemoryEffectAttemptStore()

        first = run(record(store, "operation-1"))
        second = run(record(store, "operation-1"))
        other = run(
            record(
                store,
                "operation-2",
                stream_id="agent:run-2",
                global_position=2,
            )
        )

        self.assertEqual((first.attempt_number, second.attempt_number), (1, 2))
        self.assertEqual(other.attempt_number, 1)
        self.assertEqual(
            run(store.load_for_stream("agent:run-1")),
            (first, second),
        )
        self.assertEqual(
            run(store.load_for_operation("operation-1")),
            (first, second),
        )

    def test_sqlite_store_persists_and_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "agentlog.db"
            event_store = run(SQLiteEventStore.open(path))
            run(
                event_store.append(
                    "agent:run-1",
                    -1,
                    [Event("RunCreated", {"agent": "agent"})],
                )
            )
            store = run(SQLiteEffectAttemptStore.open(path))
            first = run(record(store, "operation-1"))
            second = run(record(store, "operation-1"))
            with self.assertRaisesRegex(ValueError, "identity changed"):
                run(
                    record(
                        store,
                        "operation-1",
                        stream_id="agent:other-run",
                    )
                )

            reopened = run(SQLiteEffectAttemptStore.open(path))
            self.assertEqual(
                run(reopened.load_for_stream("agent:run-1")),
                (first, second),
            )
            self.assertEqual(
                run(reopened.load_for_operation("operation-1")),
                (first, second),
            )
            self.assertEqual(
                len(run(event_store.load("agent:run-1"))),
                1,
            )

            with sqlite3.connect(path) as connection:
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "append-only"
                ):
                    connection.execute(
                        """
                        UPDATE effect_attempts
                        SET attempt_number = 3
                        WHERE attempt_id = ?
                        """,
                        (str(first.attempt_id),),
                    )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "append-only"
                ):
                    connection.execute(
                        "DELETE FROM effect_attempts WHERE attempt_id = ?",
                        (str(first.attempt_id),),
                    )

    def test_metrics_separate_retries_from_unique_operations(self) -> None:
        store = InMemoryEffectAttemptStore()
        run(record(store, "operation-1"))
        run(record(store, "operation-1"))
        run(
            record(
                store,
                "operation-2",
                event_type="ModelCallRequested",
                global_position=2,
            )
        )
        attempts = run(store.load_for_stream("agent:run-1"))

        metrics = build_effect_attempt_metrics(attempts)

        self.assertEqual(metrics.attempt_count, 3)
        self.assertEqual(metrics.operation_count, 2)
        self.assertEqual(metrics.retried_operation_count, 1)
        self.assertEqual(metrics.retry_attempt_count, 1)
        self.assertEqual(metrics.max_attempts_per_operation, 2)
        self.assertEqual(
            metrics.attempt_count_by_event_type,
            {"ModelCallRequested": 1, "ToolCallRequested": 2},
        )

    def test_metrics_reject_non_contiguous_attempt_numbers(self) -> None:
        store = InMemoryEffectAttemptStore()
        first = run(record(store, "operation-1"))

        with self.assertRaisesRegex(ValueError, "contiguous"):
            build_effect_attempt_metrics((replace(first, attempt_number=2),))

    def test_store_rejects_invalid_operational_identity(self) -> None:
        store = InMemoryEffectAttemptStore()

        with self.assertRaisesRegex(ValueError, "operation_id"):
            run(record(store, ""))
        with self.assertRaisesRegex(ValueError, "stream_id"):
            run(record(store, "operation-1", stream_id=""))
        run(record(store, "operation-1"))
        with self.assertRaisesRegex(ValueError, "identity changed"):
            run(
                record(
                    store,
                    "operation-1",
                    global_position=2,
                )
            )


if __name__ == "__main__":
    unittest.main()
