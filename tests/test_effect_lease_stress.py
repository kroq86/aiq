from __future__ import annotations

import asyncio
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aiq import Event, SQLiteEventStore, effect_request


class SQLiteEffectLeaseStressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._temp_dir.name) / "lease-stress.db"
        self.store = await SQLiteEventStore.open(self.path)

    async def asyncTearDown(self) -> None:
        self._temp_dir.cleanup()

    async def _claim(
        self,
        *,
        subscription: str,
        request,
        position: int,
        stream_id: str,
        worker: str,
    ):
        return await self.store.try_claim_effect(
            subscription_name=subscription,
            expected_checkpoint=0,
            request_global_position=position,
            operation_id=str(request.event_id),
            stream_id=stream_id,
            request_event_type=request.event_type,
            worker_id=worker,
            lease_ttl_seconds=30,
            terminal_event_types=frozenset(),
        )

    async def test_concurrent_claim_storm_has_one_owner_per_operation(
        self,
    ) -> None:
        operations = []
        for index in range(10):
            stream_id = f"run-{index}"
            request = effect_request("ModelCallRequested", {"index": index})
            envelope = (
                await self.store.append(stream_id, -1, [request])
            )[0]
            operations.append((stream_id, request, envelope.global_position))

        for index, (stream_id, request, position) in enumerate(operations):
            subscription = f"effects-{index}"
            results = await asyncio.gather(
                *(
                    self._claim(
                        subscription=subscription,
                        request=request,
                        position=position,
                        stream_id=stream_id,
                        worker=f"worker-{worker}",
                    )
                    for worker in range(4)
                )
            )
            self.assertEqual(
                [result.status for result in results].count("acquired"), 1
            )
            self.assertEqual(
                [result.status for result in results].count("busy"), 3
            )
            observations = (
                await self.store._load_lease_observations_for_operation(
                    str(request.event_id)
                )
            )
            self.assertEqual(
                [item.observation_kind for item in observations].count(
                    "claim_acquired"
                ),
                1,
            )
            self.assertEqual(
                [item.observation_kind for item in observations].count(
                    "busy"
                ),
                3,
            )

    async def test_seeded_crash_points_preserve_single_commit(self) -> None:
        randomizer = random.Random(5043)
        scenarios = [
            randomizer.choice(("release", "expire", "direct"))
            for _ in range(30)
        ]
        for index, scenario in enumerate(scenarios):
            stream_id = f"crash-{index}"
            subscription = f"crash-effects-{index}"
            request = effect_request("ModelCallRequested", {})
            envelope = (
                await self.store.append(stream_id, -1, [request])
            )[0]
            first_result = await self._claim(
                subscription=subscription,
                request=request,
                position=envelope.global_position,
                stream_id=stream_id,
                worker="worker-a",
            )
            first = first_result.lease
            assert first is not None
            owner = first
            if scenario == "release":
                self.assertTrue(
                    await self.store.release_effect_claim(first)
                )
                replacement = await self._claim(
                    subscription=subscription,
                    request=request,
                    position=envelope.global_position,
                    stream_id=stream_id,
                    worker="worker-b",
                )
                assert replacement.lease is not None
                owner = replacement.lease
            elif scenario == "expire":
                with sqlite3.connect(self.path) as connection:
                    connection.execute(
                        """
                        UPDATE effect_leases
                        SET lease_expires_at_unix = 0
                        WHERE subscription_name = ?
                        """,
                        (subscription,),
                    )
                    connection.commit()
                replacement = await self._claim(
                    subscription=subscription,
                    request=request,
                    position=envelope.global_position,
                    stream_id=stream_id,
                    worker="worker-b",
                )
                assert replacement.lease is not None
                owner = replacement.lease
            await self.store.commit_fenced_subscription_batch(
                owner,
                expected_checkpoint=0,
                stream_id=stream_id,
                expected_stream_version=0,
                events=(Event("ModelCallSucceeded", {}),),
                new_checkpoint=envelope.global_position,
                terminal_event_types=frozenset(),
            )
            history = await self.store.load(stream_id)
            self.assertEqual(
                sum(
                    item.event.event_type == "ModelCallSucceeded"
                    for item in history
                ),
                1,
            )

    async def test_shared_file_soak_keeps_database_consistent(self) -> None:
        for index in range(100):
            stream_id = f"soak-{index}"
            subscription = f"soak-effects-{index}"
            request = effect_request("ModelCallRequested", {})
            envelope = (
                await self.store.append(stream_id, -1, [request])
            )[0]
            result = await self._claim(
                subscription=subscription,
                request=request,
                position=envelope.global_position,
                stream_id=stream_id,
                worker=f"worker-{index % 4}",
            )
            lease = result.lease
            assert lease is not None
            await self.store.commit_fenced_subscription_batch(
                lease,
                expected_checkpoint=0,
                stream_id=stream_id,
                expected_stream_version=0,
                events=(Event("ModelCallSucceeded", {}),),
                new_checkpoint=envelope.global_position,
                terminal_event_types=frozenset(),
            )
        with sqlite3.connect(self.path) as connection:
            integrity = connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            attempts = connection.execute(
                "SELECT COUNT(*) FROM effect_attempts"
            ).fetchone()
            completed = connection.execute(
                """
                SELECT COUNT(*) FROM effect_leases
                WHERE status = 'completed'
                """
            ).fetchone()
        self.assertEqual(integrity, ("ok",))
        self.assertEqual(attempts, (100,))
        self.assertEqual(completed, (100,))


if __name__ == "__main__":
    unittest.main()
