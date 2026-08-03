#!/usr/bin/env python3
"""Bounded exhaustive checker for the AIQ 0.2 reference transition system."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from formal.model.spec import (  # noqa: E402
    ReferenceState,
    _append,
    assert_invariants,
    initial_state,
    step,
)

ACTIONS = (
    "reaction",
    "effect",
    "effect_model_failure",
    "effect_tool_failure",
    "force_terminal",
    "restart",
)


@dataclass(frozen=True)
class Edge:
    source: str
    action: str
    target: str


def transition(state: ReferenceState, action: str, mutant: str | None) -> ReferenceState:
    if mutant == "duplicate_terminal" and action == "force_terminal":
        return _append(state, "RunFailed")
    return step(state, action)


def explore(mutant: str | None) -> tuple[dict[ReferenceState, str], list[Edge], list[str], dict[str, tuple[str, str]]]:
    start = initial_state()
    identifiers = {start: "s0"}
    queue = deque([start])
    edges: list[Edge] = []
    violations: list[str] = []
    parents: dict[str, tuple[str, str]] = {}

    while queue:
        current = queue.popleft()
        source = identifiers[current]
        for action in ACTIONS:
            candidate = transition(current, action, mutant)
            invalid = False
            try:
                assert_invariants(current, candidate)
            except AssertionError:
                invalid = True
            target = identifiers.get(candidate)
            if target is None:
                target = f"s{len(identifiers)}"
                identifiers[candidate] = target
                parents[target] = (source, action)
                if not invalid:
                    queue.append(candidate)
            edges.append(Edge(source, action, target))
            if invalid:
                violations.append(target)
    return identifiers, edges, sorted(set(violations)), parents


def run_setdb(binary: str, database: Path, *args: str) -> str:
    completed = subprocess.run(
        [binary, *args[:1], str(database), *args[1:]],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def persist(binary: str, database: Path, states: dict[ReferenceState, str], edges: list[Edge], violations: list[str]) -> None:
    subprocess.run([binary, "new", str(database)], check=True, timeout=10)
    commands = ["transact"]
    for state, state_id in states.items():
        commands.append(f"entity state {state_id}")
        if state.terminal:
            commands.append(f"entity terminal {state_id}")
    for index, edge in enumerate(edges):
        commands.append(f"fact t{index} {edge.source} {edge.action} {edge.target}")
    for state_id in violations:
        commands.append(f"entity violation {state_id}")
    commands.extend(("commit", "quit"))
    subprocess.run(
        [binary, "serve", str(database)],
        input="\n".join(commands) + "\n",
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def counterexample(state_id: str, parents: dict[str, tuple[str, str]]) -> str:
    actions: list[str] = []
    cursor = state_id
    while cursor in parents:
        cursor, action = parents[cursor]
        actions.append(action)
    return " -> ".join(reversed(actions))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--setdb-bin", default=os.environ.get("SETDB_BIN") or shutil.which("setdb"))
    parser.add_argument("--database")
    parser.add_argument("--mutant", choices=("duplicate_terminal",))
    args = parser.parse_args()
    if not args.setdb_bin:
        parser.error("setdb not found; pass --setdb-bin or SETDB_BIN")
    states, edges, violations, parents = explore(args.mutant)
    temporary = tempfile.TemporaryDirectory(prefix="aiq-setdb-") if not args.database else None
    database = Path(args.database) if args.database else Path(temporary.name) / "model.db"
    persist(args.setdb_bin, database, states, edges, violations)
    stored = run_setdb(
        args.setdb_bin, database, "select", "entity_kind", "second", "violation"
    )
    print(
        f"states={len(states)} transitions={len(edges)} violations={len(violations)} "
        f"max_reachable_history={max(len(state.history) for state in states)} "
        f"database={database}"
    )
    if violations:
        print(f"counterexample={counterexample(violations[0], parents)}")
        return 1
    if stored:
        print("setdb violation relation disagrees with explorer", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
