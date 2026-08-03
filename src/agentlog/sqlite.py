from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Self
from uuid import UUID

from .attempts import EffectDispatchAttempt, _new_attempt
from .core import (
    CheckpointConflictError,
    DuplicateEventError,
    Event,
    EventEnvelope,
    JsonValue,
    VersionConflictError,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    global_position INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id TEXT NOT NULL,
    stream_version INTEGER NOT NULL CHECK (stream_version >= 0),
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type <> ''),
    event_data TEXT NOT NULL,
    metadata TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (stream_id, stream_version)
);

CREATE INDEX IF NOT EXISTS events_by_stream
ON events (stream_id, stream_version);

CREATE INDEX IF NOT EXISTS events_by_stream_global_position
ON events (stream_id, global_position);

CREATE TRIGGER IF NOT EXISTS events_are_append_only_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_are_append_only_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TABLE IF NOT EXISTS subscription_checkpoints (
    subscription_name TEXT PRIMARY KEY CHECK (subscription_name <> ''),
    global_position INTEGER NOT NULL CHECK (global_position >= 0),
    updated_at TEXT NOT NULL
);
"""

_ATTEMPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS effect_attempts (
    attempt_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL UNIQUE,
    operation_id TEXT NOT NULL CHECK (operation_id <> ''),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    stream_id TEXT NOT NULL CHECK (stream_id <> ''),
    request_event_type TEXT NOT NULL CHECK (request_event_type <> ''),
    request_global_position INTEGER NOT NULL
        CHECK (request_global_position > 0),
    subscription_name TEXT NOT NULL CHECK (subscription_name <> ''),
    started_at TEXT NOT NULL,
    UNIQUE (operation_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS effect_attempts_by_stream
ON effect_attempts (stream_id, attempt_sequence);

CREATE INDEX IF NOT EXISTS effect_attempts_by_operation
ON effect_attempts (operation_id, attempt_number);

CREATE TRIGGER IF NOT EXISTS effect_attempts_are_append_only_update
BEFORE UPDATE ON effect_attempts
BEGIN
    SELECT RAISE(ABORT, 'effect attempts are append-only');
END;

CREATE TRIGGER IF NOT EXISTS effect_attempts_are_append_only_delete
BEFORE DELETE ON effect_attempts
BEGIN
    SELECT RAISE(ABORT, 'effect attempts are append-only');
END;
"""


def _json_ready(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    return value


def _encode(value: Mapping[str, JsonValue]) -> str:
    return json.dumps(
        _json_ready(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class SQLiteEventStore:
    """Durable, append-only event store backed by one SQLite file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    async def open(cls, path: str | Path) -> Self:
        resolved = Path(path)
        if resolved == Path(":memory:"):
            raise ValueError("SQLiteEventStore requires a file path for durable storage")
        store = cls(resolved)
        await asyncio.to_thread(store._initialize)
        return store

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(_SCHEMA)
            connection.commit()
        finally:
            connection.close()

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

        encoded = tuple(
            (
                event,
                _encode(event.data),
                _encode(event.metadata),
            )
            for event in events
        )
        return await asyncio.to_thread(
            self._append_sync,
            stream_id,
            expected_version,
            encoded,
        )

    def _append_sync(
        self,
        stream_id: str,
        expected_version: int,
        encoded: tuple[tuple[Event, str, str], ...],
    ) -> tuple[EventEnvelope, ...]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT COALESCE(MAX(stream_version), -1) AS current_version
                FROM events
                WHERE stream_id = ?
                """,
                (stream_id,),
            ).fetchone()
            current_version = int(row["current_version"])
            if current_version != expected_version:
                raise VersionConflictError(
                    f"stream {stream_id!r} is at version {current_version}, "
                    f"expected {expected_version}"
                )

            ids = tuple(str(event.event_id) for event, _, _ in encoded)
            placeholders = ",".join("?" for _ in ids)
            duplicate = connection.execute(
                f"SELECT event_id FROM events WHERE event_id IN ({placeholders}) LIMIT 1",
                ids,
            ).fetchone()
            if duplicate is not None:
                raise DuplicateEventError(
                    f"event id already stored: {duplicate['event_id']}"
                )

            created_at = datetime.now(timezone.utc)
            appended: list[EventEnvelope] = []
            for offset, (event, event_data, metadata) in enumerate(encoded, start=1):
                stream_version = current_version + offset
                cursor = connection.execute(
                    """
                    INSERT INTO events (
                        stream_id,
                        stream_version,
                        event_id,
                        event_type,
                        event_data,
                        metadata,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stream_id,
                        stream_version,
                        str(event.event_id),
                        event.event_type,
                        event_data,
                        metadata,
                        created_at.isoformat(),
                    ),
                )
                appended.append(
                    EventEnvelope(
                        stream_id=stream_id,
                        stream_version=stream_version,
                        global_position=int(cursor.lastrowid),
                        event=event,
                        created_at=created_at,
                    )
                )
            connection.commit()
            return tuple(appended)
        except (VersionConflictError, DuplicateEventError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as error:
            connection.rollback()
            if "event_id" in str(error):
                raise DuplicateEventError(str(error)) from error
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def load(
        self,
        stream_id: str,
        *,
        after_version: int = -1,
    ) -> tuple[EventEnvelope, ...]:
        return await asyncio.to_thread(self._load_sync, stream_id, after_version)

    def _load_sync(
        self,
        stream_id: str,
        after_version: int,
    ) -> tuple[EventEnvelope, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT
                    global_position,
                    stream_id,
                    stream_version,
                    event_id,
                    event_type,
                    event_data,
                    metadata,
                    created_at
                FROM events
                WHERE stream_id = ? AND stream_version > ?
                ORDER BY stream_version
                """,
                (stream_id, after_version),
            ).fetchall()
        finally:
            connection.close()

        return tuple(
            _row_to_envelope(row)
            for row in rows
        )

    async def current_version(self, stream_id: str) -> int:
        if not stream_id:
            raise ValueError("stream_id must not be empty")
        return await asyncio.to_thread(self._current_version_sync, stream_id)

    def _current_version_sync(self, stream_id: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(stream_version), -1) AS current_version
                FROM events
                WHERE stream_id = ?
                """,
                (stream_id,),
            ).fetchone()
            return int(row["current_version"])
        finally:
            connection.close()

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
        return await asyncio.to_thread(
            self._load_stream_after_position_sync,
            stream_id,
            after_position,
            limit,
        )

    def _load_stream_after_position_sync(
        self,
        stream_id: str,
        after_position: int,
        limit: int,
    ) -> tuple[EventEnvelope, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT
                    global_position,
                    stream_id,
                    stream_version,
                    event_id,
                    event_type,
                    event_data,
                    metadata,
                    created_at
                FROM events
                WHERE stream_id = ? AND global_position > ?
                ORDER BY global_position
                LIMIT ?
                """,
                (stream_id, after_position, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_envelope(row) for row in rows)

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
        return await asyncio.to_thread(
            self._load_global_sync,
            after_position,
            limit,
        )

    def _load_global_sync(
        self,
        after_position: int,
        limit: int,
    ) -> tuple[EventEnvelope, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT
                    global_position,
                    stream_id,
                    stream_version,
                    event_id,
                    event_type,
                    event_data,
                    metadata,
                    created_at
                FROM events
                WHERE global_position > ?
                ORDER BY global_position
                LIMIT ?
                """,
                (after_position, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_envelope(row) for row in rows)

    async def load_checkpoint(self, subscription_name: str) -> int:
        if not subscription_name:
            raise ValueError("subscription_name must not be empty")
        return await asyncio.to_thread(self._load_checkpoint_sync, subscription_name)

    def _load_checkpoint_sync(self, subscription_name: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT global_position
                FROM subscription_checkpoints
                WHERE subscription_name = ?
                """,
                (subscription_name,),
            ).fetchone()
        finally:
            connection.close()
        return 0 if row is None else int(row["global_position"])

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
        encoded = tuple(
            (event, _encode(event.data), _encode(event.metadata))
            for event in events
        )
        return await asyncio.to_thread(
            self._commit_subscription_batch_sync,
            subscription_name,
            expected_checkpoint,
            stream_id,
            expected_stream_version,
            encoded,
            new_checkpoint,
        )

    def _commit_subscription_batch_sync(
        self,
        subscription_name: str,
        expected_checkpoint: int,
        stream_id: str,
        expected_stream_version: int,
        encoded: tuple[tuple[Event, str, str], ...],
        new_checkpoint: int,
    ) -> tuple[EventEnvelope, ...]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint_row = connection.execute(
                """
                SELECT global_position
                FROM subscription_checkpoints
                WHERE subscription_name = ?
                """,
                (subscription_name,),
            ).fetchone()
            current_checkpoint = (
                0
                if checkpoint_row is None
                else int(checkpoint_row["global_position"])
            )
            if current_checkpoint != expected_checkpoint:
                raise CheckpointConflictError(
                    f"subscription {subscription_name!r} is at position "
                    f"{current_checkpoint}, expected {expected_checkpoint}"
                )

            version_row = connection.execute(
                """
                SELECT COALESCE(MAX(stream_version), -1) AS current_version
                FROM events
                WHERE stream_id = ?
                """,
                (stream_id,),
            ).fetchone()
            current_version = int(version_row["current_version"])
            if encoded and current_version != expected_stream_version:
                raise VersionConflictError(
                    f"stream {stream_id!r} is at version {current_version}, "
                    f"expected {expected_stream_version}"
                )

            ids = tuple(str(event.event_id) for event, _, _ in encoded)
            if ids:
                placeholders = ",".join("?" for _ in ids)
                duplicate = connection.execute(
                    f"SELECT event_id FROM events "
                    f"WHERE event_id IN ({placeholders}) LIMIT 1",
                    ids,
                ).fetchone()
                if duplicate is not None:
                    raise DuplicateEventError(
                        f"event id already stored: {duplicate['event_id']}"
                    )

            created_at = datetime.now(timezone.utc)
            appended: list[EventEnvelope] = []
            for offset, (event, event_data, metadata) in enumerate(encoded, start=1):
                stream_version = current_version + offset
                cursor = connection.execute(
                    """
                    INSERT INTO events (
                        stream_id,
                        stream_version,
                        event_id,
                        event_type,
                        event_data,
                        metadata,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stream_id,
                        stream_version,
                        str(event.event_id),
                        event.event_type,
                        event_data,
                        metadata,
                        created_at.isoformat(),
                    ),
                )
                appended.append(
                    EventEnvelope(
                        stream_id=stream_id,
                        stream_version=stream_version,
                        global_position=int(cursor.lastrowid),
                        event=event,
                        created_at=created_at,
                    )
                )

            connection.execute(
                """
                INSERT INTO subscription_checkpoints (
                    subscription_name,
                    global_position,
                    updated_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(subscription_name) DO UPDATE SET
                    global_position = excluded.global_position,
                    updated_at = excluded.updated_at
                """,
                (
                    subscription_name,
                    new_checkpoint,
                    created_at.isoformat(),
                ),
            )
            connection.commit()
            return tuple(appended)
        except (
            CheckpointConflictError,
            DuplicateEventError,
            VersionConflictError,
        ):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as error:
            connection.rollback()
            if "event_id" in str(error):
                raise DuplicateEventError(str(error)) from error
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _row_to_envelope(row: sqlite3.Row) -> EventEnvelope:
    return EventEnvelope(
        stream_id=str(row["stream_id"]),
        stream_version=int(row["stream_version"]),
        global_position=int(row["global_position"]),
        event=Event(
            event_type=str(row["event_type"]),
            data=json.loads(row["event_data"]),
            metadata=json.loads(row["metadata"]),
            event_id=UUID(str(row["event_id"])),
        ),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


class SQLiteEffectAttemptStore:
    """Durable append-only operational attempt ledger.

    The table may share a SQLite file with ``SQLiteEventStore`` but uses a
    separate transaction and is not part of the domain event stream.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    async def open(cls, path: str | Path) -> Self:
        resolved = Path(path)
        if resolved == Path(":memory:"):
            raise ValueError(
                "SQLiteEffectAttemptStore requires a file path for durable storage"
            )
        store = cls(resolved)
        await asyncio.to_thread(store._initialize)
        return store

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(_ATTEMPT_SCHEMA)
            connection.commit()
        finally:
            connection.close()

    async def record_start(
        self,
        *,
        operation_id: str,
        stream_id: str,
        request_event_type: str,
        request_global_position: int,
        subscription_name: str,
    ) -> EffectDispatchAttempt:
        return await asyncio.to_thread(
            self._record_start_sync,
            operation_id,
            stream_id,
            request_event_type,
            request_global_position,
            subscription_name,
        )

    def _record_start_sync(
        self,
        operation_id: str,
        stream_id: str,
        request_event_type: str,
        request_global_position: int,
        subscription_name: str,
    ) -> EffectDispatchAttempt:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT
                    attempt_number,
                    stream_id,
                    request_event_type,
                    request_global_position
                FROM effect_attempts
                WHERE operation_id = ?
                ORDER BY attempt_number DESC
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if row is not None and (
                str(row["stream_id"]) != stream_id
                or str(row["request_event_type"]) != request_event_type
                or int(row["request_global_position"])
                != request_global_position
            ):
                raise ValueError(
                    "effect attempt request identity changed for operation "
                    f"{operation_id!r}"
                )
            attempt = _new_attempt(
                operation_id=operation_id,
                attempt_number=(
                    1 if row is None else int(row["attempt_number"]) + 1
                ),
                stream_id=stream_id,
                request_event_type=request_event_type,
                request_global_position=request_global_position,
                subscription_name=subscription_name,
            )
            connection.execute(
                """
                INSERT INTO effect_attempts (
                    attempt_id,
                    operation_id,
                    attempt_number,
                    stream_id,
                    request_event_type,
                    request_global_position,
                    subscription_name,
                    started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(attempt.attempt_id),
                    attempt.operation_id,
                    attempt.attempt_number,
                    attempt.stream_id,
                    attempt.request_event_type,
                    attempt.request_global_position,
                    attempt.subscription_name,
                    attempt.started_at.isoformat(),
                ),
            )
            connection.commit()
            return attempt
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def load_for_stream(
        self, stream_id: str
    ) -> tuple[EffectDispatchAttempt, ...]:
        if not stream_id:
            raise ValueError("stream_id must not be empty")
        return await asyncio.to_thread(self._load_for_stream_sync, stream_id)

    def _load_for_stream_sync(
        self, stream_id: str
    ) -> tuple[EffectDispatchAttempt, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT
                    attempt_id,
                    operation_id,
                    attempt_number,
                    stream_id,
                    request_event_type,
                    request_global_position,
                    subscription_name,
                    started_at
                FROM effect_attempts
                WHERE stream_id = ?
                ORDER BY attempt_sequence
                """,
                (stream_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_attempt(row) for row in rows)

    async def load_for_operation(
        self, operation_id: str
    ) -> tuple[EffectDispatchAttempt, ...]:
        if not operation_id:
            raise ValueError("operation_id must not be empty")
        return await asyncio.to_thread(
            self._load_for_operation_sync, operation_id
        )

    def _load_for_operation_sync(
        self, operation_id: str
    ) -> tuple[EffectDispatchAttempt, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT
                    attempt_id,
                    operation_id,
                    attempt_number,
                    stream_id,
                    request_event_type,
                    request_global_position,
                    subscription_name,
                    started_at
                FROM effect_attempts
                WHERE operation_id = ?
                ORDER BY attempt_number
                """,
                (operation_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_attempt(row) for row in rows)


def _row_to_attempt(row: sqlite3.Row) -> EffectDispatchAttempt:
    return EffectDispatchAttempt(
        attempt_id=UUID(str(row["attempt_id"])),
        operation_id=str(row["operation_id"]),
        attempt_number=int(row["attempt_number"]),
        stream_id=str(row["stream_id"]),
        request_event_type=str(row["request_event_type"]),
        request_global_position=int(row["request_global_position"]),
        subscription_name=str(row["subscription_name"]),
        started_at=datetime.fromisoformat(str(row["started_at"])),
    )


class SQLiteSubscriptionCheckpoints:
    """Mutable worker cursors stored alongside the immutable event log."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    async def open(cls, path: str | Path) -> Self:
        event_store = await SQLiteEventStore.open(path)
        return cls(event_store._path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    async def load(self, subscription_name: str) -> int:
        if not subscription_name:
            raise ValueError("subscription_name must not be empty")
        return await asyncio.to_thread(self._load_sync, subscription_name)

    def _load_sync(self, subscription_name: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT global_position
                FROM subscription_checkpoints
                WHERE subscription_name = ?
                """,
                (subscription_name,),
            ).fetchone()
        finally:
            connection.close()
        return 0 if row is None else int(row["global_position"])

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
        return await asyncio.to_thread(
            self._save_sync,
            subscription_name,
            global_position,
            expected_position,
        )

    def _save_sync(
        self,
        subscription_name: str,
        global_position: int,
        expected_position: int,
    ) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT global_position
                FROM subscription_checkpoints
                WHERE subscription_name = ?
                """,
                (subscription_name,),
            ).fetchone()
            current = 0 if row is None else int(row["global_position"])
            if current != expected_position:
                raise CheckpointConflictError(
                    f"subscription {subscription_name!r} is at position {current}, "
                    f"expected {expected_position}"
                )
            connection.execute(
                """
                INSERT INTO subscription_checkpoints (
                    subscription_name,
                    global_position,
                    updated_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(subscription_name) DO UPDATE SET
                    global_position = excluded.global_position,
                    updated_at = excluded.updated_at
                """,
                (
                    subscription_name,
                    global_position,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
            return global_position
        except CheckpointConflictError:
            connection.rollback()
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
