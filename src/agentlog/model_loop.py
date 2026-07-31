"""Composable durable model -> optional tool -> model policy."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from .framework import Agent, DefinitionError
from .models import (
    ModelCallFailedError,
    ModelCallRejectedError,
    ModelOutputRejectedError,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolDefinition,
)
from .tools import (
    ToolArgumentsRejected,
    ToolExecutionFailed,
    ToolRegistry,
    tool_definition_fingerprint,
)

State = TypeVar("State")
StartEvent = TypeVar("StartEvent")


class DefinitionResourceMismatch(DefinitionError):
    def __init__(
        self,
        *,
        policy: str,
        resource_key: str,
        missing: tuple[str, ...] = (),
        unexpected: tuple[str, ...] = (),
        changed: tuple[str, ...] = (),
    ) -> None:
        self.policy = policy
        self.resource_key = resource_key
        self.missing = missing
        self.unexpected = unexpected
        self.changed = changed
        super().__init__(
            f"resource {resource_key!r} does not match policy {policy!r}: "
            f"missing={missing}, unexpected={unexpected}, changed={changed}"
        )


@dataclass(frozen=True, slots=True)
class ModelLoopLimits:
    max_model_steps: int = 8
    max_tool_calls: int = 8

    def __post_init__(self) -> None:
        if self.max_model_steps <= 0 or self.max_tool_calls <= 0:
            raise ValueError("model-loop limits must be positive")


@dataclass(frozen=True, slots=True)
class ModelLoopEvents:
    ModelCallRequested: type
    ModelCallSucceeded: type
    ModelCallRejected: type
    ModelCallFailed: type
    ModelOutputRejected: type
    ToolCallRequested: type
    ToolCallSucceeded: type
    ToolCallRejected: type
    ToolCallFailed: type
    AnswerProduced: type
    ModelLoopLimitExceeded: type
    RunCompleted: type
    RunFailed: type


def _event_name(namespace: str, base_name: str) -> str:
    if namespace == "model":
        return base_name
    prefix = "".join(part.capitalize() for part in namespace.split("_"))
    return f"{prefix}{base_name}"


def _event_class(namespace: str, name: str, fields: list[tuple]) -> type:
    return dataclasses.make_dataclass(
        _event_name(namespace, name), fields, frozen=True, slots=True
    )


def _build_events(namespace: str) -> ModelLoopEvents:
    transport_id = dataclasses.field(default="")
    return ModelLoopEvents(
        ModelCallRequested=_event_class(
            namespace,
            "ModelCallRequested",
            [
                ("request", Mapping),
                ("model_step", int),
                ("tool_calls_used", int),
                ("operation_id", str, transport_id),
            ],
        ),
        ModelCallSucceeded=_event_class(
            namespace,
            "ModelCallSucceeded",
            [("response", Mapping), ("continuation", Mapping)],
        ),
        ModelCallRejected=_event_class(namespace, "ModelCallRejected", [("reason", str)]),
        ModelCallFailed=_event_class(namespace, "ModelCallFailed", [("reason", str)]),
        ModelOutputRejected=_event_class(namespace, "ModelOutputRejected", [("reason", str)]),
        ToolCallRequested=_event_class(
            namespace,
            "ToolCallRequested",
            [
                ("call", Mapping),
                ("expected_definition", Mapping),
                ("continuation", Mapping),
                ("operation_id", str, dataclasses.field(default="")),
            ],
        ),
        ToolCallSucceeded=_event_class(
            namespace,
            "ToolCallSucceeded",
            [
                ("call_id", str),
                ("name", str),
                ("result", object),
                ("continuation", Mapping),
            ],
        ),
        ToolCallRejected=_event_class(namespace, "ToolCallRejected", [("reason", str)]),
        ToolCallFailed=_event_class(namespace, "ToolCallFailed", [("reason", str)]),
        AnswerProduced=_event_class(namespace, "AnswerProduced", [("answer", str)]),
        ModelLoopLimitExceeded=_event_class(
            namespace, "ModelLoopLimitExceeded", [("reason", str)]
        ),
        RunCompleted=_event_class(namespace, "RunCompleted", []),
        RunFailed=_event_class(namespace, "RunFailed", [("reason", str)]),
    )


class DurableModelLoop(Generic[State, StartEvent]):
    """Install lifecycle events/reactions/effects on an ordinary ``Agent``.

    The installed handlers close over immutable definition data only. Provider
    and executable tools are resolved from registration-specific resources.
    """

    def __init__(
        self,
        *,
        start_on: type[StartEvent],
        build_request: Callable[
            [State, StartEvent, tuple[ToolDefinition, ...]], ModelRequest
        ],
        tool_definitions: tuple[ToolDefinition, ...],
        provider: str,
        tools: str,
        limits: ModelLoopLimits | None = None,
        namespace: str = "model",
    ) -> None:
        if not provider or not tools:
            raise ValueError("provider and tools resource keys must not be empty")
        if not namespace.isidentifier():
            raise ValueError("policy namespace must be a Python identifier")
        definitions = tuple(tool_definitions)
        names = [definition.name for definition in definitions]
        if len(names) != len(set(names)):
            raise DefinitionError("tool_definitions contain duplicate names")
        self.name = namespace
        self.start_on = start_on
        self.build_request = build_request
        self.tool_definitions = definitions
        self.provider_key = provider
        self.tools_key = tools
        self.limits = limits or ModelLoopLimits()
        self.events = _build_events(namespace)
        self._installed = False

    def install(self, agent: Agent) -> None:
        if self._installed:
            raise DefinitionError("DurableModelLoop instance is already installed")
        policy_name = self.name
        provider_key = self.provider_key
        tools_key = self.tools_key
        tool_definitions = self.tool_definitions
        build_request = self.build_request
        limits = self.limits
        events = self.events
        installed_fingerprints = {
            item.name: tool_definition_fingerprint(item) for item in tool_definitions
        }

        def validate_resources(resources: object) -> None:
            _validate_resource_contract(
                resources,
                policy_name=policy_name,
                provider_key=provider_key,
                tools_key=tools_key,
                expected=installed_fingerprints,
            )

        agent._claim_policy(policy_name, validate_resources=validate_resources)
        self._installed = True
        for event_type in dataclasses.astuple(events):
            agent.event(event_type)

        @agent.react(self.start_on)
        def start(state: State, event: StartEvent):
            try:
                request = build_request(state, event, tool_definitions)
            except ModelCallRejectedError as error:
                return events.ModelCallRejected(str(error))
            if not isinstance(request, ModelRequest):
                raise TypeError("build_request must return ModelRequest")
            _validate_selected_tools(request.tools, installed_fingerprints)
            return events.ModelCallRequested(request.to_data(), 1, 0)

        @agent.effect(events.ModelCallRequested)
        async def call_model(event, resources):
            provider = _resource(resources, provider_key, policy_name=policy_name)
            if not callable(getattr(provider, "complete", None)):
                raise TypeError(f"resource {provider_key!r} must be a ModelProvider")
            request = ModelRequest.from_data(event.request)
            continuation = {
                "request": request.to_data(),
                "model_step": event.model_step,
                "tool_calls_used": event.tool_calls_used,
            }
            try:
                response = await provider.complete(request, operation_id=event.operation_id)
                if not isinstance(response, ModelResponse):
                    raise ModelOutputRejectedError("provider did not return ModelResponse")
            except ModelCallRejectedError as error:
                return events.ModelCallRejected(str(error))
            except ModelCallFailedError as error:
                return events.ModelCallFailed(str(error))
            except ModelOutputRejectedError as error:
                return events.ModelOutputRejected(str(error))
            return events.ModelCallSucceeded(response.to_data(), continuation)

        @agent.react(events.ModelCallSucceeded)
        def interpret_model(state, event):
            del state
            response = ModelResponse.from_data(event.response)
            if len(response.tool_calls) > 1:
                return events.ModelOutputRejected(
                    "single-tool policy does not support multiple tool calls"
                )
            if not response.tool_calls:
                if not response.message.content:
                    return events.ModelOutputRejected("final model response is empty")
                return (
                    events.AnswerProduced(response.message.content),
                    events.RunCompleted(),
                )
            continuation = dict(event.continuation)
            used = int(continuation["tool_calls_used"])
            if used >= limits.max_tool_calls:
                return events.ModelLoopLimitExceeded("maximum tool calls exceeded")
            request = ModelRequest.from_data(continuation["request"])
            call = response.tool_calls[0]
            expected = next((tool for tool in request.tools if tool.name == call.name), None)
            if expected is None:
                return events.ToolCallRejected(f"unknown tool: {call.name!r}")
            continuation.update(
                {
                    "assistant_message": response.message.to_data(),
                    "tool_calls_used": used + 1,
                }
            )
            return events.ToolCallRequested(
                call.to_data(), expected.to_data(), continuation
            )

        @agent.effect(events.ToolCallRequested)
        async def call_tool(event, resources):
            registry = _resource(resources, tools_key, policy_name=policy_name)
            if not isinstance(registry, ToolRegistry):
                raise TypeError(f"resource {tools_key!r} must be ToolRegistry")
            call = ToolCall.from_data(event.call)
            expected = ToolDefinition.from_data(event.expected_definition)
            tool = registry.resolve(call.name)
            if tool_definition_fingerprint(tool.definition) != tool_definition_fingerprint(
                expected
            ):
                raise DefinitionResourceMismatch(
                    policy=policy_name,
                    resource_key=tools_key,
                    changed=(call.name,),
                )
            try:
                registry.validate(call.name, call.arguments)
                result = await tool.execute(call.arguments, operation_id=event.operation_id)
            except (LookupError, ToolArgumentsRejected) as error:
                return events.ToolCallRejected(str(error))
            except ToolExecutionFailed as error:
                return events.ToolCallFailed(str(error))
            return events.ToolCallSucceeded(
                call.call_id, call.name, result, event.continuation
            )

        @agent.react(events.ToolCallSucceeded)
        def continue_model(state, event):
            del state
            continuation = event.continuation
            step = int(continuation["model_step"])
            if step >= limits.max_model_steps:
                return events.ModelLoopLimitExceeded("maximum model steps exceeded")
            request = ModelRequest.from_data(continuation["request"])
            assistant = ModelResponse.from_data(
                {"message": continuation["assistant_message"], "tool_calls": ()}
            ).message
            from .core import Event
            from .llm import tool_result_message

            tool_message = tool_result_message(
                Event(
                    "ToolCallSucceeded",
                    {"call_id": event.call_id, "name": event.name, "result": event.result},
                )
            )
            next_request = ModelRequest(
                request.messages + (assistant, tool_message), request.tools, request.model
            )
            return events.ModelCallRequested(
                next_request.to_data(),
                step + 1,
                int(continuation["tool_calls_used"]),
            )

        for failure_type in (
            events.ModelCallRejected,
            events.ModelCallFailed,
            events.ModelOutputRejected,
            events.ToolCallRejected,
            events.ToolCallFailed,
            events.ModelLoopLimitExceeded,
        ):
            agent.react(failure_type)(
                lambda state, event: events.RunFailed(str(event.reason))
            )
        agent.terminal(events.RunCompleted, status="completed")
        agent.terminal(events.RunFailed, status="failed")

def _validate_selected_tools(
    selected: tuple[ToolDefinition, ...], installed: Mapping[str, str]
) -> None:
    for definition in selected:
        if installed.get(definition.name) != tool_definition_fingerprint(definition):
            raise DefinitionError(
                f"build_request selected unknown or changed tool {definition.name!r}"
            )


def _validate_resource_contract(
    resources: object,
    *,
    policy_name: str,
    provider_key: str,
    tools_key: str,
    expected: Mapping[str, str],
) -> None:
    provider = _resource(resources, provider_key, policy_name=policy_name)
    if not callable(getattr(provider, "complete", None)):
        raise DefinitionResourceMismatch(
            policy=policy_name, resource_key=provider_key, changed=("provider",)
        )
    registry = _resource(resources, tools_key, policy_name=policy_name)
    if not isinstance(registry, ToolRegistry):
        raise DefinitionResourceMismatch(
            policy=policy_name, resource_key=tools_key, changed=("registry",)
        )
    actual = {
        item.name: tool_definition_fingerprint(item) for item in registry.definitions()
    }
    missing = tuple(sorted(expected.keys() - actual.keys()))
    unexpected = tuple(sorted(actual.keys() - expected.keys()))
    changed = tuple(
        sorted(
            name
            for name in expected.keys() & actual.keys()
            if expected[name] != actual[name]
        )
    )
    if missing or unexpected or changed:
        raise DefinitionResourceMismatch(
            policy=policy_name,
            resource_key=tools_key,
            missing=missing,
            unexpected=unexpected,
            changed=changed,
        )


def _resource(resources: object, key: str, *, policy_name: str) -> Any:
    if not isinstance(resources, Mapping):
        raise DefinitionResourceMismatch(
            policy=policy_name, resource_key=key, missing=(key,)
        )
    try:
        return resources[key]
    except KeyError as error:
        raise DefinitionResourceMismatch(
            policy=policy_name, resource_key=key, missing=(key,)
        ) from error
