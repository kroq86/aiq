from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentlog import InMemoryEventStore, ToolRegistry
from agentlog.fastapi import AgentlogApplication
from tests.model.normalization import normalize_history
from tests.model.runtime_harness import RuntimeHarness
from tests.model.test_fastapi_semantic_equivalence import collect_sse
from tests.test_model_loop_policy import Provider, define, get_weather, run as run_async

from .runtime_abstraction import encode_runtime_state

ROOT = Path(__file__).resolve().parents[2]
FORMAL = ROOT / "formal/setdb"
PAIR = re.compile(r"^\(([^,]+),([^\)]+)\)$")


def run(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        args,
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def build_graph(work: Path) -> tuple[dict[str, str], set[str], set[tuple[str, str]]]:
    fasm = os.environ.get("FASM_BIN") or shutil.which("fasm")
    setdb = os.environ.get("SETDB_BIN") or shutil.which("setdb")
    if not fasm or not setdb:
        raise RuntimeError("FASM_BIN and SETDB_BIN/setdb are required")
    binary = work / "model"
    subprocess.run(
        [fasm, "agentlog_model_normal.asm", str(binary)],
        cwd=FORMAL,
        check=True,
        capture_output=True,
        timeout=30,
    )
    emitted = run(str(binary))
    encodings: dict[str, str] = {}
    facts: list[str] = []
    for line in emitted.splitlines():
        if line.startswith("# ENC "):
            _, _, state_id, encoding = line.split()
            if encoding in encodings:
                raise AssertionError("formal state encoding is not unique")
            encodings[encoding] = state_id
        else:
            facts.append(line)
    database = work / "model.db"
    facts_path = work / "model.setdb"
    facts_path.write_text("\n".join(facts) + "\n")
    run(setdb, "new", str(database))
    run(setdb, "load", str(database), str(facts_path))
    initial = run(setdb, "members", str(database), "Initial")
    reachable = {initial, *run(setdb, "expand", str(database), "Transition", "first", initial).splitlines()}
    transitions = set()
    for line in run(setdb, "pairs", str(database), "Transition").splitlines():
        match = PAIR.match(line)
        if not match:
            raise AssertionError(f"invalid setdb transition pair: {line!r}")
        transitions.add((match.group(1), match.group(2)))
    return encodings, reachable, transitions


def verify_scenario(
    name: str,
    actions: tuple[str, ...],
    encodings: dict[str, str],
    reachable: set[str],
    transitions: set[tuple[str, str]],
) -> int:
    runtime = RuntimeHarness.create()
    snapshots = 0

    def abstract() -> str:
        encoding = encode_runtime_state(
            normalize_history(runtime.history()), runtime.checkpoints()
        )
        try:
            state_id = encodings[encoding]
        except KeyError as error:
            raise AssertionError(f"{name}: runtime snapshot is absent from formal States: {encoding}") from error
        if state_id not in reachable:
            raise AssertionError(f"{name}: formal state is not Reachable: {state_id}")
        return state_id

    previous = abstract()
    snapshots += 1
    for action in actions:
        runtime.dispatch(action)
        current = abstract()
        snapshots += 1
        if (previous, current) not in transitions:
            raise AssertionError(
                f"{name}: runtime action {action!r} has no formal transition: "
                f"{previous} -> {current}"
            )
        previous = current
    return snapshots


def verify_fastapi(encodings: dict[str, str], reachable: set[str]) -> int:
    store = InMemoryEventStore()
    tools = ToolRegistry.from_functions(get_weather)
    agent, loop = define(tools)
    application = AgentlogApplication(store=store, poll_interval_seconds=0.01)
    application.register(agent, resources={"model": Provider(), "tools": tools})
    app = FastAPI(lifespan=application.lifespan)
    app.include_router(application.router)
    with TestClient(app) as client:
        run_id = client.post("/agents/assistant/runs").json()["run_id"]
        response = client.post(
            f"/agents/assistant/runs/{run_id}/commands/message",
            json={"text": "weather"},
        )
        if response.status_code != 200:
            raise AssertionError(f"FastAPI command failed: {response.status_code}")
        with client.stream(
            "GET", f"/agents/assistant/runs/{run_id}/stream"
        ) as stream:
            collect_sse(stream, stop_at=loop.events.RunCompleted.__name__)

        history = normalize_history(run_async(store.load(f"assistant:{run_id}")))
        checkpoints = (
            run_async(store.load_checkpoint("assistant:1:reactions")),
            run_async(store.load_checkpoint("assistant:1:effects")),
        )
        encoding = encode_runtime_state(history, checkpoints)
        try:
            state_id = encodings[encoding]
        except KeyError as error:
            raise AssertionError("FastAPI terminal snapshot is absent from formal States") from error
        if state_id not in reachable:
            raise AssertionError(f"FastAPI terminal state is not Reachable: {state_id}")
    return 1


def main() -> int:
    scenarios = {
        "restart_success": tuple(
            action
            for _ in range(14)
            for action in ("reaction", "restart", "effect", "restart")
        ),
        "model_failure": tuple(
            action for _ in range(10) for action in ("reaction", "effect_model_failure")
        ),
        "tool_failure": tuple(
            action for _ in range(12) for action in ("reaction", "effect_tool_failure")
        ),
        "forced_terminal": ("force_terminal",) + tuple(
            action for _ in range(8) for action in ("reaction", "effect")
        ),
    }
    with tempfile.TemporaryDirectory(prefix="agentlog-refinement-") as directory:
        encodings, reachable, transitions = build_graph(Path(directory))
        snapshots = sum(
            verify_scenario(name, actions, encodings, reachable, transitions)
            for name, actions in scenarios.items()
        )
        snapshots += verify_fastapi(encodings, reachable)
    print(
        f"REFINEMENT_PASS scenarios={len(scenarios)} fastapi=1 snapshots={snapshots} "
        f"formal_states={len(encodings)} reachable={len(reachable)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
