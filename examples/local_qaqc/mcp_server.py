from __future__ import annotations

import hashlib
import io
import json
import os
import time

from mcp.server.fastmcp import FastMCP
from minio import Minio
from minio.error import S3Error

BUCKET = os.getenv("MINIO_BUCKET", "agentlog-lab")
POLICY = os.getenv("QA_POLICY", "allow")
FAULT = os.getenv("MCP_FAULT", "none")

client = Minio(
    os.getenv("MINIO_ENDPOINT", "minio:9000"),
    access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
    secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
    secure=False,
)
mcp = FastMCP(
    "agentlog-local-qaqc",
    host="0.0.0.0",
    port=8001,
    stateless_http=True,
    json_response=True,
)


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _read_exact(
    object_name: str, version_id: str | None = None
) -> tuple[bytes, object]:
    response = client.get_object(BUCKET, object_name, version_id=version_id)
    try:
        content = response.read()
    finally:
        response.close()
        response.release_conn()
    return content, client.stat_object(BUCKET, object_name, version_id=version_id)


@mcp.tool()
def list_rules(dataset: str) -> dict:
    """Return the exact QA rule-set identity authorized for a dataset."""
    if POLICY == "deny":
        raise PermissionError(f"policy denied dataset {dataset}")
    content, stat = _read_exact("rules/qa-rules-v17.json")
    payload = json.loads(content)
    return {
        "rules_version": payload["version"],
        "rules_digest": _sha256(content),
        "rules_object_version": stat.version_id,
    }


@mcp.tool()
def stat_dataset(path: str) -> dict:
    """Pin a dataset to its exact MinIO object version and digest."""
    content, stat = _read_exact(path)
    digest = _sha256(content)
    if FAULT == "change_after_pin":
        changed = b'[{"order_id":99,"amount":999.0}]\n'
        client.put_object(BUCKET, path, io.BytesIO(changed), len(changed))
    if FAULT == "digest_mismatch":
        digest = f"sha256:{'0' * 64}"
    return {
        "path": path,
        "version_id": stat.version_id,
        "etag": stat.etag,
        "digest": digest,
        "size": stat.size,
    }


@mcp.tool()
def run_qaqc(
    path: str,
    version_id: str,
    dataset_digest: str,
    rules_version: str,
) -> dict:
    """Run deterministic QA/QC against an exact dataset version."""
    content, _ = _read_exact(path, version_id)
    if _sha256(content) != dataset_digest:
        raise ValueError("dataset digest mismatch after pinning")
    if rules_version != "qa-rules-v17":
        raise ValueError("unsupported rules version")
    rows = json.loads(content)
    failures = [row["order_id"] for row in rows if row["amount"] <= 0]
    return {"rows": len(rows), "failed_order_ids": failures, "passed": not failures}


@mcp.tool()
def save_report(operation_id: str, result_json: str) -> dict:
    """Write and verify an immutable report version in MinIO."""
    content = result_json.encode()
    digest = _sha256(content)
    object_name = f"reports/{operation_id}/qaqc-report.json"
    try:
        existing = client.stat_object(BUCKET, object_name)
    except S3Error as error:
        if error.code not in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            raise
        existing = None
    if existing is None:
        put = client.put_object(
            BUCKET,
            object_name,
            io.BytesIO(content),
            len(content),
            content_type="application/json",
            metadata={"sha256": digest},
        )
        version_id = put.version_id
    else:
        stored, _ = _read_exact(object_name, existing.version_id)
        if _sha256(stored) != digest:
            raise ValueError("operation_id already owns different report content")
        version_id = existing.version_id
    if FAULT == "timeout_after_put":
        time.sleep(30)
    stat = client.stat_object(BUCKET, object_name, version_id=version_id)
    stored, _ = _read_exact(object_name, version_id)
    if stat.size != len(content) or _sha256(stored) != digest:
        raise ValueError("stored report identity verification failed")
    return {
        "artifact_ref": {
            "name": "qaqc-report.json",
            "version": operation_id,
            "media_type": "application/json",
            "digest": digest,
            "size": len(content),
            "created_causation": operation_id,
            "storage_reference": (
                f"s3://{BUCKET}/{object_name}?versionId={version_id}"
            ),
        }
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
