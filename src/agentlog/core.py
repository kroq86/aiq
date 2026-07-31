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


class InMemoryEventStore:
    """Reference adapter for tests and local execution; it is not durable."""

    def __init__(self) -> None:
        self._streams: dict[str, list[EventEnvelope]] = {}
        self._global_events: list[EventEnvelope] = []
        self._event_ids: set[UUID] = set()
        self._checkpoints: dict[str, int] = {}
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
