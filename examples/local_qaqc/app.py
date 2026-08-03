from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from aiq import (
    Agent,
    ArtifactRef,
    DurableModelLoop,
    Event,
    MCPTool,
    ModelLoopLimits,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    OllamaProvider,
    SQLiteArtifactStore,
    SQLiteEventStore,
    ToolCall,
    ToolDefinition,
    ToolExecutionFailed,
    ToolRegistry,
    run_stream_id,
)
from aiq.fastapi import AIQApplication

MCP_URL = os.getenv("MCP_URL", "http://mcp-server:8001/mcp")
DATASET = "datasets/orders.json"
FAULT = os.getenv("AIQ_FAULT", "none")
FAULT_DIR = Path(os.getenv("FAULT_DIR", "/data/faults"))


@dataclass(frozen=True)
class QAState:
    answer: str | None = None
    failure: str | None = None


@dataclass(frozen=True)
class TaskSubmitted:
    text: str


class DeterministicProvider:
    async def complete(
        self, request: ModelRequest, *, operation_id: str
    ) -> ModelResponse:
        del operation_id
        previous = request.messages[-1]
        if previous.role == "user":
            call = ToolCall("rules", "list_rules", {"dataset": DATASET})
        elif previous.name == "list_rules":
            call = ToolCall("dataset", "stat_dataset", {"path": DATASET})
        elif previous.name == "stat_dataset":
            pinned = json.loads(previous.content)
            rules_message = next(
                message
                for message in reversed(request.messages)
                if message.name == "list_rules"
            )
            rules = json.loads(rules_message.content)
            call = ToolCall(
                "qaqc",
                "run_qaqc",
                {
                    "path": pinned["path"],
                    "version_id": pinned["version_id"],
                    "dataset_digest": pinned["digest"],
                    "rules_version": rules["rules_version"],
                },
            )
        elif previous.name == "run_qaqc":
            call = ToolCall(
                "report",
                "save_report",
                {"result_json": previous.content},
            )
        else:
            return ModelResponse(ModelMessage("assistant", "QA/QC report saved"))
        return ModelResponse(ModelMessage("assistant", "tool call"), (call,))


class QaqcMCPTool:
    def __init__(
        self,
        definition: ToolDefinition,
        artifacts: SQLiteArtifactStore,
    ) -> None:
        self._transport = MCPTool(
            definition,
            url=MCP_URL,
            operation_id_argument=(
                "operation_id" if definition.name == "save_report" else None
            ),
        )
        self._artifacts = artifacts

    @property
    def definition(self) -> ToolDefinition:
        return self._transport.definition

    async def execute(self, arguments, *, operation_id: str):
        value = await self._transport.execute(
            arguments, operation_id=operation_id
        )
        while (
            isinstance(value, Mapping)
            and set(value) == {"result"}
            and isinstance(value["result"], Mapping)
        ):
            value = value["result"]
        if not isinstance(value, Mapping):
            raise ToolExecutionFailed("MCP tool returned no structured object")
        if self.definition.name == "save_report":
            raw_ref = value.get("artifact_ref", value)
            ref = ArtifactRef.from_data(raw_ref)
            self._crash_once("after_put_before_registration", operation_id)
            registered = await self._artifacts.register_external(ref)
            self._crash_once("after_registration_before_result", operation_id)
            return registered.to_data()
        return value

    @staticmethod
    def _crash_once(boundary: str, operation_id: str) -> None:
        if FAULT != boundary:
            return
        FAULT_DIR.mkdir(parents=True, exist_ok=True)
        marker = FAULT_DIR / f"{boundary}-{operation_id}"
        if marker.exists():
            return
        marker.write_text(operation_id)
        os._exit(86)


def definition(name: str, properties: dict[str, dict]) -> ToolDefinition:
    return ToolDefinition(
        name,
        f"Local QA/QC MCP tool: {name}",
        {
            "type": "object",
            "properties": properties,
            "required": tuple(properties),
            "additionalProperties": False,
        },
    )


DEFINITIONS = (
    definition("list_rules", {"dataset": {"type": "string"}}),
    definition("stat_dataset", {"path": {"type": "string"}}),
    definition(
        "run_qaqc",
        {
            "path": {"type": "string"},
            "version_id": {"type": "string"},
            "dataset_digest": {"type": "string"},
            "rules_version": {"type": "string"},
        },
    ),
    definition(
        "save_report",
        {
            "result_json": {"type": "string"},
        },
    ),
)


def define_agent(registry: ToolRegistry) -> Agent:
    agent = Agent(name="local-qaqc", version="0.3-lab", initial_state=QAState)
    agent.event(TaskSubmitted)
    loop = DurableModelLoop(
        start_on=TaskSubmitted,
        build_request=lambda state, event, tools: ModelRequest(
            (
                ModelMessage(
                    "system",
                    "Use exactly one provided tool per response and never imitate a "
                    "tool call in text. First call list_rules with dataset "
                    "datasets/orders.json. After each tool result call the next tool "
                    "in this order: stat_dataset, run_qaqc, save_report. Reuse exact "
                    "identities from tool results and never invent them.",
                ),
                ModelMessage("user", event.text),
            ),
            tools,
        ),
        tool_definitions=registry.definitions(),
        provider="provider",
        tools="mcp",
        limits=ModelLoopLimits(max_model_steps=6, max_tool_calls=4),
    )
    loop.install(agent)

    @agent.reduce(loop.events.AnswerProduced)
    def answer(state: QAState, event):
        return replace(state, answer=event.answer)

    @agent.reduce(loop.events.RunFailed)
    def failure(state: QAState, event):
        return replace(state, failure=event.reason)

    return agent


class StartRequest(BaseModel):
    run_id: str
    task: str = "Check orders and save a QA/QC report"


async def build() -> FastAPI:
    event_store = await SQLiteEventStore.open(
        Path(os.getenv("AIQ_DB", "/data/aiq.db"))
    )
    artifacts = await SQLiteArtifactStore.open(
        Path(os.getenv("ARTIFACT_DB", "/data/artifacts.db"))
    )
    registry = ToolRegistry()
    for item in DEFINITIONS:
        registry.register(QaqcMCPTool(item, artifacts))
    if os.getenv("AIQ_PROVIDER", "deterministic") == "ollama":
        provider = OllamaProvider(
            model=os.getenv("OLLAMA_MODEL", "llama3.2:1b"),
            url=os.getenv("OLLAMA_URL", "http://ollama:11434/api/chat"),
            think=False,
        )
    else:
        provider = DeterministicProvider()
    agent = define_agent(registry)
    application = AIQApplication(store=event_store, poll_interval_seconds=0.05)
    application.register(agent, resources={"provider": provider, "mcp": registry})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            async with application.lifespan(app):
                yield
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    api = FastAPI(lifespan=lifespan)
    api.include_router(application.router, prefix="/aiq")

    @api.get("/health")
    async def health():
        return {
            "status": "ok",
            "provider": os.getenv("AIQ_PROVIDER", "deterministic"),
        }

    @api.post("/runs", status_code=202)
    async def start(request: StartRequest):
        stream = run_stream_id(agent.name, request.run_id)
        if await event_store.load(stream):
            raise HTTPException(409, "run already exists")
        await event_store.append(
            stream,
            -1,
            (
                Event(
                    "RunCreated",
                    {"agent": agent.name, "definition_version": "0.3-lab"},
                ),
                Event("TaskSubmitted", {"text": request.task}),
            ),
        )
        return {"run_id": request.run_id}

    @api.get("/runs/{run_id}")
    async def read(run_id: str):
        history = await event_store.load(run_stream_id(agent.name, run_id))
        return {
            "run_id": run_id,
            "events": [
                {
                    "type": item.event.event_type,
                    "data": dict(item.event.data),
                    "metadata": dict(item.event.metadata),
                }
                for item in history
            ],
        }

    return api


app = __import__("asyncio").run(build())
