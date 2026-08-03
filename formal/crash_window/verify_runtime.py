from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agentlog import (
    DurableDispatcher,
    DurableEffectDispatcher,
    Event,
    InMemoryEventStore,
    ModelMessage,
    ModelResponse,
    SQLiteEffectAttemptStore,
    ToolRegistry,
    run_stream_id,
)
from tests.test_model_loop_policy import define, get_weather


ROOT = Path(__file__).resolve().parent
PAIR = re.compile(r"^\(([^,]+),([^\)]+)\)$")

DP_NONE = 0
DP_REQUESTED = 1
DP_RESULT = 2
DP_FAILED = 3
OP_IDLE = 0
OP_INVOKED = 1
OID_NONE = 0
OID_ORIGINAL = 1
OID_OTHER = 2
COUNT_ZERO = 0
COUNT_ONE = 1
COUNT_MANY = 2


def run_command(*args: str) -> str:
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def state_id(
    durable: int,
    operational: int,
    operation_id: int,
    physical: int,
    committed: int,
) -> str:
    numeric = durable
    numeric = numeric * 2 + operational
    numeric = numeric * 3 + operation_id
    numeric = numeric * 3 + physical
    numeric = numeric * 3 + committed
    return f"c{numeric:08x}"


def build_graph(work: Path) -> tuple[set[str], set[str], set[tuple[str, str]]]:
    fasm = os.environ.get("FASM_BIN") or shutil.which("fasm")
    setdb = os.environ.get("SETDB_BIN") or shutil.which("setdb")
    if not fasm or not setdb:
        raise RuntimeError("FASM_BIN and SETDB_BIN/setdb are required")
    binary = work / "crash-model"
    subprocess.run(
        [fasm, "crash_model_normal.asm", str(binary)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=30,
    )
    facts = work / "crash-model.setdb"
    facts.write_text(run_command(str(binary)) + "\n")
    database = work / "crash-model.db"
    run_command(setdb, "new", str(database))
    run_command(setdb, "load", str(database), str(facts))
    states = set(run_command(setdb, "members", str(database), "CStates").splitlines())
    invariant = set(run_command(setdb, "members", str(database), "CInv").splitlines())
    transitions: set[tuple[str, str]] = set()
    for line in run_command(setdb, "pairs", str(database), "CTransition").splitlines():
        match = PAIR.match(line)
        if not match:
            raise AssertionError(f"invalid crash transition pair: {line!r}")
        transitions.add((match.group(1), match.group(2)))
    return states, invariant, transitions


@dataclass
class InvocationLedger:
    operation_ids: list[str]
    entered: asyncio.Event
    release: asyncio.Event


class ObservedProvider:
    def __init__(self, ledger: InvocationLedger) -> None:
        self._ledger = ledger

    async def complete(self, request, *, operation_id: str) -> ModelResponse:
        del request
        self._ledger.operation_ids.append(operation_id)
        self._ledger.entered.set()
        await self._ledger.release.wait()
        return ModelResponse(ModelMessage("assistant", "done"))


def count_class(value: int) -> int:
    if value == 0:
        return COUNT_ZERO
    if value == 1:
        return COUNT_ONE
    return COUNT_MANY


def abstract_runtime(history, attempts, operational: int) -> str:
    request = next(
        (item.event for item in history if item.event.event_type == "ModelCallRequested"),
        None,
    )
    results = [
        item.event
        for item in history
        if item.event.event_type
        in {"ModelCallSucceeded", "ModelCallFailed", "ModelCallRejected"}
    ]
    failed = any(item.event.event_type == "RunFailed" for item in history)
    if results:
        durable = DP_RESULT
    elif failed:
        durable = DP_FAILED
    elif request is not None:
        durable = DP_REQUESTED
    else:
        durable = DP_NONE

    if request is None:
        operation_class = OID_NONE
        committed_for_operation = 0
    else:
        original = str(request.event_id)
        request_operation = request.metadata.get("operation_id")
        if request_operation != original:
            raise AssertionError("runtime request does not carry its event_id as operation_id")
        operation_class = (
            OID_ORIGINAL
            if all(
                attempt.operation_id == original for attempt in attempts
            )
            else OID_OTHER
        )
        committed_for_operation = sum(
            result.metadata.get("operation_id") == original for result in results
        )

    return state_id(
        durable,
        operational,
        operation_class,
        count_class(len(attempts)),
        count_class(committed_for_operation),
    )


async def advance_to_model_request(store, stream_id: str, runtime) -> None:
    reactions = DurableDispatcher(
        agent=runtime.agent,
        store=store,
        subscription_name="assistant:1:reactions",
    )
    for _ in range(4):
        await reactions.run_once()
        history = await store.load(stream_id)
        if any(item.event.event_type == "ModelCallRequested" for item in history):
            return
    raise AssertionError("reaction dispatcher did not commit ModelCallRequested")


async def advance_effect_to_request(dispatcher: DurableEffectDispatcher) -> None:
    # RunCreated and UserMessageAdded have no effect handlers. Advancing them
    # leaves ModelCallRequested as the next durable effect input.
    if not await dispatcher.run_once() or not await dispatcher.run_once():
        raise AssertionError("effect dispatcher did not reach ModelCallRequested")


async def verify_runtime_trace(
    states: set[str],
    invariant: set[str],
    transitions: set[tuple[str, str]],
    attempt_path: Path,
) -> tuple[int, int]:
    store = InMemoryEventStore()
    attempt_store = await SQLiteEffectAttemptStore.open(attempt_path)
    stream_id = run_stream_id("assistant", "crash-window-refinement")
    tools = ToolRegistry.from_functions(get_weather)
    first_ledger = InvocationLedger([], asyncio.Event(), asyncio.Event())
    first_agent, _ = define(tools)
    first_runtime = first_agent.build_runtime(
        context={"model": ObservedProvider(first_ledger), "tools": tools}
    )
    await store.append(
        stream_id,
        -1,
        (
            Event("RunCreated", {"agent": "assistant", "definition_version": "1"}),
            first_agent.handle_command("message", {"text": "hello"})[0],
        ),
    )
    await advance_to_model_request(store, stream_id, first_runtime)
    first_effects = DurableEffectDispatcher(
        agent=first_runtime.agent,
        store=store,
        effects=first_runtime.effects,
        context=first_runtime.context,
        subscription_name="assistant:1:effects",
        attempt_store=attempt_store,
    )
    await advance_effect_to_request(first_effects)

    snapshots = [
        abstract_runtime(
            await store.load(stream_id),
            await attempt_store.load_for_stream(stream_id),
            OP_IDLE,
        )
    ]
    pending = asyncio.create_task(first_effects.run_once())
    await asyncio.wait_for(first_ledger.entered.wait(), timeout=5)
    snapshots.append(
        abstract_runtime(
            await store.load(stream_id),
            await attempt_store.load_for_stream(stream_id),
            OP_INVOKED,
        )
    )

    # Cancellation represents abrupt process loss after provider invocation and
    # before the handler returns an observation to the dispatcher.
    pending.cancel()
    try:
        await pending
    except asyncio.CancelledError:
        pass
    snapshots.append(
        abstract_runtime(
            await store.load(stream_id),
            await attempt_store.load_for_stream(stream_id),
            OP_IDLE,
        )
    )

    second_ledger = InvocationLedger(
        first_ledger.operation_ids,
        asyncio.Event(),
        asyncio.Event(),
    )
    fresh_tools = ToolRegistry.from_functions(get_weather)
    fresh_agent, _ = define(fresh_tools)
    fresh_runtime = fresh_agent.build_runtime(
        context={"model": ObservedProvider(second_ledger), "tools": fresh_tools}
    )
    fresh_attempt_store = await SQLiteEffectAttemptStore.open(attempt_path)
    fresh_effects = DurableEffectDispatcher(
        agent=fresh_runtime.agent,
        store=store,
        effects=fresh_runtime.effects,
        context=fresh_runtime.context,
        subscription_name="assistant:1:effects",
        attempt_store=fresh_attempt_store,
    )
    retry = asyncio.create_task(fresh_effects.run_once())
    await asyncio.wait_for(second_ledger.entered.wait(), timeout=5)
    snapshots.append(
        abstract_runtime(
            await store.load(stream_id),
            await fresh_attempt_store.load_for_stream(stream_id),
            OP_INVOKED,
        )
    )
    second_ledger.release.set()
    await retry
    attempts = await fresh_attempt_store.load_for_stream(stream_id)
    snapshots.append(
        abstract_runtime(await store.load(stream_id), attempts, OP_IDLE)
    )

    if len(second_ledger.operation_ids) != 2:
        raise AssertionError("crash/retry did not produce exactly two physical invocations")
    if len(set(second_ledger.operation_ids)) != 1:
        raise AssertionError("retry changed the durable operation_id")
    if [attempt.operation_id for attempt in attempts] != second_ledger.operation_ids:
        raise AssertionError(
            "durable dispatch attempts do not match observed provider entries"
        )

    for snapshot in snapshots:
        if snapshot not in states or snapshot not in invariant:
            raise AssertionError(f"runtime snapshot is outside crash invariant: {snapshot}")
    for parent, child in zip(snapshots, snapshots[1:]):
        if (parent, child) not in transitions:
            raise AssertionError(f"runtime boundary has no formal transition: {parent} -> {child}")
    return len(snapshots), len(attempts)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agentlog-crash-refinement-") as directory:
        states, invariant, transitions = build_graph(Path(directory))
        snapshots, invocations = asyncio.run(
            verify_runtime_trace(
                states,
                invariant,
                transitions,
                Path(directory) / "attempts.db",
            )
        )
    print(
        "CRASH_RUNTIME_REFINEMENT_PASS "
        f"scenarios=1 snapshots={snapshots} physical_invocations={invocations} "
        "committed_results=1 stable_operation_id=1 unmatched_transitions=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
