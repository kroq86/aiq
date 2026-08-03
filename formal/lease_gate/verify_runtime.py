"""Controlled SQLite traces for the lease-gate abstraction."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import tempfile
from pathlib import Path

from aiq import (
    Event,
    SQLiteEffectAttemptStore,
    SQLiteEventStore,
    effect_request,
)
from aiq.leases import LeaseLostError


async def verify() -> tuple[str, ...]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "lease.db"
        store = await SQLiteEventStore.open(path)
        request = effect_request("ModelCallRequested", {})
        envelope = (await store.append("run-1", -1, [request]))[0]
        identity = {
            "subscription_name": "effects",
            "expected_checkpoint": 0,
            "request_global_position": envelope.global_position,
            "operation_id": str(request.event_id),
            "stream_id": "run-1",
            "request_event_type": request.event_type,
            "lease_ttl_seconds": 30,
            "terminal_event_types": frozenset(),
        }
        first_result = await store.try_claim_effect(
            **identity, worker_id="A"
        )
        first = first_result.lease
        assert first is not None
        busy = await store.try_claim_effect(**identity, worker_id="B")
        assert busy.status == "busy"
        renewed = await store.renew_effect_claim(
            first, lease_ttl_seconds=30
        )
        assert renewed.fencing_token == first.fencing_token
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE effect_leases SET lease_expires_at_unix = 0"
            )
            connection.commit()
        second_result = await store.try_claim_effect(
            **identity, worker_id="B"
        )
        second = second_result.lease
        assert second is not None
        assert second.fencing_token > first.fencing_token
        try:
            await store.renew_effect_claim(first, lease_ttl_seconds=30)
        except LeaseLostError:
            pass
        else:
            raise AssertionError("stale worker renewed")
        try:
            await store.commit_fenced_subscription_batch(
                first,
                expected_checkpoint=0,
                stream_id="run-1",
                expected_stream_version=0,
                events=(Event("ModelCallSucceeded", {"worker": "A"}),),
                new_checkpoint=1,
                terminal_event_types=frozenset(),
            )
        except LeaseLostError:
            pass
        else:
            raise AssertionError("stale worker committed")
        await store.commit_fenced_subscription_batch(
            second,
            expected_checkpoint=0,
            stream_id="run-1",
            expected_stream_version=0,
            events=(Event("ModelCallSucceeded", {"worker": "B"}),),
            new_checkpoint=1,
            terminal_event_types=frozenset(),
        )
        attempts = await SQLiteEffectAttemptStore.open(path)
        recorded = await attempts.load_for_stream("run-1")
        assert [item.attempt_number for item in recorded] == [1, 2]
        assert await store.load_checkpoint("effects") == 1
        observations = await store._load_lease_observations_for_operation(
            str(request.event_id)
        )
        assert [item.observation_kind for item in observations] == [
            "claim_acquired",
            "busy",
            "renewal",
            "expiry",
            "takeover",
            "stale_ownership",
            "stale_commit_rejection",
        ]
        return (
            "claim",
            "contention",
            "renewal",
            "expiry_takeover",
            "stale_ownership",
            "stale_rejection",
            "fenced_commit",
            "observation_alignment",
        )


def main() -> int:
    argparse.ArgumentParser().parse_args()
    scenarios = asyncio.run(verify())
    print(f"PASS scenarios={','.join(scenarios)} count={len(scenarios)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
