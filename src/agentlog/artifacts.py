"""Immutable, versioned artifact references and storage adapters."""

from __future__ import annotations

import asyncio
import hashlib
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, Self


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactVersionConflictError(ValueError):
    pass


class ArtifactDigestMismatchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    name: str
    version: str
    media_type: str
    digest: str
    size: int | None = None
    created_causation: str | None = None
    storage_reference: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.version or not self.media_type:
            raise ValueError("artifact name, version, and media_type must not be empty")
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError("artifact digest must use sha256:<64 lowercase hex>")
        if self.size is not None and self.size < 0:
            raise ValueError("artifact size must be non-negative")

    def to_data(self) -> dict[str, str | int | None]:
        return {
            "name": self.name,
            "version": self.version,
            "media_type": self.media_type,
            "digest": self.digest,
            "size": self.size,
            "created_causation": self.created_causation,
            "storage_reference": self.storage_reference,
        }

    @classmethod
    def from_data(cls, data: object) -> ArtifactRef:
        if not isinstance(data, Mapping):
            raise TypeError("artifact ref must be an object")
        return cls(
            name=str(data["name"]),
            version=str(data["version"]),
            media_type=str(data["media_type"]),
            digest=str(data["digest"]),
            size=int(data["size"]) if data.get("size") is not None else None,
            created_causation=(
                str(data["created_causation"])
                if data.get("created_causation") is not None
                else None
            ),
            storage_reference=(
                str(data["storage_reference"])
                if data.get("storage_reference") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ArtifactVersion:
    ref: ArtifactRef
    created_at: str


class ArtifactStore(Protocol):
    async def put(
        self,
        name: str,
        content: bytes,
        *,
        media_type: str,
        version: str | None = None,
        created_causation: str | None = None,
    ) -> ArtifactRef: ...

    async def get(self, ref: ArtifactRef) -> bytes | ArtifactRef: ...

    async def get_many(
        self, refs: tuple[ArtifactRef, ...]
    ) -> tuple[bytes | ArtifactRef, ...]: ...


def artifact_digest(content: bytes) -> str:
    if not isinstance(content, bytes):
        raise TypeError("artifact content must be bytes")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


class InMemoryArtifactStore:
    """Process-local test/reference adapter; not durable across restart."""

    def __init__(self) -> None:
        self._refs: dict[tuple[str, str], ArtifactRef] = {}
        self._content: dict[tuple[str, str], bytes] = {}

    @property
    def refs(self):
        return MappingProxyType(dict(self._refs))

    async def put(
        self,
        name: str,
        content: bytes,
        *,
        media_type: str,
        version: str | None = None,
        created_causation: str | None = None,
    ) -> ArtifactRef:
        digest = artifact_digest(content)
        artifact_version = version or digest.removeprefix("sha256:")
        candidate = ArtifactRef(
            name,
            artifact_version,
            media_type,
            digest,
            len(content),
            created_causation,
            f"memory://{name}/{artifact_version}",
        )
        key = (name, artifact_version)
        existing = self._refs.get(key)
        if existing is not None:
            if existing != candidate or self._content[key] != content:
                raise ArtifactVersionConflictError(
                    f"artifact version already exists with different content: {key!r}"
                )
            return existing
        self._refs[key] = candidate
        self._content[key] = bytes(content)
        return candidate

    async def get(self, ref: ArtifactRef) -> bytes:
        key = (ref.name, ref.version)
        try:
            stored_ref = self._refs[key]
            content = self._content[key]
        except KeyError as error:
            raise ArtifactNotFoundError(
                f"artifact version not found: {(ref.name, ref.version)!r}"
            ) from error
        if stored_ref != ref or artifact_digest(content) != ref.digest:
            raise ArtifactDigestMismatchError(
                f"artifact reference does not match stored content: {key!r}"
            )
        return bytes(content)

    async def get_many(self, refs: tuple[ArtifactRef, ...]) -> tuple[bytes, ...]:
        # This reference adapter performs no I/O; production adapters should
        # implement one backend batch request rather than N round-trips.
        return tuple([await self.get(ref) for ref in refs])


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifact_versions (
    name TEXT NOT NULL CHECK (name <> ''),
    version TEXT NOT NULL CHECK (version <> ''),
    media_type TEXT NOT NULL CHECK (media_type <> ''),
    digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    created_causation TEXT,
    storage_reference TEXT NOT NULL,
    storage_kind TEXT NOT NULL CHECK (storage_kind IN ('embedded', 'external')),
    content BLOB,
    created_at TEXT NOT NULL,
    CHECK (
        (storage_kind = 'embedded' AND content IS NOT NULL
            AND storage_reference LIKE 'sqlite://%')
        OR
        (storage_kind = 'external' AND content IS NULL
            AND storage_reference NOT LIKE 'sqlite://%')
    ),
    PRIMARY KEY (name, version)
);

CREATE TRIGGER IF NOT EXISTS artifact_versions_are_immutable_update
BEFORE UPDATE ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_versions_are_immutable_delete
BEFORE DELETE ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact versions are immutable');
END;
"""


class SQLiteArtifactStore:
    """Durable immutable artifact versions backed by one SQLite file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    async def open(cls, path: str | Path) -> Self:
        resolved = Path(path)
        if resolved == Path(":memory:"):
            raise ValueError("SQLiteArtifactStore requires a file path")
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
            self._migrate_pre_external_schema(connection)
            connection.executescript(_SQLITE_SCHEMA)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _migrate_pre_external_schema(connection: sqlite3.Connection) -> None:
        columns = connection.execute("PRAGMA table_info(artifact_versions)").fetchall()
        if not columns or any(str(column[1]) == "storage_kind" for column in columns):
            return
        connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            DROP TRIGGER IF EXISTS artifact_versions_are_immutable_update;
            DROP TRIGGER IF EXISTS artifact_versions_are_immutable_delete;
            ALTER TABLE artifact_versions RENAME TO artifact_versions_legacy;
            {_SQLITE_SCHEMA}
            INSERT INTO artifact_versions (
                name, version, media_type, digest, size, created_causation,
                storage_reference, storage_kind, content, created_at
            )
            SELECT name, version, media_type, digest, size, created_causation,
                   storage_reference, 'embedded', content, created_at
            FROM artifact_versions_legacy
            ;
            DROP TABLE artifact_versions_legacy;
            COMMIT;
            """
        )

    async def put(
        self,
        name: str,
        content: bytes,
        *,
        media_type: str,
        version: str | None = None,
        created_causation: str | None = None,
    ) -> ArtifactRef:
        digest = artifact_digest(content)
        artifact_version = version or digest.removeprefix("sha256:")
        storage_reference = f"sqlite://artifact_versions/{name}/{artifact_version}"
        candidate = ArtifactRef(
            name,
            artifact_version,
            media_type,
            digest,
            len(content),
            created_causation,
            storage_reference,
        )
        return await asyncio.to_thread(self._put_sync, candidate, bytes(content))

    def _put_sync(self, candidate: ArtifactRef, content: bytes) -> ArtifactRef:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM artifact_versions WHERE name = ? AND version = ?",
                (candidate.name, candidate.version),
            ).fetchone()
            if row is not None:
                existing = self._row_ref(row)
                if (
                    str(row["storage_kind"]) != "embedded"
                    or existing != candidate
                    or bytes(row["content"]) != content
                ):
                    raise ArtifactVersionConflictError(
                        "artifact version already exists with different content: "
                        f"{(candidate.name, candidate.version)!r}"
                    )
                connection.rollback()
                return existing
            connection.execute(
                """
                INSERT INTO artifact_versions (
                    name, version, media_type, digest, size,
                    created_causation, storage_reference, storage_kind,
                    content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'embedded', ?, ?)
                """,
                (
                    candidate.name,
                    candidate.version,
                    candidate.media_type,
                    candidate.digest,
                    candidate.size,
                    candidate.created_causation,
                    candidate.storage_reference,
                    content,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
            return candidate
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def register_external(self, ref: ArtifactRef) -> ArtifactRef:
        """Register verified external blob identity without fetching its content."""
        if ref.size is None:
            raise ValueError("external artifact size is required")
        if not ref.storage_reference:
            raise ValueError("external artifact storage_reference is required")
        if ref.storage_reference.startswith("sqlite://"):
            raise ValueError(
                "external artifact storage_reference must not use sqlite://"
            )
        return await asyncio.to_thread(self._register_external_sync, ref)

    def _register_external_sync(self, candidate: ArtifactRef) -> ArtifactRef:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM artifact_versions WHERE name = ? AND version = ?",
                (candidate.name, candidate.version),
            ).fetchone()
            if row is not None:
                existing = self._row_ref(row)
                if str(row["storage_kind"]) != "external" or existing != candidate:
                    raise ArtifactVersionConflictError(
                        "artifact version already exists with different identity: "
                        f"{(candidate.name, candidate.version)!r}"
                    )
                connection.rollback()
                return existing
            connection.execute(
                """
                INSERT INTO artifact_versions (
                    name, version, media_type, digest, size,
                    created_causation, storage_reference, storage_kind,
                    content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'external', NULL, ?)
                """,
                (
                    candidate.name,
                    candidate.version,
                    candidate.media_type,
                    candidate.digest,
                    candidate.size,
                    candidate.created_causation,
                    candidate.storage_reference,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
            return candidate
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def get(self, ref: ArtifactRef) -> bytes | ArtifactRef:
        return (await self.get_many((ref,)))[0]

    async def get_many(
        self, refs: tuple[ArtifactRef, ...]
    ) -> tuple[bytes | ArtifactRef, ...]:
        if not refs:
            return ()
        return await asyncio.to_thread(self._get_many_sync, refs)

    def _get_many_sync(
        self, refs: tuple[ArtifactRef, ...]
    ) -> tuple[bytes | ArtifactRef, ...]:
        keys = tuple((ref.name, ref.version) for ref in refs)
        placeholders = ",".join("(?, ?)" for _ in keys)
        parameters = tuple(item for key in keys for item in key)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM artifact_versions WHERE (name, version) IN ({placeholders})",
                parameters,
            ).fetchall()
        finally:
            connection.close()
        by_key = {(str(row["name"]), str(row["version"])): row for row in rows}
        content: list[bytes | ArtifactRef] = []
        for ref in refs:
            key = (ref.name, ref.version)
            row = by_key.get(key)
            if row is None:
                raise ArtifactNotFoundError(f"artifact version not found: {key!r}")
            stored_ref = self._row_ref(row)
            if stored_ref != ref:
                raise ArtifactDigestMismatchError(
                    f"artifact reference does not match stored identity: {key!r}"
                )
            if str(row["storage_kind"]) == "external":
                content.append(stored_ref)
                continue
            blob = bytes(row["content"])
            if artifact_digest(blob) != ref.digest:
                raise ArtifactDigestMismatchError(
                    f"artifact reference does not match stored content: {key!r}"
                )
            content.append(blob)
        return tuple(content)

    @staticmethod
    def _row_ref(row: sqlite3.Row) -> ArtifactRef:
        return ArtifactRef(
            str(row["name"]),
            str(row["version"]),
            str(row["media_type"]),
            str(row["digest"]),
            int(row["size"]),
            str(row["created_causation"])
            if row["created_causation"] is not None
            else None,
            str(row["storage_reference"]),
        )
