from __future__ import annotations

import json
from dataclasses import dataclass

from aiq import (
    Agent,
    ArtifactRef,
    DurableDispatcher,
    DurableEffectDispatcher,
    DurableModelLoop,
    Event,
    InMemoryArtifactStore,
    InMemoryEventStore,
    ModelLoopLimits,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolArgumentsRejected,
    ToolDefinition,
    ToolRegistry,
    SQLiteArtifactStore,
    artifact_digest,
    build_causal_trace,
    run_stream_id,
)
from aiq.evals import CrashWindowEvidence, InvocationObservation
from tests.model.crash_window_harness import (
    CrashBeforeCommitSQLiteEventStore,
    InjectedProcessCrash,
)


DATASET = "production/orders/2026-08-01.parquet"
ETAG = "etag-abc"
DATASET_DIGEST = "sha256:" + "1" * 64
RULES_VERSION = "qa-rules-v17"


@dataclass(frozen=True)
class QAState:
    pass


@dataclass(frozen=True)
class UserMessageAdded:
    text: str


class ScriptedQAProvider:
    async def complete(self, request, *, operation_id):
        del operation_id
        previous = request.messages[-1]
        if previous.role == "user":
            call = ToolCall("call-rules", "list_rules", {"dataset": DATASET})
        elif previous.name == "list_rules":
            call = ToolCall("call-metadata", "read_dataset_metadata", {"path": DATASET})
        elif previous.name == "read_dataset_metadata":
            call = ToolCall(
                "call-qaqc",
                "run_qaqc",
                {
                    "pinned_path": f"{DATASET}@{ETAG}",
                    "dataset_digest": DATASET_DIGEST,
                    "rules_version": RULES_VERSION,
                },
            )
        elif previous.name == "run_qaqc":
            call = ToolCall(
                "call-report",
                "save_report",
                {"result": previous.content},
            )
        else:
            return ModelResponse(
                ModelMessage("assistant", json.dumps({"status": "completed"}))
            )
        return ModelResponse(ModelMessage("assistant", "tool"), (call,))


class FakeMCPGateway:
    def __init__(
        self,
        artifacts,
        *,
        deny_dataset_metadata: bool = False,
        generation: int = 1,
        operational_log: list[tuple[str, str, int]] | None = None,
        external_reports: bool = False,
    ) -> None:
        self.artifacts = artifacts
        self.deny_dataset_metadata = deny_dataset_metadata
        self.generation = generation
        self.operational_log = operational_log if operational_log is not None else []
        self.external_reports = external_reports
        self.invocations: list[tuple[str, str, dict]] = []

    async def invoke(self, name, arguments, *, operation_id):
        plain = dict(arguments)
        self.invocations.append((name, operation_id, plain))
        self.operational_log.append((name, operation_id, self.generation))
        if name == "list_rules":
            return {
                "dataset": DATASET,
                "rules_version": RULES_VERSION,
                "rules": ("r1", "r2"),
            }
        if name == "read_dataset_metadata":
            if self.deny_dataset_metadata:
                raise ToolArgumentsRejected("policy_denied")
            return {
                "path": DATASET,
                "etag": ETAG,
                "digest": DATASET_DIGEST,
                "rows": 100,
            }
        if name == "run_qaqc":
            assert plain["pinned_path"] == f"{DATASET}@{ETAG}"
            assert plain["dataset_digest"] == DATASET_DIGEST
            assert plain["rules_version"] == RULES_VERSION
            return {"passed": False, "failed_rules": 1}
        if name == "save_report":
            if self.external_reports:
                content = plain["result"].encode()
                ref = ArtifactRef(
                    "qaqc-report.json",
                    operation_id,
                    "application/json",
                    artifact_digest(content),
                    len(content),
                    operation_id,
                    f"s3://reports/{operation_id}/qaqc-report.json?versionId=exact-v1",
                )
                registered = await self.artifacts.register_external(ref)
                return registered.to_data()
            ref = await self.artifacts.put(
                "qaqc-report.json",
                plain["result"].encode(),
                media_type="application/json",
                version="report-v1",
            )
            return ref.to_data()
        raise AssertionError(name)


class MCPTool:
    def __init__(self, gateway: FakeMCPGateway, definition: ToolDefinition) -> None:
        self.gateway = gateway
        self.definition = definition

    async def execute(self, arguments, *, operation_id):
        return await self.gateway.invoke(
            self.definition.name, arguments, operation_id=operation_id
        )


def _definition(name: str, properties: dict[str, dict]) -> ToolDefinition:
    return ToolDefinition(
        name,
        f"QA/QC MCP operation {name}",
        {
            "type": "object",
            "properties": properties,
            "required": tuple(properties),
            "additionalProperties": False,
        },
    )


DEFINITIONS = (
    _definition("list_rules", {"dataset": {"type": "string"}}),
    _definition("read_dataset_metadata", {"path": {"type": "string"}}),
    _definition(
        "run_qaqc",
        {
            "pinned_path": {"type": "string"},
            "dataset_digest": {"type": "string"},
            "rules_version": {"type": "string"},
        },
    ),
    _definition("save_report", {"result": {"type": "string"}}),
)


def build(gateway: FakeMCPGateway):
    registry = ToolRegistry()
    for definition in DEFINITIONS:
        registry.register(MCPTool(gateway, definition))
    agent = Agent(name="qaqc", version="0.3", initial_state=QAState)
    agent.event(UserMessageAdded)
    loop = DurableModelLoop(
        start_on=UserMessageAdded,
        build_request=lambda state, event, definitions: ModelRequest(
            (ModelMessage("user", event.text),), definitions
        ),
        tool_definitions=registry.definitions(),
        provider="model",
        tools="mcp",
        limits=ModelLoopLimits(max_model_steps=5, max_tool_calls=4),
    )
    loop.install(agent)
    return agent, agent.build_runtime(
        context={"model": ScriptedQAProvider(), "mcp": registry}
    )


async def execute(
    *, restart_after_every_dispatch: bool, deny_dataset_metadata: bool = False
):
    store = InMemoryEventStore()
    artifacts = InMemoryArtifactStore()
    gateway = FakeMCPGateway(artifacts, deny_dataset_metadata=deny_dataset_metadata)
    agent, runtime = build(gateway)
    run_id = "restart" if restart_after_every_dispatch else "normal"
    stream_id = run_stream_id("qaqc", run_id)
    await store.append(
        stream_id,
        -1,
        (
            Event("RunCreated", {"agent": "qaqc", "definition_version": "0.3"}),
            Event(
                "UserMessageAdded",
                {"text": f"Проверь {DATASET} по QA/QC и сохрани отчёт"},
            ),
        ),
    )

    for _ in range(100):
        reactions = DurableDispatcher(
            agent=runtime.agent,
            store=store,
            subscription_name="qaqc:0.3:reactions",
        )
        effects = DurableEffectDispatcher(
            agent=runtime.agent,
            store=store,
            effects=runtime.effects,
            context=runtime.context,
            subscription_name="qaqc:0.3:effects",
        )
        progressed = await reactions.run_once()
        if restart_after_every_dispatch:
            agent, runtime = build(gateway)
            effects = DurableEffectDispatcher(
                agent=runtime.agent,
                store=store,
                effects=runtime.effects,
                context=runtime.context,
                subscription_name="qaqc:0.3:effects",
            )
        progressed = await effects.run_once() or progressed
        if restart_after_every_dispatch:
            agent, runtime = build(gateway)
        history = await store.load(stream_id)
        if runtime.agent.is_terminal(
            history, through_version=history[-1].stream_version
        ):
            break
        if not progressed:
            continue
    else:
        raise AssertionError("QA/QC run did not terminate")

    history = await store.load(stream_id)
    return (
        build_causal_trace(
            agent_name="qaqc",
            run_id=run_id,
            agent=runtime.agent,
            history=history,
        ),
        history,
        gateway,
        artifacts,
    )


async def execute_save_report_crash(path):
    store = await CrashBeforeCommitSQLiteEventStore.open(path)
    artifacts = await SQLiteArtifactStore.open(f"{path}.artifacts")
    operational_log: list[tuple[str, str, int]] = []
    first_gateway = FakeMCPGateway(
        artifacts,
        generation=1,
        operational_log=operational_log,
        external_reports=True,
    )
    first_agent, first_runtime = build(first_gateway)
    stream_id = run_stream_id("qaqc", "save-report-crash")
    await store.append(
        stream_id,
        -1,
        (
            Event("RunCreated", {"agent": "qaqc", "definition_version": "0.3"}),
            Event("UserMessageAdded", {"text": f"Проверь {DATASET} и сохрани отчёт"}),
        ),
    )

    def dispatchers(runtime):
        return (
            DurableDispatcher(
                agent=runtime.agent,
                store=store,
                subscription_name="qaqc:0.3:reactions",
            ),
            DurableEffectDispatcher(
                agent=runtime.agent,
                store=store,
                effects=runtime.effects,
                context=runtime.context,
                subscription_name="qaqc:0.3:effects",
            ),
        )

    reactions, effects = dispatchers(first_runtime)
    for _ in range(80):
        await reactions.run_once()
        history = await store.load(stream_id)
        save_requests = tuple(
            item
            for item in history
            if item.event.event_type == "ToolCallRequested"
            and item.event.data["call"]["name"] == "save_report"
        )
        if save_requests:
            request = save_requests[0]
            break
        await effects.run_once()
    else:
        raise AssertionError("save_report request boundary was not reached")

    store.arm("ToolCallSucceeded")
    for _ in range(80):
        try:
            await effects.run_once()
        except InjectedProcessCrash:
            break
    else:
        raise AssertionError("save_report result commit did not crash")

    checkpoint_after_crash = await store.load_checkpoint("qaqc:0.3:effects")
    history_after_crash = await store.load(stream_id)
    self_results = tuple(
        item
        for item in history_after_crash
        if item.event.event_type == "ToolCallSucceeded"
        and item.event.metadata.get("causation_id") == str(request.event.event_id)
    )
    if self_results:
        raise AssertionError("save_report result committed despite injected crash")

    fresh_gateway = FakeMCPGateway(
        artifacts,
        generation=2,
        operational_log=operational_log,
        external_reports=True,
    )
    fresh_agent, fresh_runtime = build(fresh_gateway)
    reactions, effects = dispatchers(fresh_runtime)
    for _ in range(100):
        await effects.run_once()
        await reactions.run_once()
        history = await store.load(stream_id)
        if fresh_runtime.agent.is_terminal(
            history, through_version=history[-1].stream_version
        ):
            break
    else:
        raise AssertionError("fresh runtime did not complete after save_report retry")

    history = await store.load(stream_id)
    trace = build_causal_trace(
        agent_name="qaqc",
        run_id="save-report-crash",
        agent=fresh_runtime.agent,
        history=history,
    )
    save_invocations = tuple(
        InvocationObservation("tool", operation_id, generation)
        for name, operation_id, generation in operational_log
        if name == "save_report"
    )
    return (
        CrashWindowEvidence(
            kind="tool",
            trace=trace,
            request_event_id=str(request.event.event_id),
            request_global_position=request.global_position,
            checkpoint_after_crash=checkpoint_after_crash,
            result_event_type="ToolCallSucceeded",
            invocations=save_invocations,
        ),
        history,
        artifacts,
        operational_log,
    )
