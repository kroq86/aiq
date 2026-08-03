from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from aiq import DuplicateEventError, Event, SQLiteEventStore, VersionConflictError


ROOT = Path(__file__).resolve().parent
PAIR = re.compile(r"^\(([^,]+),([^\)]+)\)$")
STREAM_ID = "store-refinement"


def run_command(*args: str) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=60
    ).stdout.strip()


def state_id(pending: int) -> str:
    # Seven true ghost monitors occupy the low seven bits.
    return f"s{((pending << 7) | 0x7F):08x}"


def count_class(value: int) -> int:
    return 0 if value == 0 else 1 if value == 1 else 2


@dataclass(frozen=True)
class PersistedSnapshot:
    rows: tuple[tuple[int, int, str, str], ...]

    def abstract(self) -> str:
        versions = [row[1] for row in self.rows]
        positions = [row[0] for row in self.rows]
        event_ids = [row[2] for row in self.rows]
        if versions != list(range(len(versions))):
            raise AssertionError(f"non-contiguous stream versions: {versions}")
        if positions != sorted(positions) or len(positions) != len(set(positions)):
            raise AssertionError(f"non-monotonic global positions: {positions}")
        if len(event_ids) != len(set(event_ids)):
            raise AssertionError("duplicate persisted event ids")
        return state_id(count_class(len(self.rows)))


def persisted_snapshot(path: Path) -> PersistedSnapshot:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """
            SELECT global_position, stream_version, event_id, event_type
            FROM events WHERE stream_id = ? ORDER BY stream_version
            """,
            (STREAM_ID,),
        ).fetchall()
    return PersistedSnapshot(tuple((int(p), int(v), str(i), str(t)) for p, v, i, t in rows))


def build_graph(work: Path) -> tuple[set[str], set[tuple[str, str]]]:
    fasm = os.environ.get("FASM_BIN") or shutil.which("fasm")
    setdb = os.environ.get("SETDB_BIN") or shutil.which("setdb")
    if not fasm or not setdb:
        raise RuntimeError("FASM_BIN and SETDB_BIN/setdb are required")
    binary = work / "store-model"
    subprocess.run(
        [fasm, "store_model_normal.asm", str(binary)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=30,
    )
    facts = work / "store-model.facts"
    facts.write_text(run_command(str(binary)) + "\n")
    database = work / "store-model.db"
    run_command(setdb, "new", str(database))
    run_command(setdb, "load", str(database), str(facts))
    states = set(run_command(setdb, "members", str(database), "SStates").splitlines())
    transitions: set[tuple[str, str]] = set()
    for line in run_command(setdb, "pairs", str(database), "STransition").splitlines():
        match = PAIR.match(line)
        if not match:
            raise AssertionError(f"invalid store transition pair: {line!r}")
        transitions.add((match.group(1), match.group(2)))
    return states, transitions


class Scenario:
    def __init__(self, name: str, states: set[str], transitions: set[tuple[str, str]]) -> None:
        self.name = name
        self.states = states
        self.transitions = transitions
        self.directory = tempfile.TemporaryDirectory(prefix=f"aiq-store-{name}-")
        self.path = Path(self.directory.name) / "events.db"
        self.store = asyncio.run(SQLiteEventStore.open(self.path))
        self.previous = self.observe("create empty stream")
        self.snapshots = 1

    def close(self) -> None:
        self.directory.cleanup()

    def observe(self, action: str) -> str:
        current = persisted_snapshot(self.path).abstract()
        if current not in self.states:
            raise AssertionError(f"{self.name}: {action}: {current} is absent from StoreStates")
        return current

    def boundary(self, action: str) -> None:
        current = self.observe(action)
        self.snapshots += 1
        if (self.previous, current) not in self.transitions:
            raise AssertionError(
                f"{self.name}: {action}: no StoreTransition {self.previous} -> {current}"
            )
        self.previous = current


def event(kind: str, event_id: UUID | None = None) -> Event:
    return Event(kind, {"scenario": kind}, event_id=event_id) if event_id else Event(kind, {"scenario": kind})


def run_scenarios(states: set[str], transitions: set[tuple[str, str]]) -> tuple[int, int]:
    total = 0
    scenarios = 0

    scenario = Scenario("append_and_reopen", states, transitions)
    try:
        asyncio.run(scenario.store.append(STREAM_ID, -1, [event("first")]))
        scenario.boundary("append first batch")
        asyncio.run(scenario.store.append(STREAM_ID, 0, [event("next")]))
        scenario.boundary("append next batch with expected version")
        scenario.store = asyncio.run(SQLiteEventStore.open(scenario.path))
        history = asyncio.run(scenario.store.load(STREAM_ID))
        if len(history) != 2:
            raise AssertionError("reopen did not preserve history")
        scenario.boundary("read history after reopen")
        total += scenario.snapshots
        scenarios += 1
    finally:
        scenario.close()

    scenario = Scenario("stale_version", states, transitions)
    try:
        asyncio.run(scenario.store.append(STREAM_ID, -1, [event("first")]))
        scenario.boundary("append first batch")
        try:
            asyncio.run(scenario.store.append(STREAM_ID, -1, [event("stale")]))
        except VersionConflictError:
            pass
        else:
            raise AssertionError("stale expected version was accepted")
        scenario.boundary("reject stale expected version")
        total += scenario.snapshots
        scenarios += 1
    finally:
        scenario.close()

    scenario = Scenario("atomic_batch", states, transitions)
    try:
        ids = [event("batch-a"), event("batch-b")]
        asyncio.run(scenario.store.append(STREAM_ID, -1, ids))
        snapshot = persisted_snapshot(scenario.path)
        if len(snapshot.rows) != 2:
            raise AssertionError("atomic batch was not fully persisted")
        scenario.boundary("atomic batch append")
        total += scenario.snapshots
        scenarios += 1
    finally:
        scenario.close()

    scenario = Scenario("failed_transaction", states, transitions)
    try:
        duplicate = event("original")
        asyncio.run(scenario.store.append(STREAM_ID, -1, [duplicate]))
        scenario.boundary("append first batch")
        before = persisted_snapshot(scenario.path)
        try:
            asyncio.run(
                scenario.store.append(
                    STREAM_ID,
                    0,
                    [event("would-be-inserted"), event("duplicate", duplicate.event_id)],
                )
            )
        except DuplicateEventError:
            pass
        else:
            raise AssertionError("duplicate event transaction was accepted")
        if persisted_snapshot(scenario.path) != before:
            raise AssertionError("failed transaction changed persisted state")
        scenario.boundary("failed transaction leaves state unchanged")
        total += scenario.snapshots
        scenarios += 1
    finally:
        scenario.close()

    return scenarios, total


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aiq-store-refinement-") as directory:
        states, transitions = build_graph(Path(directory))
        scenarios, snapshots = run_scenarios(states, transitions)
    print(
        f"STORE_RUNTIME_REFINEMENT_PASS scenarios={scenarios} snapshots={snapshots} "
        f"formal_states={len(states)} unmatched_transitions=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
