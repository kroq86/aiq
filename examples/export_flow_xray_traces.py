"""Thin wrapper: generate both canonical domain-event-history v1 trace
artifacts in one call, using the shared logic in `agentlog.demo`.

    PYTHONPATH=src python3 examples/export_flow_xray_traces.py --output-dir /tmp

Writes:

    agentlog-completed-domain-event-history-v1.json  (terminal_status="completed")
    agentlog-active-domain-event-history-v1.json      (terminal_status="active")

For the single-file, subprocess-stable form Flow Xray actually calls, use
the package module directly instead of this script:

    python -m agentlog.demo --status completed --output trace.json
    python -m agentlog.demo --status active    --output trace.json

All agent/effect/trace-generation logic lives in `agentlog.demo` (public
API: `generate_completed_trace`, `generate_active_trace`,
`write_trace_json`) so this script and the installed CLI can never drift
apart -- this file only wires them to a two-file, one-directory output
shape for local inspection.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from agentlog.demo import (
    generate_active_trace,
    generate_completed_trace,
    write_trace_json,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory to write both JSON artifacts into (default: current directory).",
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()

    completed = await generate_completed_trace()
    active = await generate_active_trace()

    completed_path = args.output_dir / "agentlog-completed-domain-event-history-v1.json"
    active_path = args.output_dir / "agentlog-active-domain-event-history-v1.json"
    write_trace_json(completed, completed_path)
    write_trace_json(active, active_path)

    print(f"wrote {completed_path} (terminal_status={completed['terminal_status']!r})")
    print(f"wrote {active_path} (terminal_status={active['terminal_status']!r})")


if __name__ == "__main__":
    asyncio.run(_main())
