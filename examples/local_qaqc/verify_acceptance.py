from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def request_json(url: str, *, payload: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--run-id", default=f"acceptance-{int(time.time())}")
    parser.add_argument(
        "--expect", choices=("completed", "failed"), default="completed"
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    status, _ = request_json(
        f"{args.url}/runs",
        payload={"run_id": args.run_id, "task": "Check orders and save a report"},
    )
    if status != 202:
        raise AssertionError(f"start returned {status}")
    duplicate_status, _ = request_json(
        f"{args.url}/runs",
        payload={"run_id": args.run_id, "task": "duplicate start"},
    )
    if duplicate_status != 409:
        raise AssertionError(
            f"duplicate start returned {duplicate_status}, expected 409"
        )

    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        _, document = request_json(f"{args.url}/runs/{args.run_id}")
        events = document["events"]
        if events and events[-1]["type"] in {"RunCompleted", "RunFailed"}:
            break
        time.sleep(0.25)
    else:
        raise AssertionError(
            f"run did not terminate within {args.timeout_seconds:g} seconds"
        )

    terminal = events[-1]["type"]
    expected_terminal = "RunCompleted" if args.expect == "completed" else "RunFailed"
    if terminal != expected_terminal:
        raise AssertionError(f"terminal={terminal}, expected={expected_terminal}")
    requests = [item for item in events if item["type"] == "ToolCallRequested"]
    outcomes = [
        item
        for item in events
        if item["type"] in {"ToolCallSucceeded", "ToolCallRejected", "ToolCallFailed"}
    ]
    if args.expect == "completed":
        if len(requests) != 4 or len(outcomes) != 4:
            raise AssertionError(
                f"expected four tool boundaries, got {len(requests)}/{len(outcomes)}"
            )
        report = next(
            item["data"]["result"]
            for item in outcomes
            if item["type"] == "ToolCallSucceeded"
            and item["data"].get("name") == "save_report"
        )
        if not report["storage_reference"].startswith("s3://"):
            raise AssertionError("report is not an external artifact")
        if "versionId=" not in report["storage_reference"]:
            raise AssertionError("report does not pin an exact object version")
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "terminal": terminal,
                "tool_requests": len(requests),
                "tool_outcomes": len(outcomes),
                "duplicate_start": duplicate_status,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
