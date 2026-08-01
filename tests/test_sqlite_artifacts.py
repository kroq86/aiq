from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from agentlog import (
    ArtifactRef,
    ArtifactDigestMismatchError,
    ArtifactVersionConflictError,
    SQLiteArtifactStore,
)


EXTERNAL_DIGEST = f"sha256:{'1' * 64}"


def run(coro):
    return asyncio.run(coro)


class SQLiteArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "artifacts.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_versions_survive_reopen_and_old_version_remains_readable(self):
        store = run(SQLiteArtifactStore.open(self.path))
        first = run(
            store.put(
                "policy",
                b"version one",
                media_type="text/plain",
                version="1",
                created_causation="event-1",
            )
        )
        second = run(
            store.put("policy", b"version two", media_type="text/plain", version="2")
        )
        reopened = run(SQLiteArtifactStore.open(self.path))
        self.assertEqual(run(reopened.get(first)), b"version one")
        self.assertEqual(run(reopened.get(second)), b"version two")
        self.assertEqual(
            run(reopened.get_many((second, first))), (b"version two", b"version one")
        )

    def test_duplicate_version_conflict_rolls_back_without_changing_original(self):
        store = run(SQLiteArtifactStore.open(self.path))
        original = run(
            store.put("policy", b"original", media_type="text/plain", version="stable")
        )
        with self.assertRaises(ArtifactVersionConflictError):
            run(
                store.put(
                    "policy", b"changed", media_type="text/plain", version="stable"
                )
            )
        self.assertEqual(run(store.get(original)), b"original")

    def test_digest_mismatch_is_rejected(self):
        store = run(SQLiteArtifactStore.open(self.path))
        ref = run(store.put("policy", b"content", media_type="text/plain"))
        wrong = replace(ref, digest=f"sha256:{'0' * 64}")
        with self.assertRaises(ArtifactDigestMismatchError):
            run(store.get(wrong))

    def test_database_rejects_update_and_delete(self):
        store = run(SQLiteArtifactStore.open(self.path))
        ref = run(store.put("policy", b"content", media_type="text/plain"))
        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE artifact_versions SET content = ? WHERE name = ?",
                    (b"changed", ref.name),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM artifact_versions WHERE name = ?", (ref.name,)
                )

    def external_ref(self, **changes):
        values = {
            "name": "report",
            "version": "object-version-1",
            "media_type": "application/json",
            "digest": EXTERNAL_DIGEST,
            "size": 42,
            "created_causation": "tool-result-1",
            "storage_reference": "s3://reports/run/report.json?versionId=v1",
        }
        values.update(changes)
        return ArtifactRef(**values)

    def test_external_registration_is_idempotent_and_survives_reopen(self):
        store = run(SQLiteArtifactStore.open(self.path))
        ref = self.external_ref()
        self.assertEqual(run(store.register_external(ref)), ref)
        self.assertEqual(run(store.register_external(ref)), ref)

        reopened = run(SQLiteArtifactStore.open(self.path))
        self.assertEqual(run(reopened.get(ref)), ref)
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT storage_kind, content FROM artifact_versions"
            ).fetchone()
        self.assertEqual(row, ("external", None))

    def test_external_registration_conflicts_do_not_change_original(self):
        store = run(SQLiteArtifactStore.open(self.path))
        ref = self.external_ref()
        run(store.register_external(ref))
        conflicts = (
            replace(ref, digest=f"sha256:{'2' * 64}"),
            replace(ref, size=43),
            replace(ref, storage_reference="s3://reports/run/report.json?versionId=v2"),
        )
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                with self.assertRaises(ArtifactVersionConflictError):
                    run(store.register_external(conflict))
        self.assertEqual(run(store.get(ref)), ref)

    def test_external_registration_rejects_incomplete_or_embedded_refs(self):
        store = run(SQLiteArtifactStore.open(self.path))
        for invalid in (
            self.external_ref(size=None),
            self.external_ref(storage_reference=None),
            self.external_ref(storage_reference="sqlite://artifact_versions/report/1"),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    run(store.register_external(invalid))

        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO artifact_versions (
                        name, version, media_type, digest, size,
                        storage_reference, storage_kind, content, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'external', ?, ?)
                    """,
                    (
                        "invalid",
                        "1",
                        "text/plain",
                        EXTERNAL_DIGEST,
                        4,
                        "s3://bucket/object?versionId=1",
                        b"blob",
                        "2026-08-01T00:00:00+00:00",
                    ),
                )

    def test_embedded_and_external_same_version_conflict_in_both_directions(self):
        store = run(SQLiteArtifactStore.open(self.path))
        embedded = run(
            store.put("shared", b"blob", media_type="text/plain", version="1")
        )
        external = self.external_ref(name="shared", version="1")
        with self.assertRaises(ArtifactVersionConflictError):
            run(store.register_external(external))
        self.assertEqual(run(store.get(embedded)), b"blob")

        second_store = run(
            SQLiteArtifactStore.open(Path(self.temp_dir.name) / "second.db")
        )
        run(second_store.register_external(external))
        with self.assertRaises(ArtifactVersionConflictError):
            run(
                second_store.put(
                    "shared", b"blob", media_type="text/plain", version="1"
                )
            )
        self.assertEqual(run(second_store.get(external)), external)

    def test_pre_external_schema_is_migrated_as_embedded(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                """
                CREATE TABLE artifact_versions (
                    name TEXT NOT NULL, version TEXT NOT NULL, media_type TEXT NOT NULL,
                    digest TEXT NOT NULL, size INTEGER NOT NULL,
                    created_causation TEXT, storage_reference TEXT NOT NULL,
                    content BLOB NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (name, version)
                );
                """
            )
            content = b"legacy"
            connection.execute(
                "INSERT INTO artifact_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy",
                    "1",
                    "text/plain",
                    "sha256:c49fea7425fa7f8699897a97c159c6690267d9003bb78c53fafa8fc15c325d84",
                    len(content),
                    None,
                    "sqlite://artifact_versions/legacy/1",
                    content,
                    "2026-08-01T00:00:00+00:00",
                ),
            )
            connection.commit()
        store = run(SQLiteArtifactStore.open(self.path))
        ref = ArtifactRef(
            "legacy",
            "1",
            "text/plain",
            "sha256:c49fea7425fa7f8699897a97c159c6690267d9003bb78c53fafa8fc15c325d84",
            len(b"legacy"),
            None,
            "sqlite://artifact_versions/legacy/1",
        )
        self.assertEqual(run(store.get(ref)), b"legacy")

    def test_crash_after_external_put_before_registration_retries_same_identity(self):
        physical_puts: list[ArtifactRef] = []

        def put_exact_version(operation_id: str) -> ArtifactRef:
            ref = self.external_ref(
                version=operation_id,
                storage_reference=(
                    f"s3://reports/{operation_id}/report.json?versionId=exact-v1"
                ),
            )
            physical_puts.append(ref)
            return ref

        first = put_exact_version("operation-1")
        # Injected process crash: no SQLite registration happened.
        reopened = run(SQLiteArtifactStore.open(self.path))
        second = put_exact_version("operation-1")
        self.assertEqual(first, second)
        self.assertEqual(run(reopened.register_external(second)), second)
        self.assertEqual(run(reopened.register_external(second)), second)
        self.assertEqual(len(physical_puts), 2)
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM artifact_versions"
            ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
