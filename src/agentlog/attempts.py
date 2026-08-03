"""Operational effect-dispatch attempt facts and derived metrics.

These records are deliberately separate from the domain event log. A record
means that a dispatcher durably recorded an imminent handler invocation; it
does not prove that downstream I/O started, completed, or deduplicated a call.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class EffectDispatchAttempt:
    attempt_id: UUID
    operation_id: str
    attempt_number: int
    stream_id: str
    request_event_type: str
    request_global_position: int
    subscription_name: str
    started_at: datetime

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("operation_id must not be empty")
        if self.attempt_number <= 0:
            raise ValueError("attempt_number must be positive")
        if not self.stream_id:
            raise ValueError("stream_id must not be empty")
        if not self.request_event_type:
            raise ValueError("request_event_type must not be empty")
        if self.request_global_position <= 0:
            raise ValueError("request_global_position must be positive")
        if not self.subscription_name:
            raise ValueError("subscription_name must not be empty")
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")


class EffectAttemptStore(Protocol):
    async def record_start(
        self,
        *,
        operation_id: str,
        stream_id: str,
        request_event_type: str,
        request_global_position: int,
        subscription_name: str,
    ) -> EffectDispatchAttempt: ...

    async def load_for_stream(
        self, stream_id: str
    ) -> tuple[EffectDispatchAttempt, ...]: ...

    async def load_for_operation(
        self, operation_id: str
    ) -> tuple[EffectDispatchAttempt, ...]: ...


def _new_attempt(
    *,
    operation_id: str,
    attempt_number: int,
    stream_id: str,
    request_event_type: str,
    request_global_position: int,
    subscription_name: str,
    attempt_id: UUID | None = None,
    started_at: datetime | None = None,
) -> EffectDispatchAttempt:
    return EffectDispatchAttempt(
        attempt_id=attempt_id or uuid4(),
        operation_id=operation_id,
        attempt_number=attempt_number,
        stream_id=stream_id,
        request_event_type=request_event_type,
        request_global_position=request_global_position,
        subscription_name=subscription_name,
        started_at=started_at or datetime.now(timezone.utc),
    )


class InMemoryEffectAttemptStore:
    """Process-local reference adapter; unlike SQLite, it is not durable."""

    def __init__(self) -> None:
        self._attempts: list[EffectDispatchAttempt] = []
        self._attempt_count_by_operation: dict[str, int] = {}
        self._identity_by_operation: dict[str, tuple[str, str, int]] = {}
        self._lock = asyncio.Lock()

    async def record_start(
        self,
        *,
        operation_id: str,
        stream_id: str,
        request_event_type: str,
        request_global_position: int,
        subscription_name: str,
    ) -> EffectDispatchAttempt:
        async with self._lock:
            identity = (
                stream_id,
                request_event_type,
                request_global_position,
            )
            existing_identity = self._identity_by_operation.get(operation_id)
            if (
                existing_identity is not None
                and existing_identity != identity
            ):
                raise ValueError(
                    "effect attempt request identity changed for operation "
                    f"{operation_id!r}"
                )
            attempt_number = (
                self._attempt_count_by_operation.get(operation_id, 0) + 1
            )
            attempt = _new_attempt(
                operation_id=operation_id,
                attempt_number=attempt_number,
                stream_id=stream_id,
                request_event_type=request_event_type,
                request_global_position=request_global_position,
                subscription_name=subscription_name,
            )
            self._attempts.append(attempt)
            self._attempt_count_by_operation[operation_id] = attempt_number
            self._identity_by_operation[operation_id] = identity
            return attempt

    async def load_for_stream(
        self, stream_id: str
    ) -> tuple[EffectDispatchAttempt, ...]:
        if not stream_id:
            raise ValueError("stream_id must not be empty")
        async with self._lock:
            return tuple(
                attempt
                for attempt in self._attempts
                if attempt.stream_id == stream_id
            )

    async def load_for_operation(
        self, operation_id: str
    ) -> tuple[EffectDispatchAttempt, ...]:
        if not operation_id:
            raise ValueError("operation_id must not be empty")
        async with self._lock:
            return tuple(
                attempt
                for attempt in self._attempts
                if attempt.operation_id == operation_id
            )


@dataclass(frozen=True, slots=True)
class EffectAttemptMetrics:
    observation_kind: str
    attempt_count: int
    operation_count: int
    retried_operation_count: int
    retry_attempt_count: int
    max_attempts_per_operation: int
    attempt_count_by_event_type: dict[str, int]


def build_effect_attempt_metrics(
    attempts: Sequence[EffectDispatchAttempt],
) -> EffectAttemptMetrics:
    by_operation: dict[str, list[EffectDispatchAttempt]] = defaultdict(list)
    attempt_ids: set[UUID] = set()
    for attempt in attempts:
        if attempt.attempt_id in attempt_ids:
            raise ValueError(
                f"duplicate effect attempt id: {attempt.attempt_id}"
            )
        attempt_ids.add(attempt.attempt_id)
        by_operation[attempt.operation_id].append(attempt)

    for operation_id, operation_attempts in by_operation.items():
        expected_numbers = list(range(1, len(operation_attempts) + 1))
        actual_numbers = sorted(
            attempt.attempt_number for attempt in operation_attempts
        )
        if actual_numbers != expected_numbers:
            raise ValueError(
                "effect attempt numbers must be contiguous from 1 for "
                f"operation {operation_id!r}: {actual_numbers}"
            )
        if len({attempt.stream_id for attempt in operation_attempts}) != 1:
            raise ValueError(
                f"effect attempts changed stream for operation {operation_id!r}"
            )
        if (
            len(
                {
                    attempt.request_event_type
                    for attempt in operation_attempts
                }
            )
            != 1
        ):
            raise ValueError(
                "effect attempts changed request event type for operation "
                f"{operation_id!r}"
            )
        if (
            len(
                {
                    attempt.request_global_position
                    for attempt in operation_attempts
                }
            )
            != 1
        ):
            raise ValueError(
                "effect attempts changed request position for operation "
                f"{operation_id!r}"
            )

    counts = [len(items) for items in by_operation.values()]
    by_event_type = Counter(
        attempt.request_event_type for attempt in attempts
    )
    return EffectAttemptMetrics(
        observation_kind="durable-dispatch-attempt",
        attempt_count=len(attempts),
        operation_count=len(by_operation),
        retried_operation_count=sum(count > 1 for count in counts),
        retry_attempt_count=sum(max(0, count - 1) for count in counts),
        max_attempts_per_operation=max(counts, default=0),
        attempt_count_by_event_type=dict(sorted(by_event_type.items())),
    )
