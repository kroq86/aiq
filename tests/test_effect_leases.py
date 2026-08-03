from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from aiq import (
    EffectLeaseOptions,
    Event,
    InMemoryEventStore,
    SQLiteEffectAttemptStore,
    SQLiteEventStore,
    effect_request,
)
from aiq.leases import LeaseLostError


def run(coro):
    return asyncio.run(coro)


class EffectLeaseOptionsTests(unittest.TestCase):
    def test_options_require_valid_heartbeat_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "worker_id"):
            EffectLeaseOptions("")
        with self.assertRaisesRegex(ValueError, "less than"):
            EffectLeaseOptions(
                "worker-a",
                ttl_seconds=1,
                renewal_interval_seconds=1,
            )


class SQLiteEffectLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "events.db"
        self.store = run(SQLiteEventStore.open(self.path))
        self.request = effect_request("ModelCallRequested", {})
        self.envelope = run(
            self.store.append("run-1", -1, [self.request])
        )[0]

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _claim(self, worker: str):
        result = run(
            self.store.try_claim_effect(
                subscription_name="effects",
                expected_checkpoint=0,
                request_global_position=self.envelope.global_position,
                operation_id=str(self.request.event_id),
                stream_id="run-1",
                request_event_type=self.request.event_type,
                worker_id=worker,
                lease_ttl_seconds=30,
                terminal_event_types=frozenset(),
            )
        )
        return result.lease

    def _expire(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                UPDATE effect_leases
                SET lease_expires_at_unix = 0
                WHERE subscription_name = 'effects'
                """
            )
            connection.commit()

    def test_claim_is_atomic_with_attempt_and_busy_is_not_attempt(self) -> None:
        first = self._claim("worker-a")
        self.assertIsNotNone(first)
        self.assertIsNone(self._claim("worker-b"))

        attempts = run(
            SQLiteEffectAttemptStore.open(self.path)
        )
        recorded = run(attempts.load_for_stream("run-1"))
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0].attempt_number, 1)

    def test_claim_and_attempt_roll_back_when_lease_write_fails(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                """
                CREATE TRIGGER fail_lease_insert
                BEFORE INSERT ON effect_leases
                BEGIN
                    SELECT RAISE(ABORT, 'injected lease failure');
                END;
                """
            )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "injected lease failure"
        ):
            self._claim("worker-a")
        attempts = run(SQLiteEffectAttemptStore.open(self.path))
        self.assertEqual(
            run(attempts.load_for_operation(str(self.request.event_id))),
            (),
        )

    def test_simultaneous_claims_have_one_winner(self) -> None:
        async def compete():
            async def claim(worker):
                return await self.store.try_claim_effect(
                    subscription_name="effects",
                    expected_checkpoint=0,
                    request_global_position=self.envelope.global_position,
                    operation_id=str(self.request.event_id),
                    stream_id="run-1",
                    request_event_type=self.request.event_type,
                    worker_id=worker,
                    lease_ttl_seconds=30,
                    terminal_event_types=frozenset(),
                )

            return await asyncio.gather(claim("worker-a"), claim("worker-b"))

        claims = run(compete())
        self.assertEqual(
            sum(claim.status == "acquired" for claim in claims), 1
        )

    def test_renew_keeps_token_and_takeover_increments_it(self) -> None:
        first = self._claim("worker-a")
        assert first is not None
        renewed = run(
            self.store.renew_effect_claim(first, lease_ttl_seconds=60)
        )
        self.assertEqual(renewed.fencing_token, first.fencing_token)
        self.assertEqual(renewed.lease_id, first.lease_id)
        self.assertGreater(
            renewed.lease_expires_at, first.lease_expires_at
        )

        self._expire()
        second = self._claim("worker-b")
        assert second is not None
        self.assertGreater(second.fencing_token, first.fencing_token)
        self.assertNotEqual(second.lease_id, first.lease_id)
        self.assertEqual(second.attempt.attempt_number, 2)

    def test_repeated_takeover_has_fresh_ids_and_monotonic_tokens(self) -> None:
        leases = []
        for worker in ("worker-a", "worker-b", "worker-c"):
            if leases:
                self._expire()
            lease = self._claim(worker)
            assert lease is not None
            leases.append(lease)
        self.assertEqual(
            [lease.fencing_token for lease in leases], [1, 2, 3]
        )
        self.assertEqual(len({lease.lease_id for lease in leases}), 3)
        self.assertEqual(
            [lease.attempt.attempt_number for lease in leases],
            [1, 2, 3],
        )

    def test_released_claim_gets_larger_token(self) -> None:
        first = self._claim("worker-a")
        assert first is not None
        self.assertTrue(run(self.store.release_effect_claim(first)))
        second = self._claim("worker-b")
        assert second is not None
        self.assertEqual(second.fencing_token, first.fencing_token + 1)

    def test_expired_or_foreign_lease_cannot_renew(self) -> None:
        first = self._claim("worker-a")
        assert first is not None
        foreign = replace(first, worker_id="worker-b")
        with self.assertRaises(LeaseLostError):
            run(
                self.store.renew_effect_claim(
                    foreign, lease_ttl_seconds=30
                )
            )
        self._expire()
        with self.assertRaises(LeaseLostError):
            run(
                self.store.renew_effect_claim(
                    first, lease_ttl_seconds=30
                )
            )

    def test_commit_requires_matching_token_and_unexpired_lease(self) -> None:
        first = self._claim("worker-a")
        assert first is not None
        wrong_token = replace(
            first, fencing_token=first.fencing_token + 1
        )
        with self.assertRaises(LeaseLostError):
            run(
                self.store.commit_fenced_subscription_batch(
                    wrong_token,
                    expected_checkpoint=0,
                    stream_id="run-1",
                    expected_stream_version=0,
                    events=[Event("ModelCallSucceeded", {})],
                    new_checkpoint=1,
                    terminal_event_types=frozenset(),
                )
            )
        self._expire()
        with self.assertRaises(LeaseLostError):
            run(
                self.store.commit_fenced_subscription_batch(
                    first,
                    expected_checkpoint=0,
                    stream_id="run-1",
                    expected_stream_version=0,
                    events=[Event("ModelCallSucceeded", {})],
                    new_checkpoint=1,
                    terminal_event_types=frozenset(),
                )
            )
        self.assertEqual(run(self.store.load_checkpoint("effects")), 0)

    def test_worker_clock_skew_does_not_expire_live_db_lease(self) -> None:
        first = self._claim("worker-a")
        assert first is not None

        class FutureDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2100, 1, 1, tzinfo=tz or timezone.utc)

        with mock.patch("aiq.sqlite.datetime", FutureDateTime):
            self.assertIsNone(self._claim("worker-b"))

    def test_stale_worker_cannot_append_or_advance_checkpoint(self) -> None:
        first = self._claim("worker-a")
        assert first is not None
        self._expire()
        second = self._claim("worker-b")
        assert second is not None

        with self.assertRaises(LeaseLostError):
            run(
                self.store.commit_fenced_subscription_batch(
                    first,
                    expected_checkpoint=0,
                    stream_id="run-1",
                    expected_stream_version=0,
                    events=[Event("ModelCallSucceeded", {})],
                    new_checkpoint=1,
                    terminal_event_types=frozenset(),
                )
            )
        self.assertEqual(run(self.store.load_checkpoint("effects")), 0)
        self.assertEqual(len(run(self.store.load("run-1"))), 1)

        run(
            self.store.commit_fenced_subscription_batch(
                second,
                expected_checkpoint=0,
                stream_id="run-1",
                expected_stream_version=0,
                events=[Event("ModelCallSucceeded", {})],
                new_checkpoint=1,
                terminal_event_types=frozenset(),
            )
        )
        self.assertEqual(run(self.store.load_checkpoint("effects")), 1)

    def test_restart_preserves_token_and_attempt_number(self) -> None:
        first = self._claim("worker-a")
        assert first is not None
        self._expire()
        reopened = run(SQLiteEventStore.open(self.path))
        second_result = run(
            reopened.try_claim_effect(
                subscription_name="effects",
                expected_checkpoint=0,
                request_global_position=1,
                operation_id=str(self.request.event_id),
                stream_id="run-1",
                request_event_type=self.request.event_type,
                worker_id="worker-b",
                lease_ttl_seconds=30,
                terminal_event_types=frozenset(),
            )
        )
        second = second_result.lease
        assert second is not None
        self.assertEqual(second.fencing_token, first.fencing_token + 1)
        self.assertEqual(second.attempt.attempt_number, 2)

    def test_same_file_migration_continues_existing_attempt_number(self) -> None:
        attempts = run(SQLiteEffectAttemptStore.open(self.path))
        run(
            attempts.record_start(
                operation_id=str(self.request.event_id),
                stream_id="run-1",
                request_event_type=self.request.event_type,
                request_global_position=1,
                subscription_name="effects",
            )
        )
        claim = self._claim("worker-a")
        assert claim is not None
        self.assertEqual(claim.attempt.attempt_number, 2)

    def test_open_migrates_pre_lease_id_unreleased_schema(self) -> None:
        legacy_path = self.path.parent / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE effect_leases (
                    subscription_name TEXT NOT NULL,
                    request_global_position INTEGER NOT NULL,
                    operation_id TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    request_event_type TEXT NOT NULL,
                    worker_id TEXT,
                    fencing_token INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    lease_expires_at_unix REAL,
                    claimed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (
                        subscription_name, request_global_position
                    )
                );
                INSERT INTO effect_leases VALUES (
                    'effects', 1, 'operation-1', 'run-1',
                    'ModelCallRequested', 'worker-a', 1, 'released',
                    NULL, '2026-08-03T00:00:00+00:00',
                    '2026-08-03T00:00:00+00:00'
                );
                """
            )
        run(SQLiteEventStore.open(legacy_path))
        with closing(sqlite3.connect(legacy_path)) as connection:
            lease_id = connection.execute(
                "SELECT lease_id FROM effect_leases"
            ).fetchone()
            observation_table = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'effect_lease_observations'
                """
            ).fetchone()
        self.assertIsNotNone(lease_id[0])
        self.assertEqual(observation_table, ("effect_lease_observations",))

    def test_atomic_claim_rejects_terminal_and_committed_result(self) -> None:
        terminal_store = run(SQLiteEventStore.open(self.path.parent / "terminal.db"))
        terminal_request = effect_request("ModelCallRequested", {})
        envelope = run(
            terminal_store.append(
                "run-terminal",
                -1,
                [terminal_request, Event("RunCompleted", {})],
            )
        )[0]
        terminal = run(
            terminal_store.try_claim_effect(
                subscription_name="effects",
                expected_checkpoint=0,
                request_global_position=envelope.global_position,
                operation_id=str(terminal_request.event_id),
                stream_id="run-terminal",
                request_event_type=terminal_request.event_type,
                worker_id="worker-a",
                lease_ttl_seconds=30,
                terminal_event_types=frozenset({"RunCompleted"}),
            )
        )
        self.assertEqual(terminal.status, "terminal")

        result_store = run(SQLiteEventStore.open(self.path.parent / "result.db"))
        result_request = effect_request("ModelCallRequested", {})
        result_envelope = run(
            result_store.append(
                "run-result",
                -1,
                [
                    result_request,
                    Event(
                        "ModelCallSucceeded",
                        {},
                        {
                            "operation_id": str(result_request.event_id),
                            "causation_id": str(result_request.event_id),
                        },
                    ),
                ],
            )
        )[0]
        completed = run(
            result_store.try_claim_effect(
                subscription_name="effects",
                expected_checkpoint=0,
                request_global_position=result_envelope.global_position,
                operation_id=str(result_request.event_id),
                stream_id="run-result",
                request_event_type=result_request.event_type,
                worker_id="worker-a",
                lease_ttl_seconds=30,
                terminal_event_types=frozenset(),
            )
        )
        self.assertEqual(completed.status, "already_completed")

    def test_confirm_rechecks_terminal_before_handler_admission(self) -> None:
        lease = self._claim("worker-a")
        assert lease is not None
        run(
            self.store.append(
                "run-1",
                0,
                [Event("RunCompleted", {})],
            )
        )
        confirmation = run(
            self.store.confirm_effect_claim(
                lease,
                terminal_event_types=frozenset({"RunCompleted"}),
            )
        )
        self.assertEqual(confirmation.status, "terminal")

    def test_observation_ledger_is_ordered_and_append_only(self) -> None:
        first = self._claim("worker-a")
        assert first is not None
        self.assertIsNone(self._claim("worker-b"))
        run(self.store.renew_effect_claim(first, lease_ttl_seconds=30))
        self._expire()
        second = self._claim("worker-b")
        assert second is not None
        with self.assertRaises(LeaseLostError):
            run(
                self.store.commit_fenced_subscription_batch(
                    first,
                    expected_checkpoint=0,
                    stream_id="run-1",
                    expected_stream_version=0,
                    events=[Event("ModelCallSucceeded", {})],
                    new_checkpoint=1,
                    terminal_event_types=frozenset(),
                )
            )
        observations = run(
            self.store._load_lease_observations_for_operation(
                str(self.request.event_id)
            )
        )
        self.assertEqual(
            [item.observation_kind for item in observations],
            [
                "claim_acquired",
                "busy",
                "renewal",
                "expiry",
                "takeover",
                "stale_commit_rejection",
            ],
        )
        self.assertEqual(
            [item.attempt_number for item in observations],
            [1, None, None, None, 2, None],
        )
        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "append-only"
            ):
                connection.execute(
                    "DELETE FROM effect_lease_observations"
                )


class InMemoryEffectLeaseTests(unittest.TestCase):
    def test_reference_adapter_fences_released_claim(self) -> None:
        store = InMemoryEventStore()
        request = effect_request("ModelCallRequested", {})
        envelope = run(store.append("run-1", -1, [request]))[0]
        claim_result = run(
            store.try_claim_effect(
                subscription_name="effects",
                expected_checkpoint=0,
                request_global_position=envelope.global_position,
                operation_id=str(request.event_id),
                stream_id="run-1",
                request_event_type=request.event_type,
                worker_id="worker-a",
                lease_ttl_seconds=30,
                terminal_event_types=frozenset(),
            )
        )
        claim = claim_result.lease
        assert claim is not None
        self.assertTrue(run(store.release_effect_claim(claim)))
        replacement_result = run(
            store.try_claim_effect(
                subscription_name="effects",
                expected_checkpoint=0,
                request_global_position=envelope.global_position,
                operation_id=str(request.event_id),
                stream_id="run-1",
                request_event_type=request.event_type,
                worker_id="worker-b",
                lease_ttl_seconds=30,
                terminal_event_types=frozenset(),
            )
        )
        replacement = replacement_result.lease
        assert replacement is not None
        self.assertEqual(replacement.fencing_token, 2)


if __name__ == "__main__":
    unittest.main()
