from __future__ import annotations

import asyncio
import math
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Protocol, TypeVar
from uuid import UUID, uuid4


JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | tuple["JsonValue", ...]
    | Mapping[str, "JsonValue"]
)
State = TypeVar("State")


class VersionConflictError(Exception):
    """The stream changed after the caller read it."""


class DuplicateEventError(Exception):
    """An event id must identify at most one stored event."""


class CheckpointConflictError(Exception):
    """A subscription checkpoint changed after the caller read it."""


def _freeze(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Event numbers must be finite JSON values")
        return value
    if isinstance(value, Mapping):
        if non_string_keys := [key for key in value if not isinstance(key, str)]:
            raise TypeError(
                "Event object keys must be strings, "
                f"got {type(non_string_keys[0]).__name__}"
            )
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise TypeError(f"Event data must be JSON-compatible, got {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class Event:
    event_type: str
    data: Mapping[str, JsonValue]
    metadata: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))
    event_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.event_type:
            raise ValueError("event_type must not be empty")
        if not isinstance(self.event_id, UUID):
            raise TypeError("event_id must be a UUID")
        object.__setattr__(self, "data", _freeze(self.data))
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    stream_id: str
    stream_version: int
    global_position: int
    event: Event
    created_at: datetime


class EventStore(Protocol):
    async def append(
        self,
        stream_id: str,
        expected_version: int,
        events: Sequence[Event],
    ) -> tuple[EventEnvelope, ...]: ...

    async def load(
        self,
        stream_id: str,
        *,
        after_version: int = -1,
    ) -> tuple[EventEnvelope, ...]: ...

    async def load_stream_after_position(
        self,
        stream_id: str,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> tuple[EventEnvelope, ...]: ...

    async def current_version(self, stream_id: str) -> int: ...

    async def load_global(
        self,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> tuple[EventEnvelope, ...]: ...

    async def load_checkpoint(self, subscription_name: str) -> int: ...

    async def commit_subscription_batch(
        self,
        *,
        subscription_name: str,
        expected_checkpoint: int,
        stream_id: str,
        expected_stream_version: int,
        events: Sequence[Event],
        new_checkpoint: int,
    ) -> tuple[EventEnvelope, ...]:
        """`expected_stream_version` is only asserted when `events` is
        non-empty. A checkpoint-only advance (`events=()`) never conflicts
        with a concurrent append to `stream_id` -- there is no stream fact
        being asserted, so there is nothing for the caller's snapshot to
        go stale against."""
        ...


class SubscriptionCheckpointStore(Protocol):
    async def load(self, subscription_name: str) -> int: ...

    async def save(
        self,
        subscription_name: str,
        global_position: int,
        *,
        expected_position: int,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class _InMemoryLeaseRow:
    operation_id: str
    stream_id: str
    request_event_type: str
    lease_id: UUID
    worker_id: str | None
    fencing_token: int
    status: str
    expires_at: datetime | None


class InMemoryEventStore:
    """Reference adapter for tests and local execution; it is not durable."""

    def __init__(
        self, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._streams: dict[str, list[EventEnvelope]] = {}
        self._global_events: list[EventEnvelope] = []
        self._event_ids: set[UUID] = set()
        self._checkpoints: dict[str, int] = {}
        self._effect_leases: dict[tuple[str, int], _InMemoryLeaseRow] = {}
        self._effect_attempts: list[Any] = []
        self._effect_lease_observations: list[Any] = []
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._global_position = 0
        self._lock = asyncio.Lock()

    async def append(
        self,
        stream_id: str,
        expected_version: int,
        events: Sequence[Event],
    ) -> tuple[EventEnvelope, ...]:
        if not stream_id:
            raise ValueError("stream_id must not be empty")
        if not events:
            return ()

        event_ids = [event.event_id for event in events]
        if len(event_ids) != len(set(event_ids)):
            raise DuplicateEventError("append batch contains duplicate event ids")

        async with self._lock:
            stream = self._streams.get(stream_id, [])
            current_version = len(stream) - 1
            if current_version != expected_version:
                raise VersionConflictError(
                    f"stream {stream_id!r} is at version {current_version}, "
                    f"expected {expected_version}"
                )
            duplicates = self._event_ids.intersection(event_ids)
            if duplicates:
                raise DuplicateEventError(f"event id already stored: {next(iter(duplicates))}")

            now = datetime.now(timezone.utc)
            appended = tuple(
                EventEnvelope(
                    stream_id=stream_id,
                    stream_version=current_version + offset,
                    global_position=self._global_position + offset,
                    event=event,
                    created_at=now,
                )
                for offset, event in enumerate(events, start=1)
            )
            self._global_position += len(appended)
            self._event_ids.update(event_ids)
            self._streams.setdefault(stream_id, []).extend(appended)
            self._global_events.extend(appended)
            return appended

    async def load(
        self,
        stream_id: str,
        *,
        after_version: int = -1,
    ) -> tuple[EventEnvelope, ...]:
        async with self._lock:
            return tuple(
                envelope
                for envelope in self._streams.get(stream_id, ())
                if envelope.stream_version > after_version
            )

    async def load_stream_after_position(
        self,
        stream_id: str,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> tuple[EventEnvelope, ...]:
        if not stream_id:
            raise ValueError("stream_id must not be empty")
        if after_position < 0:
            raise ValueError("after_position must be non-negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._lock:
            stream = self._streams.get(stream_id, ())
            start = bisect_right(
                stream,
                after_position,
                key=lambda envelope: envelope.global_position,
            )
            return tuple(stream[start : start + limit])

    async def current_version(self, stream_id: str) -> int:
        if not stream_id:
            raise ValueError("stream_id must not be empty")
        async with self._lock:
            return len(self._streams.get(stream_id, ())) - 1

    async def load_global(
        self,
        *,
        after_position: int = 0,
        limit: int = 100,
    ) -> tuple[EventEnvelope, ...]:
        if after_position < 0:
            raise ValueError("after_position must be non-negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._lock:
            start = bisect_right(
                self._global_events,
                after_position,
                key=lambda envelope: envelope.global_position,
            )
            return tuple(self._global_events[start : start + limit])

    async def load_checkpoint(self, subscription_name: str) -> int:
        if not subscription_name:
            raise ValueError("subscription_name must not be empty")
        async with self._lock:
            return self._checkpoints.get(subscription_name, 0)

    async def commit_subscription_batch(
        self,
        *,
        subscription_name: str,
        expected_checkpoint: int,
        stream_id: str,
        expected_stream_version: int,
        events: Sequence[Event],
        new_checkpoint: int,
    ) -> tuple[EventEnvelope, ...]:
        if not subscription_name or not stream_id:
            raise ValueError("subscription_name and stream_id must not be empty")
        if expected_checkpoint < 0 or new_checkpoint < 0:
            raise ValueError("checkpoint positions must be non-negative")
        event_ids = [event.event_id for event in events]
        if len(event_ids) != len(set(event_ids)):
            raise DuplicateEventError("append batch contains duplicate event ids")

        async with self._lock:
            checkpoint = self._checkpoints.get(subscription_name, 0)
            if checkpoint != expected_checkpoint:
                raise CheckpointConflictError(
                    f"subscription {subscription_name!r} is at position {checkpoint}, "
                    f"expected {expected_checkpoint}"
                )
            stream = self._streams.get(stream_id, [])
            current_version = len(stream) - 1
            if events and current_version != expected_stream_version:
                raise VersionConflictError(
                    f"stream {stream_id!r} is at version {current_version}, "
                    f"expected {expected_stream_version}"
                )
            duplicates = self._event_ids.intersection(event_ids)
            if duplicates:
                raise DuplicateEventError(f"event id already stored: {next(iter(duplicates))}")

            now = datetime.now(timezone.utc)
            appended = tuple(
                EventEnvelope(
                    stream_id=stream_id,
                    stream_version=current_version + offset,
                    global_position=self._global_position + offset,
                    event=event,
                    created_at=now,
                )
                for offset, event in enumerate(events, start=1)
            )
            self._global_position += len(appended)
            self._event_ids.update(event_ids)
            self._streams.setdefault(stream_id, []).extend(appended)
            self._global_events.extend(appended)
            self._checkpoints[subscription_name] = new_checkpoint
            return appended

    def _record_lease_observation_unlocked(
        self,
        *,
        kind: str,
        row: _InMemoryLeaseRow,
        subscription_name: str,
        request_global_position: int,
        worker_id: str,
        observed_at: datetime,
        attempt: Any = None,
    ) -> None:
        from .leases import EffectLeaseObservation

        self._effect_lease_observations.append(
            EffectLeaseObservation(
                observation_sequence=len(
                    self._effect_lease_observations
                )
                + 1,
                observed_at=observed_at,
                observation_kind=kind,
                subscription_name=subscription_name,
                request_global_position=request_global_position,
                operation_id=row.operation_id,
                stream_id=row.stream_id,
                request_event_type=row.request_event_type,
                worker_id=worker_id,
                lease_id=row.lease_id,
                fencing_token=row.fencing_token,
                attempt_id=(
                    None if attempt is None else attempt.attempt_id
                ),
                attempt_number=(
                    None if attempt is None else attempt.attempt_number
                ),
            )
        )

    async def try_claim_effect(
        self,
        *,
        subscription_name: str,
        expected_checkpoint: int,
        request_global_position: int,
        operation_id: str,
        stream_id: str,
        request_event_type: str,
        worker_id: str,
        lease_ttl_seconds: float,
        terminal_event_types: frozenset[str],
    ):
        from datetime import timedelta

        from .attempts import _new_attempt
        from .leases import EffectClaimResult, EffectLease

        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be > 0")
        async with self._lock:
            checkpoint = self._checkpoints.get(subscription_name, 0)
            if checkpoint != expected_checkpoint:
                raise CheckpointConflictError(
                    f"subscription {subscription_name!r} is at position "
                    f"{checkpoint}, expected {expected_checkpoint}"
                )
            now = self._clock()
            if now.tzinfo is None:
                raise ValueError("in-memory lease clock must be timezone-aware")
            key = (subscription_name, request_global_position)
            request = next(
                (
                    envelope
                    for envelope in self._global_events
                    if envelope.global_position
                    == request_global_position
                ),
                None,
            )
            if request is None or (
                request.stream_id != stream_id
                or request.event.event_type != request_event_type
            ):
                raise ValueError("effect claim request identity is invalid")
            history = self._streams.get(stream_id, ())
            if any(
                envelope.event.event_type in terminal_event_types
                for envelope in history
            ):
                return EffectClaimResult("terminal")
            if any(
                envelope.event.metadata.get("operation_id")
                == operation_id
                and envelope.event.metadata.get("causation_id")
                == operation_id
                for envelope in history
            ):
                return EffectClaimResult("already_completed")
            row = self._effect_leases.get(key)
            if row is not None:
                if (
                    row.operation_id != operation_id
                    or row.stream_id != stream_id
                    or row.request_event_type != request_event_type
                ):
                    raise ValueError("effect lease request identity changed")
                if (
                    row.status == "claimed"
                    and row.expires_at is not None
                    and row.expires_at > now
                ):
                    self._record_lease_observation_unlocked(
                        kind="busy",
                        row=row,
                        subscription_name=subscription_name,
                        request_global_position=request_global_position,
                        worker_id=worker_id,
                        observed_at=now,
                    )
                    return EffectClaimResult("busy")
                if row.status == "completed":
                    return EffectClaimResult("already_completed")
                token = row.fencing_token + 1
            else:
                token = 1
            matching = [
                attempt
                for attempt in self._effect_attempts
                if attempt.operation_id == operation_id
            ]
            if matching:
                previous = matching[-1]
                if (
                    previous.stream_id != stream_id
                    or previous.request_event_type != request_event_type
                    or previous.request_global_position
                    != request_global_position
                ):
                    raise ValueError(
                        "effect attempt request identity changed for operation "
                        f"{operation_id!r}"
                    )
            attempt = _new_attempt(
                operation_id=operation_id,
                attempt_number=len(matching) + 1,
                stream_id=stream_id,
                request_event_type=request_event_type,
                request_global_position=request_global_position,
                subscription_name=subscription_name,
                started_at=now,
            )
            expires_at = now + timedelta(seconds=lease_ttl_seconds)
            lease_id = uuid4()
            self._effect_attempts.append(attempt)
            if row is not None and row.status == "claimed":
                self._record_lease_observation_unlocked(
                    kind="expiry",
                    row=row,
                    subscription_name=subscription_name,
                    request_global_position=request_global_position,
                    worker_id=str(row.worker_id),
                    observed_at=now,
                )
            self._effect_leases[key] = _InMemoryLeaseRow(
                operation_id=operation_id,
                stream_id=stream_id,
                request_event_type=request_event_type,
                lease_id=lease_id,
                worker_id=worker_id,
                fencing_token=token,
                status="claimed",
                expires_at=expires_at,
            )
            current_row = self._effect_leases[key]
            self._record_lease_observation_unlocked(
                kind="claim_acquired" if row is None else "takeover",
                row=current_row,
                subscription_name=subscription_name,
                request_global_position=request_global_position,
                worker_id=worker_id,
                observed_at=now,
                attempt=attempt,
            )
            lease = EffectLease(
                lease_id=lease_id,
                subscription_name=subscription_name,
                request_global_position=request_global_position,
                operation_id=operation_id,
                stream_id=stream_id,
                request_event_type=request_event_type,
                worker_id=worker_id,
                fencing_token=token,
                lease_expires_at=expires_at,
                attempt=attempt,
            )
            return EffectClaimResult("acquired", lease)

    async def confirm_effect_claim(
        self,
        lease,
        *,
        terminal_event_types: frozenset[str],
    ):
        from .leases import EffectClaimConfirmation, LeaseLostError

        async with self._lock:
            now = self._clock()
            key = (
                lease.subscription_name,
                lease.request_global_position,
            )
            row = self._effect_leases.get(key)
            if (
                row is None
                or row.status != "claimed"
                or row.lease_id != lease.lease_id
                or row.worker_id != lease.worker_id
                or row.fencing_token != lease.fencing_token
                or row.expires_at is None
                or row.expires_at <= now
            ):
                stale_row = _InMemoryLeaseRow(
                    operation_id=lease.operation_id,
                    stream_id=lease.stream_id,
                    request_event_type=lease.request_event_type,
                    lease_id=lease.lease_id,
                    worker_id=lease.worker_id,
                    fencing_token=lease.fencing_token,
                    status="claimed",
                    expires_at=lease.lease_expires_at,
                )
                self._record_lease_observation_unlocked(
                    kind="stale_ownership",
                    row=stale_row,
                    subscription_name=lease.subscription_name,
                    request_global_position=lease.request_global_position,
                    worker_id=lease.worker_id,
                    observed_at=now,
                )
                raise LeaseLostError("effect lease is stale or expired")
            history = self._streams.get(lease.stream_id, ())
            if any(
                envelope.event.event_type in terminal_event_types
                for envelope in history
            ):
                return EffectClaimConfirmation("terminal")
            if any(
                envelope.event.metadata.get("operation_id")
                == lease.operation_id
                and envelope.event.metadata.get("causation_id")
                == lease.operation_id
                for envelope in history
            ):
                return EffectClaimConfirmation("already_completed")
            return EffectClaimConfirmation("confirmed")

    async def renew_effect_claim(
        self, lease, *, lease_ttl_seconds: float
    ):
        from datetime import timedelta

        from .leases import EffectLease, LeaseLostError

        async with self._lock:
            now = self._clock()
            key = (
                lease.subscription_name,
                lease.request_global_position,
            )
            row = self._effect_leases.get(key)
            if (
                row is None
                or row.status != "claimed"
                or row.lease_id != lease.lease_id
                or row.worker_id != lease.worker_id
                or row.fencing_token != lease.fencing_token
                or row.expires_at is None
                or row.expires_at <= now
            ):
                self._record_lease_observation_unlocked(
                    kind="stale_ownership",
                    row=_InMemoryLeaseRow(
                        operation_id=lease.operation_id,
                        stream_id=lease.stream_id,
                        request_event_type=lease.request_event_type,
                        lease_id=lease.lease_id,
                        worker_id=lease.worker_id,
                        fencing_token=lease.fencing_token,
                        status="claimed",
                        expires_at=lease.lease_expires_at,
                    ),
                    subscription_name=lease.subscription_name,
                    request_global_position=lease.request_global_position,
                    worker_id=lease.worker_id,
                    observed_at=now,
                )
                raise LeaseLostError("effect lease is stale or expired")
            expires_at = now + timedelta(seconds=lease_ttl_seconds)
            self._effect_leases[key] = _InMemoryLeaseRow(
                operation_id=row.operation_id,
                stream_id=row.stream_id,
                request_event_type=row.request_event_type,
                lease_id=row.lease_id,
                worker_id=row.worker_id,
                fencing_token=row.fencing_token,
                status=row.status,
                expires_at=expires_at,
            )
            self._record_lease_observation_unlocked(
                kind="renewal",
                row=self._effect_leases[key],
                subscription_name=lease.subscription_name,
                request_global_position=lease.request_global_position,
                worker_id=lease.worker_id,
                observed_at=now,
            )
            return EffectLease(
                lease_id=lease.lease_id,
                subscription_name=lease.subscription_name,
                request_global_position=lease.request_global_position,
                operation_id=lease.operation_id,
                stream_id=lease.stream_id,
                request_event_type=lease.request_event_type,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                lease_expires_at=expires_at,
                attempt=lease.attempt,
            )

    async def release_effect_claim(self, lease) -> bool:
        async with self._lock:
            now = self._clock()
            key = (
                lease.subscription_name,
                lease.request_global_position,
            )
            row = self._effect_leases.get(key)
            if (
                row is None
                or row.status != "claimed"
                or row.lease_id != lease.lease_id
                or row.worker_id != lease.worker_id
                or row.fencing_token != lease.fencing_token
                or row.expires_at is None
                or row.expires_at <= now
            ):
                return False
            self._effect_leases[key] = _InMemoryLeaseRow(
                operation_id=row.operation_id,
                stream_id=row.stream_id,
                request_event_type=row.request_event_type,
                lease_id=row.lease_id,
                worker_id=None,
                fencing_token=row.fencing_token,
                status="released",
                expires_at=None,
            )
            return True

    async def commit_fenced_subscription_batch(
        self,
        lease,
        *,
        expected_checkpoint: int,
        stream_id: str,
        expected_stream_version: int,
        events: Sequence[Event],
        new_checkpoint: int,
        terminal_event_types: frozenset[str],
    ) -> tuple[EventEnvelope, ...]:
        from .leases import LeaseLostError

        async with self._lock:
            key = (
                lease.subscription_name,
                lease.request_global_position,
            )
            row = self._effect_leases.get(key)
            now = self._clock()
            if (
                row is None
                or row.status != "claimed"
                or row.lease_id != lease.lease_id
                or row.worker_id != lease.worker_id
                or row.fencing_token != lease.fencing_token
                or row.expires_at is None
                or row.expires_at <= now
            ):
                stale_row = _InMemoryLeaseRow(
                    operation_id=lease.operation_id,
                    stream_id=lease.stream_id,
                    request_event_type=lease.request_event_type,
                    lease_id=lease.lease_id,
                    worker_id=lease.worker_id,
                    fencing_token=lease.fencing_token,
                    status="claimed",
                    expires_at=lease.lease_expires_at,
                )
                self._record_lease_observation_unlocked(
                    kind="stale_commit_rejection",
                    row=stale_row,
                    subscription_name=lease.subscription_name,
                    request_global_position=lease.request_global_position,
                    worker_id=lease.worker_id,
                    observed_at=now,
                )
                raise LeaseLostError("effect lease is stale or expired")
            current_history = self._streams.get(stream_id, ())
            if any(
                envelope.event.event_type in terminal_event_types
                for envelope in current_history
            ) or any(
                envelope.event.metadata.get("operation_id")
                == lease.operation_id
                and envelope.event.metadata.get("causation_id")
                == lease.operation_id
                for envelope in current_history
            ):
                events = ()
            checkpoint = self._checkpoints.get(lease.subscription_name, 0)
            if checkpoint != expected_checkpoint:
                raise CheckpointConflictError(
                    f"subscription {lease.subscription_name!r} is at "
                    f"position {checkpoint}, expected {expected_checkpoint}"
                )
            stream = self._streams.get(stream_id, [])
            current_version = len(stream) - 1
            if events and current_version != expected_stream_version:
                raise VersionConflictError(
                    f"stream {stream_id!r} is at version {current_version}, "
                    f"expected {expected_stream_version}"
                )
            event_ids = [event.event_id for event in events]
            duplicates = self._event_ids.intersection(event_ids)
            if len(event_ids) != len(set(event_ids)) or duplicates:
                raise DuplicateEventError("fenced batch has duplicate event ids")
            appended = tuple(
                EventEnvelope(
                    stream_id=stream_id,
                    stream_version=current_version + offset,
                    global_position=self._global_position + offset,
                    event=event,
                    created_at=now,
                )
                for offset, event in enumerate(events, start=1)
            )
            self._global_position += len(appended)
            self._event_ids.update(event_ids)
            self._streams.setdefault(stream_id, []).extend(appended)
            self._global_events.extend(appended)
            self._checkpoints[lease.subscription_name] = new_checkpoint
            self._effect_leases[key] = _InMemoryLeaseRow(
                operation_id=row.operation_id,
                stream_id=row.stream_id,
                request_event_type=row.request_event_type,
                lease_id=row.lease_id,
                worker_id=None,
                fencing_token=row.fencing_token,
                status="completed",
                expires_at=None,
            )
            return appended

    async def load_effect_attempts_for_stream(self, stream_id: str):
        async with self._lock:
            return tuple(
                attempt
                for attempt in self._effect_attempts
                if attempt.stream_id == stream_id
            )

    async def _load_lease_observations_for_operation(
        self, operation_id: str
    ):
        async with self._lock:
            return tuple(
                observation
                for observation in self._effect_lease_observations
                if observation.operation_id == operation_id
            )


class InMemorySubscriptionCheckpoints:
    def __init__(self) -> None:
        self._positions: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def load(self, subscription_name: str) -> int:
        if not subscription_name:
            raise ValueError("subscription_name must not be empty")
        async with self._lock:
            return self._positions.get(subscription_name, 0)

    async def save(
        self,
        subscription_name: str,
        global_position: int,
        *,
        expected_position: int,
    ) -> int:
        if not subscription_name:
            raise ValueError("subscription_name must not be empty")
        if global_position < 0 or expected_position < 0:
            raise ValueError("checkpoint positions must be non-negative")
        async with self._lock:
            current = self._positions.get(subscription_name, 0)
            if current != expected_position:
                raise CheckpointConflictError(
                    f"subscription {subscription_name!r} is at position {current}, "
                    f"expected {expected_position}"
                )
            self._positions[subscription_name] = global_position
            return global_position


def replay(
    initial_state: State,
    events: Sequence[EventEnvelope],
    evolve: Callable[[State, Event], State],
) -> State:
    state = initial_state
    for envelope in events:
        state = evolve(state, envelope.event)
    return state
