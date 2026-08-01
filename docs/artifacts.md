# Versioned artifacts

`ArtifactRef` pins a model request to one immutable artifact version. Event
history stores the reference and digest, not the blob. `ModelRequest` preserves
the exact references through serialization and durable model-loop continuation.

`ArtifactStore` is an explicit async I/O boundary. `InMemoryArtifactStore` is a
test/reference adapter and is not durable across process restart.
`SQLiteArtifactStore` persists immutable versions in a file-backed SQLite
database using WAL and `synchronous=FULL`. A version is either `embedded`
(content is in SQLite) or `external` (SQLite contains only immutable identity
metadata). It rejects SQL `UPDATE` and `DELETE` through database triggers and
batch-loads exact references with one query.

```python
store = await SQLiteArtifactStore.open("agentlog.db")
ref = await store.put(
    "policy",
    content,
    media_type="application/pdf",
    version="approved-2026-08",
    created_causation=event_id,
)
content = await store.get(ref)
```

An external-storage adapter must PUT and verify the exact external object before
registration. Registration itself performs no network I/O:

```python
ref = await store.register_external(
    ArtifactRef(
        name="qaqc-report.json",
        version=operation_id,
        media_type="application/json",
        digest=verified_digest,
        size=verified_size,
        created_causation=operation_id,
        storage_reference=(
            f"s3://reports/{operation_id}/qaqc-report.json?versionId={version_id}"
        ),
    )
)
assert await store.get(ref) == ref  # metadata only; no external blob fetch
```

Exact re-registration is idempotent. Reusing a name/version with a different
kind, digest, size, media type, causation, or storage reference is a conflict.
The adapter that owns the URI remains responsible for retrieving the blob.

There is deliberately no implicit `latest` lookup. Missing versions, version
conflicts, and digest mismatches are explicit errors. The bounded ArtifactModel
establishes registered identity immutability within its stated bound; it does
not establish that MinIO/S3 contains matching bytes. ACLs, remote-store
implementations, and blob streaming are outside this change set.
