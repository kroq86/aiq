from __future__ import annotations

import asyncio
import json
import queue
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

from .attempts import EffectDispatchAttempt, _new_attempt
from .core import (
    CheckpointConflictError,
    DuplicateEventError,
    Event,
    EventEnvelope,
    JsonValue,
    VersionConflictError,
)
from .leases import (
    EffectClaimConfirmation,
    EffectClaimResult,
    EffectLease,
    EffectLeaseObservation,
    LeaseLostError,
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

_LEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS effect_leases (
    subscription_name TEXT NOT NULL CHECK (subscription_name <> ''),
    request_global_position INTEGER NOT NULL
        CHECK (request_global_position > 0),
    operation_id TEXT NOT NULL CHECK (operation_id <> ''),
    stream_id TEXT NOT NULL CHECK (stream_id <> ''),
    request_event_type TEXT NOT NULL CHECK (request_event_type <> ''),
    lease_id TEXT NOT NULL,
    worker_id TEXT,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    status TEXT NOT NULL
        CHECK (status IN ('claimed', 'released', 'completed')),
    lease_expires_at_unix REAL,
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subscription_name, request_global_position)
);

CREATE INDEX IF NOT EXISTS effect_leases_by_worker
ON effect_leases (worker_id, lease_expires_at_unix);

CREATE UNIQUE INDEX IF NOT EXISTS effect_leases_by_lease_id
ON effect_leases (lease_id);
"""

_LEASE_OBSERVATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS effect_lease_observations (
    observation_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at_unix REAL NOT NULL,
    observation_kind TEXT NOT NULL CHECK (observation_kind IN (
        'claim_acquired', 'busy', 'expiry', 'renewal', 'takeover',
        'stale_ownership', 'stale_commit_rejection'
    )),
    subscription_name TEXT NOT NULL CHECK (subscription_name <> ''),
    request_global_position INTEGER NOT NULL
        CHECK (request_global_position > 0),
    operation_id TEXT NOT NULL CHECK (operation_id <> ''),
    stream_id TEXT NOT NULL CHECK (stream_id <> ''),
    request_event_type TEXT NOT NULL CHECK (request_event_type <> ''),
    worker_id TEXT NOT NULL CHECK (worker_id <> ''),
    lease_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    attempt_id TEXT,
    attempt_number INTEGER
        CHECK (attempt_number IS NULL OR attempt_number > 0)
);

CREATE INDEX IF NOT EXISTS effect_lease_observations_by_operation
ON effect_lease_observations (operation_id, observation_sequence);

CREATE INDEX IF NOT EXISTS effect_lease_observations_by_request
ON effect_lease_observations (
    subscription_name, request_global_position, observation_sequence
);

CREATE INDEX IF NOT EXISTS effect_lease_observations_by_stream
ON effect_lease_observations (stream_id, observation_sequence);

CREATE TRIGGER IF NOT EXISTS effect_lease_observations_append_only_update
BEFORE UPDATE ON effect_lease_observations
BEGIN
    SELECT RAISE(ABORT, 'effect lease observations are append-only');
END;

CREATE TRIGGER IF NOT EXISTS effect_lease_observations_append_only_delete
BEFORE DELETE ON effect_lease_observations
BEGIN
    SELECT RAISE(ABORT, 'effect lease observations are append-only');
END;
"""

_DB_NOW_UNIX = "(julianday('now') - 2440587.5) * 86400.0"


def _database_now_unix(connection: sqlite3.Connection) -> float:
    return float(
        connection.execute(
            f"SELECT {_DB_NOW_UNIX} AS now_unix"
        ).fetchone()["now_unix"]
    )


def _next_released_fencing_token(current: int) -> int:
    return current + 1


def _next_takeover_fencing_token(current: int) -> int:
    return current + 1


def _insert_lease_observation(
    connection: sqlite3.Connection,
    *,
    observed_at_unix: float,
    observation_kind: str,
    subscription_name: str,
    request_global_position: int,
    operation_id: str,
    stream_id: str,
    request_event_type: str,
    worker_id: str,
    lease_id: UUID,
    fencing_token: int,
    attempt: EffectDispatchAttempt | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO effect_lease_observations (
            observed_at_unix, observation_kind, subscription_name,
            request_global_position, operation_id, stream_id,
            request_event_type, worker_id, lease_id, fencing_token,
            attempt_id, attempt_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observed_at_unix,
            observation_kind,
            subscription_name,
            request_global_position,
            operation_id,
            stream_id,
            request_event_type,
            worker_id,
            str(lease_id),
            fencing_token,
            None if attempt is None else str(attempt.attempt_id),
            None if attempt is None else attempt.attempt_number,
        ),
    )


def _stream_is_terminal(
    connection: sqlite3.Connection,
    *,
    stream_id: str,
    terminal_event_types: frozenset[str],
) -> bool:
    if not terminal_event_types:
        return False
    event_types = tuple(sorted(terminal_event_types))
    placeholders = ",".join("?" for _ in event_types)
    return (
        connection.execute(
            f"""
            SELECT 1 FROM events
            WHERE stream_id = ? AND event_type IN ({placeholders})
            LIMIT 1
            """,
            (stream_id, *event_types),
        ).fetchone()
        is not None
    )


def _operation_has_committed_result(
    connection: sqlite3.Connection,
    *,
    stream_id: str,
    operation_id: str,
) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM events
            WHERE stream_id = ?
              AND json_extract(metadata, '$.operation_id') = ?
              AND json_extract(metadata, '$.causation_id') = ?
            LIMIT 1
            """,
            (stream_id, operation_id, operation_id),
        ).fetchone()
        is not None
    )


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


class _SQLiteConnectionPool:
    """Bounded pool of already-open connections for one SQLite file.

    Each store method used to open()/close() a fresh sqlite3.Connection
    per call; under load that connect/close pair dominated per-call
    latency (measured ~4-5ms p50 vs ~0.03ms for the equivalent in-memory
    path). Connections are borrowed for the duration of one operation and
    returned afterwards instead of being closed, so repeated operations
    reuse already-open connections. Every borrower already rolls back on
    exception before releasing (see the BEGIN IMMEDIATE / except
    BaseException pairs below), so a released connection is always free
    of an open transaction.
    """

    def __init__(self, factory: Callable[[], sqlite3.Connection], size: int = 5) -> None:
        self._factory = factory
        self._size = size
        self._idle: queue.SimpleQueue[sqlite3.Connection] = queue.SimpleQueue()
        self._created = 0
        self._creation_lock = threading.Lock()

    def acquire(self) -> sqlite3.Connection:
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._creation_lock:
            if self._created < self._size:
                self._created += 1
                return self._factory()
        return self._idle.get()

    def release(self, connection: sqlite3.Connection) -> None:
        self._idle.put(connection)


class SQLiteEventStore:
    """Durable, append-only event store backed by one SQLite file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._pool = _SQLiteConnectionPool(self._open_connection)

    @classmethod
    async def open(cls, path: str | Path) -> Self:
        resolved = Path(path)
        if resolved == Path(":memory:"):
            raise ValueError("SQLiteEventStore requires a file path for durable storage")
        store = cls(resolved)
        await asyncio.to_thread(store._initialize)
        return store

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _connect(self) -> sqlite3.Connection:
        return self._pool.acquire()

    def _release(self, connection: sqlite3.Connection) -> None:
        self._pool.release(connection)

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        init_connection = self._open_connection()
        try:
            init_connection.execute("PRAGMA journal_mode = WAL")
            init_connection.execute("PRAGMA synchronous = FULL")
            init_connection.executescript(_SCHEMA)
            init_connection.executescript(_ATTEMPT_SCHEMA)
            lease_columns = {
                str(row["name"])
                for row in init_connection.execute(
                    "PRAGMA table_info(effect_leases)"
                ).fetchall()
            }
            if lease_columns and "lease_id" not in lease_columns:
                init_connection.execute(
                    "ALTER TABLE effect_leases ADD COLUMN lease_id TEXT"
                )
                rows = init_connection.execute(
                    """
                    SELECT subscription_name, request_global_position
                    FROM effect_leases WHERE lease_id IS NULL
                    """
                ).fetchall()
                for row in rows:
                    init_connection.execute(
                        """
                        UPDATE effect_leases SET lease_id = ?
                        WHERE subscription_name = ?
                          AND request_global_position = ?
                        """,
                        (
                            str(uuid4()),
                            str(row["subscription_name"]),
                            int(row["request_global_position"]),
                        ),
                    )
            init_connection.executescript(_LEASE_SCHEMA)
            init_connection.executescript(_LEASE_OBSERVATION_SCHEMA)
            init_connection.commit()
        finally:
            init_connection.close()

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
            self._release(connection)

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
            self._release(connection)

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
            self._release(connection)

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
            self._release(connection)
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
            self._release(connection)
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
            self._release(connection)
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
            self._release(connection)

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
    ) -> EffectClaimResult:
        if (
            not subscription_name
            or not operation_id
            or not stream_id
            or not request_event_type
            or not worker_id
        ):
            raise ValueError("lease identity fields must not be empty")
        if expected_checkpoint < 0 or request_global_position <= 0:
            raise ValueError("lease checkpoint positions are invalid")
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be > 0")
        return await asyncio.to_thread(
            self._try_claim_effect_sync,
            subscription_name,
            expected_checkpoint,
            request_global_position,
            operation_id,
            stream_id,
            request_event_type,
            worker_id,
            lease_ttl_seconds,
            terminal_event_types,
        )

    def _try_claim_effect_sync(
        self,
        subscription_name: str,
        expected_checkpoint: int,
        request_global_position: int,
        operation_id: str,
        stream_id: str,
        request_event_type: str,
        worker_id: str,
        lease_ttl_seconds: float,
        terminal_event_types: frozenset[str],
    ) -> EffectClaimResult:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint_row = connection.execute(
                """
                SELECT global_position FROM subscription_checkpoints
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
            request_row = connection.execute(
                """
                SELECT stream_id, event_type FROM events
                WHERE global_position = ?
                """,
                (request_global_position,),
            ).fetchone()
            if request_row is None or (
                str(request_row["stream_id"]) != stream_id
                or str(request_row["event_type"]) != request_event_type
            ):
                raise ValueError("effect claim request identity is invalid")
            if _stream_is_terminal(
                connection,
                stream_id=stream_id,
                terminal_event_types=terminal_event_types,
            ):
                connection.rollback()
                return EffectClaimResult("terminal")
            if _operation_has_committed_result(
                connection,
                stream_id=stream_id,
                operation_id=operation_id,
            ):
                connection.rollback()
                return EffectClaimResult("already_completed")
            now_unix = _database_now_unix(connection)
            row = connection.execute(
                """
                SELECT * FROM effect_leases
                WHERE subscription_name = ?
                  AND request_global_position = ?
                """,
                (subscription_name, request_global_position),
            ).fetchone()
            if row is not None:
                if (
                    str(row["operation_id"]) != operation_id
                    or str(row["stream_id"]) != stream_id
                    or str(row["request_event_type"]) != request_event_type
                ):
                    raise ValueError("effect lease request identity changed")
                expiry = row["lease_expires_at_unix"]
                if (
                    str(row["status"]) == "claimed"
                    and expiry is not None
                    and float(expiry) > now_unix
                ):
                    _insert_lease_observation(
                        connection,
                        observed_at_unix=now_unix,
                        observation_kind="busy",
                        subscription_name=subscription_name,
                        request_global_position=request_global_position,
                        operation_id=operation_id,
                        stream_id=stream_id,
                        request_event_type=request_event_type,
                        worker_id=worker_id,
                        lease_id=UUID(str(row["lease_id"])),
                        fencing_token=int(row["fencing_token"]),
                    )
                    connection.commit()
                    return EffectClaimResult("busy")
                if str(row["status"]) == "completed":
                    connection.rollback()
                    return EffectClaimResult("already_completed")
                if str(row["status"]) == "claimed":
                    fencing_token = _next_takeover_fencing_token(
                        int(row["fencing_token"])
                    )
                else:
                    fencing_token = _next_released_fencing_token(
                        int(row["fencing_token"])
                    )
            else:
                fencing_token = 1

            attempt_row = connection.execute(
                """
                SELECT attempt_number, stream_id, request_event_type,
                       request_global_position
                FROM effect_attempts
                WHERE operation_id = ?
                ORDER BY attempt_number DESC
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if attempt_row is not None and (
                str(attempt_row["stream_id"]) != stream_id
                or str(attempt_row["request_event_type"]) != request_event_type
                or int(attempt_row["request_global_position"])
                != request_global_position
            ):
                raise ValueError(
                    "effect attempt request identity changed for operation "
                    f"{operation_id!r}"
                )
            attempt = _new_attempt(
                operation_id=operation_id,
                attempt_number=(
                    1
                    if attempt_row is None
                    else int(attempt_row["attempt_number"]) + 1
                ),
                stream_id=stream_id,
                request_event_type=request_event_type,
                request_global_position=request_global_position,
                subscription_name=subscription_name,
            )
            connection.execute(
                """
                INSERT INTO effect_attempts (
                    attempt_id, operation_id, attempt_number, stream_id,
                    request_event_type, request_global_position,
                    subscription_name, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            expires_unix = now_unix + lease_ttl_seconds
            lease_id = uuid4()
            now_iso = datetime.fromtimestamp(
                now_unix, tz=timezone.utc
            ).isoformat()
            if row is not None and str(row["status"]) == "claimed":
                _insert_lease_observation(
                    connection,
                    observed_at_unix=now_unix,
                    observation_kind="expiry",
                    subscription_name=subscription_name,
                    request_global_position=request_global_position,
                    operation_id=operation_id,
                    stream_id=stream_id,
                    request_event_type=request_event_type,
                    worker_id=str(row["worker_id"]),
                    lease_id=UUID(str(row["lease_id"])),
                    fencing_token=int(row["fencing_token"]),
                )
            connection.execute(
                """
                INSERT INTO effect_leases (
                    subscription_name, request_global_position, operation_id,
                    stream_id, request_event_type, lease_id, worker_id, fencing_token,
                    status, lease_expires_at_unix, claimed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?)
                ON CONFLICT(subscription_name, request_global_position)
                DO UPDATE SET
                    lease_id = excluded.lease_id,
                    worker_id = excluded.worker_id,
                    fencing_token = excluded.fencing_token,
                    status = 'claimed',
                    lease_expires_at_unix = excluded.lease_expires_at_unix,
                    claimed_at = excluded.claimed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    subscription_name,
                    request_global_position,
                    operation_id,
                    stream_id,
                    request_event_type,
                    str(lease_id),
                    worker_id,
                    fencing_token,
                    expires_unix,
                    now_iso,
                    now_iso,
                ),
            )
            _insert_lease_observation(
                connection,
                observed_at_unix=now_unix,
                observation_kind=(
                    "claim_acquired" if row is None else "takeover"
                ),
                subscription_name=subscription_name,
                request_global_position=request_global_position,
                operation_id=operation_id,
                stream_id=stream_id,
                request_event_type=request_event_type,
                worker_id=worker_id,
                lease_id=lease_id,
                fencing_token=fencing_token,
                attempt=attempt,
            )
            connection.commit()
            lease = EffectLease(
                lease_id=lease_id,
                subscription_name=subscription_name,
                request_global_position=request_global_position,
                operation_id=operation_id,
                stream_id=stream_id,
                request_event_type=request_event_type,
                worker_id=worker_id,
                fencing_token=fencing_token,
                lease_expires_at=datetime.fromtimestamp(
                    expires_unix, tz=timezone.utc
                ),
                attempt=attempt,
            )
            return EffectClaimResult("acquired", lease)
        except BaseException:
            connection.rollback()
            raise
        finally:
            self._release(connection)

    async def confirm_effect_claim(
        self,
        lease: EffectLease,
        *,
        terminal_event_types: frozenset[str],
    ) -> EffectClaimConfirmation:
        return await asyncio.to_thread(
            self._confirm_effect_claim_sync,
            lease,
            terminal_event_types,
        )

    def _confirm_effect_claim_sync(
        self,
        lease: EffectLease,
        terminal_event_types: frozenset[str],
    ) -> EffectClaimConfirmation:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now_unix = _database_now_unix(connection)
            row = connection.execute(
                """
                SELECT 1 FROM effect_leases
                WHERE subscription_name = ?
                  AND request_global_position = ?
                  AND lease_id = ?
                  AND worker_id = ?
                  AND fencing_token = ?
                  AND status = 'claimed'
                  AND lease_expires_at_unix > ?
                """,
                (
                    lease.subscription_name,
                    lease.request_global_position,
                    str(lease.lease_id),
                    lease.worker_id,
                    lease.fencing_token,
                    now_unix,
                ),
            ).fetchone()
            if row is None:
                _insert_lease_observation(
                    connection,
                    observed_at_unix=now_unix,
                    observation_kind="stale_ownership",
                    subscription_name=lease.subscription_name,
                    request_global_position=lease.request_global_position,
                    operation_id=lease.operation_id,
                    stream_id=lease.stream_id,
                    request_event_type=lease.request_event_type,
                    worker_id=lease.worker_id,
                    lease_id=lease.lease_id,
                    fencing_token=lease.fencing_token,
                )
                connection.commit()
                raise LeaseLostError("effect lease is stale or expired")
            if _stream_is_terminal(
                connection,
                stream_id=lease.stream_id,
                terminal_event_types=terminal_event_types,
            ):
                connection.rollback()
                return EffectClaimConfirmation("terminal")
            if _operation_has_committed_result(
                connection,
                stream_id=lease.stream_id,
                operation_id=lease.operation_id,
            ):
                connection.rollback()
                return EffectClaimConfirmation("already_completed")
            connection.commit()
            return EffectClaimConfirmation("confirmed")
        except BaseException:
            connection.rollback()
            raise
        finally:
            self._release(connection)

    async def renew_effect_claim(
        self,
        lease: EffectLease,
        *,
        lease_ttl_seconds: float,
    ) -> EffectLease:
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be > 0")
        return await asyncio.to_thread(
            self._renew_effect_claim_sync, lease, lease_ttl_seconds
        )

    def _renew_effect_claim_sync(
        self, lease: EffectLease, lease_ttl_seconds: float
    ) -> EffectLease:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now_unix = _database_now_unix(connection)
            expires_unix = now_unix + lease_ttl_seconds
            cursor = connection.execute(
                """
                UPDATE effect_leases
                SET lease_expires_at_unix = ?, updated_at = ?
                WHERE subscription_name = ?
                  AND request_global_position = ?
                  AND lease_id = ?
                  AND worker_id = ?
                  AND fencing_token = ?
                  AND status = 'claimed'
                  AND lease_expires_at_unix > ?
                """,
                (
                    expires_unix,
                    datetime.fromtimestamp(
                        now_unix, tz=timezone.utc
                    ).isoformat(),
                    lease.subscription_name,
                    lease.request_global_position,
                    str(lease.lease_id),
                    lease.worker_id,
                    lease.fencing_token,
                    now_unix,
                ),
            )
            if cursor.rowcount != 1:
                _insert_lease_observation(
                    connection,
                    observed_at_unix=now_unix,
                    observation_kind="stale_ownership",
                    subscription_name=lease.subscription_name,
                    request_global_position=lease.request_global_position,
                    operation_id=lease.operation_id,
                    stream_id=lease.stream_id,
                    request_event_type=lease.request_event_type,
                    worker_id=lease.worker_id,
                    lease_id=lease.lease_id,
                    fencing_token=lease.fencing_token,
                )
                connection.commit()
                raise LeaseLostError("effect lease is stale or expired")
            _insert_lease_observation(
                connection,
                observed_at_unix=now_unix,
                observation_kind="renewal",
                subscription_name=lease.subscription_name,
                request_global_position=lease.request_global_position,
                operation_id=lease.operation_id,
                stream_id=lease.stream_id,
                request_event_type=lease.request_event_type,
                worker_id=lease.worker_id,
                lease_id=lease.lease_id,
                fencing_token=lease.fencing_token,
            )
            connection.commit()
            return EffectLease(
                lease_id=lease.lease_id,
                subscription_name=lease.subscription_name,
                request_global_position=lease.request_global_position,
                operation_id=lease.operation_id,
                stream_id=lease.stream_id,
                request_event_type=lease.request_event_type,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                lease_expires_at=datetime.fromtimestamp(
                    expires_unix, tz=timezone.utc
                ),
                attempt=lease.attempt,
            )
        except BaseException:
            connection.rollback()
            raise
        finally:
            self._release(connection)

    async def release_effect_claim(self, lease: EffectLease) -> bool:
        return await asyncio.to_thread(self._release_effect_claim_sync, lease)

    def _release_effect_claim_sync(self, lease: EffectLease) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now_unix = _database_now_unix(connection)
            cursor = connection.execute(
                """
                UPDATE effect_leases
                SET worker_id = NULL, status = 'released',
                    lease_expires_at_unix = NULL, updated_at = ?
                WHERE subscription_name = ?
                  AND request_global_position = ?
                  AND lease_id = ?
                  AND worker_id = ?
                  AND fencing_token = ?
                  AND status = 'claimed'
                  AND lease_expires_at_unix > ?
                """,
                (
                    datetime.fromtimestamp(
                        now_unix, tz=timezone.utc
                    ).isoformat(),
                    lease.subscription_name,
                    lease.request_global_position,
                    str(lease.lease_id),
                    lease.worker_id,
                    lease.fencing_token,
                    now_unix,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            connection.rollback()
            raise
        finally:
            self._release(connection)

    async def commit_fenced_subscription_batch(
        self,
        lease: EffectLease,
        *,
        expected_checkpoint: int,
        stream_id: str,
        expected_stream_version: int,
        events: Sequence[Event],
        new_checkpoint: int,
        terminal_event_types: frozenset[str],
    ) -> tuple[EventEnvelope, ...]:
        encoded = tuple(
            (event, _encode(event.data), _encode(event.metadata))
            for event in events
        )
        return await asyncio.to_thread(
            self._commit_fenced_subscription_batch_sync,
            lease,
            expected_checkpoint,
            stream_id,
            expected_stream_version,
            encoded,
            new_checkpoint,
            terminal_event_types,
        )

    def _commit_fenced_subscription_batch_sync(
        self,
        lease: EffectLease,
        expected_checkpoint: int,
        stream_id: str,
        expected_stream_version: int,
        encoded: tuple[tuple[Event, str, str], ...],
        new_checkpoint: int,
        terminal_event_types: frozenset[str],
    ) -> tuple[EventEnvelope, ...]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now_unix = _database_now_unix(connection)
            lease_row = connection.execute(
                """
                SELECT lease_id, worker_id, fencing_token, status,
                       lease_expires_at_unix
                FROM effect_leases
                WHERE subscription_name = ?
                  AND request_global_position = ?
                """,
                (
                    lease.subscription_name,
                    lease.request_global_position,
                ),
            ).fetchone()
            if (
                lease_row is None
                or str(lease_row["status"]) != "claimed"
                or str(lease_row["lease_id"]) != str(lease.lease_id)
                or str(lease_row["worker_id"]) != lease.worker_id
                or int(lease_row["fencing_token"]) != lease.fencing_token
                or float(lease_row["lease_expires_at_unix"]) <= now_unix
            ):
                _insert_lease_observation(
                    connection,
                    observed_at_unix=now_unix,
                    observation_kind="stale_commit_rejection",
                    subscription_name=lease.subscription_name,
                    request_global_position=lease.request_global_position,
                    operation_id=lease.operation_id,
                    stream_id=lease.stream_id,
                    request_event_type=lease.request_event_type,
                    worker_id=lease.worker_id,
                    lease_id=lease.lease_id,
                    fencing_token=lease.fencing_token,
                )
                connection.commit()
                raise LeaseLostError("effect lease is stale or expired")

            if _stream_is_terminal(
                connection,
                stream_id=stream_id,
                terminal_event_types=terminal_event_types,
            ) or _operation_has_committed_result(
                connection,
                stream_id=stream_id,
                operation_id=lease.operation_id,
            ):
                encoded = ()

            checkpoint_row = connection.execute(
                """
                SELECT global_position FROM subscription_checkpoints
                WHERE subscription_name = ?
                """,
                (lease.subscription_name,),
            ).fetchone()
            current_checkpoint = (
                0
                if checkpoint_row is None
                else int(checkpoint_row["global_position"])
            )
            if current_checkpoint != expected_checkpoint:
                raise CheckpointConflictError(
                    f"subscription {lease.subscription_name!r} is at position "
                    f"{current_checkpoint}, expected {expected_checkpoint}"
                )
            version_row = connection.execute(
                """
                SELECT COALESCE(MAX(stream_version), -1) AS current_version
                FROM events WHERE stream_id = ?
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
            for offset, (event, event_data, metadata) in enumerate(
                encoded, start=1
            ):
                stream_version = current_version + offset
                cursor = connection.execute(
                    """
                    INSERT INTO events (
                        stream_id, stream_version, event_id, event_type,
                        event_data, metadata, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
                    subscription_name, global_position, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(subscription_name) DO UPDATE SET
                    global_position = excluded.global_position,
                    updated_at = excluded.updated_at
                """,
                (
                    lease.subscription_name,
                    new_checkpoint,
                    created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE effect_leases
                SET worker_id = NULL, status = 'completed',
                    lease_expires_at_unix = NULL, updated_at = ?
                WHERE subscription_name = ?
                  AND request_global_position = ?
                  AND lease_id = ?
                  AND worker_id = ?
                  AND fencing_token = ?
                  AND status = 'claimed'
                """,
                (
                    created_at.isoformat(),
                    lease.subscription_name,
                    lease.request_global_position,
                    str(lease.lease_id),
                    lease.worker_id,
                    lease.fencing_token,
                ),
            )
            connection.commit()
            return tuple(appended)
        except (
            CheckpointConflictError,
            DuplicateEventError,
            LeaseLostError,
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
            self._release(connection)

    async def _load_lease_observations_for_operation(
        self, operation_id: str
    ) -> tuple[EffectLeaseObservation, ...]:
        if not operation_id:
            raise ValueError("operation_id must not be empty")
        return await asyncio.to_thread(
            self._load_lease_observations_sync,
            "operation_id = ?",
            (operation_id,),
        )

    async def _load_lease_observations_for_stream(
        self, stream_id: str
    ) -> tuple[EffectLeaseObservation, ...]:
        if not stream_id:
            raise ValueError("stream_id must not be empty")
        return await asyncio.to_thread(
            self._load_lease_observations_sync,
            "stream_id = ?",
            (stream_id,),
        )

    async def _load_lease_observations_for_request(
        self, *, subscription_name: str, request_global_position: int
    ) -> tuple[EffectLeaseObservation, ...]:
        return await asyncio.to_thread(
            self._load_lease_observations_sync,
            "subscription_name = ? AND request_global_position = ?",
            (subscription_name, request_global_position),
        )

    def _load_lease_observations_sync(
        self, where_clause: str, parameters: tuple[object, ...]
    ) -> tuple[EffectLeaseObservation, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM effect_lease_observations
                WHERE {where_clause}
                ORDER BY observation_sequence
                """,
                parameters,
            ).fetchall()
        finally:
            self._release(connection)
        return tuple(_row_to_lease_observation(row) for row in rows)


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


def _row_to_lease_observation(
    row: sqlite3.Row,
) -> EffectLeaseObservation:
    return EffectLeaseObservation(
        observation_sequence=int(row["observation_sequence"]),
        observed_at=datetime.fromtimestamp(
            float(row["observed_at_unix"]), tz=timezone.utc
        ),
        observation_kind=str(row["observation_kind"]),  # type: ignore[arg-type]
        subscription_name=str(row["subscription_name"]),
        request_global_position=int(row["request_global_position"]),
        operation_id=str(row["operation_id"]),
        stream_id=str(row["stream_id"]),
        request_event_type=str(row["request_event_type"]),
        worker_id=str(row["worker_id"]),
        lease_id=UUID(str(row["lease_id"])),
        fencing_token=int(row["fencing_token"]),
        attempt_id=(
            None
            if row["attempt_id"] is None
            else UUID(str(row["attempt_id"]))
        ),
        attempt_number=(
            None
            if row["attempt_number"] is None
            else int(row["attempt_number"])
        ),
    )


class SQLiteEffectAttemptStore:
    """Durable append-only operational attempt ledger.

    The table may share a SQLite file with ``SQLiteEventStore`` but uses a
    separate transaction and is not part of the domain event stream.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._pool = _SQLiteConnectionPool(self._open_connection)

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

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _connect(self) -> sqlite3.Connection:
        return self._pool.acquire()

    def _release(self, connection: sqlite3.Connection) -> None:
        self._pool.release(connection)

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        init_connection = self._open_connection()
        try:
            init_connection.execute("PRAGMA journal_mode = WAL")
            init_connection.execute("PRAGMA synchronous = FULL")
            init_connection.executescript(_ATTEMPT_SCHEMA)
            init_connection.commit()
        finally:
            init_connection.close()

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
            self._release(connection)

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
            self._release(connection)
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
            self._release(connection)
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
        self._pool = _SQLiteConnectionPool(self._open_connection)

    @classmethod
    async def open(cls, path: str | Path) -> Self:
        event_store = await SQLiteEventStore.open(path)
        return cls(event_store._path)

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _connect(self) -> sqlite3.Connection:
        return self._pool.acquire()

    def _release(self, connection: sqlite3.Connection) -> None:
        self._pool.release(connection)

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
            self._release(connection)
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
            self._release(connection)
