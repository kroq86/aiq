"""Composable durable model -> optional tool -> model policy."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from .artifacts import (
    ArtifactDigestMismatchError,
    ArtifactNotFoundError,
)
from .core import Event
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
from .middleware import (
    AgentMiddleware,
    MiddlewareExecutionError,
    ModelCallContext,
    ToolCallContext,
    ToolRequest,
    apply_after_model,
    apply_after_tool,
    apply_before_model,
    apply_before_tool,
    validate_middleware,
)
from .instructions import InstructionResolutionError
from .tools import (
    ToolArgumentsRejected,
    ToolExecutionFailed,
    ToolRegistry,
    tool_definition_fingerprint,
)
from .validation import (
    PostconditionFailed,
    ToolValidationContext,
    ValidationAccepted,
    ValidationAmbiguous,
    ValidationDecision,
    ValidationRejected,
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
    max_state_visits: int = 2

    def __post_init__(self) -> None:
        if (
            self.max_model_steps <= 0
            or self.max_tool_calls <= 0
            or self.max_state_visits <= 0
        ):
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
    ToolValidationSucceeded: type
    ToolValidationFailed: type
    AnswerProduced: type
    ModelLoopLimitExceeded: type
    WorkflowInvariantViolated: type
    GoalSatisfied: type
    GoalNotSatisfied: type
    WorkflowCycleDetected: type
    MiddlewareFailed: type
    ArtifactResolutionFailed: type
    InstructionResolutionFailed: type
    RunCompleted: type
    RunFailed: type
    RunAbstained: type


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
                ("control", Mapping, dataclasses.field(default_factory=dict)),
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
        ToolValidationSucceeded=_event_class(
            namespace,
            "ToolValidationSucceeded",
            [
                ("call_id", str),
                ("name", str),
                ("phase", str),
                ("evidence", Mapping),
            ],
        ),
        ToolValidationFailed=_event_class(
            namespace,
            "ToolValidationFailed",
            [
                ("call_id", str),
                ("name", str),
                ("reason", str),
                ("phase", str),
                ("retryable", bool),
                ("details", Mapping),
                ("continuation", Mapping),
            ],
        ),
        AnswerProduced=_event_class(namespace, "AnswerProduced", [("answer", str)]),
        ModelLoopLimitExceeded=_event_class(
            namespace, "ModelLoopLimitExceeded", [("reason", str)]
        ),
        WorkflowInvariantViolated=_event_class(
            namespace, "WorkflowInvariantViolated", [("reason", str)]
        ),
        GoalSatisfied=_event_class(
            namespace, "GoalSatisfied", [("evidence", Mapping)]
        ),
        GoalNotSatisfied=_event_class(
            namespace, "GoalNotSatisfied", [("reason", str)]
        ),
        WorkflowCycleDetected=_event_class(
            namespace, "WorkflowCycleDetected", [("reason", str)]
        ),
        MiddlewareFailed=_event_class(
            namespace,
            "MiddlewareFailed",
            [("middleware_id", str), ("phase", str), ("reason", str)],
        ),
        ArtifactResolutionFailed=_event_class(
            namespace, "ArtifactResolutionFailed", [("reason", str)]
        ),
        InstructionResolutionFailed=_event_class(
            namespace, "InstructionResolutionFailed", [("reason", str)]
        ),
        RunCompleted=_event_class(namespace, "RunCompleted", []),
        RunFailed=_event_class(namespace, "RunFailed", [("reason", str)]),
        RunAbstained=_event_class(namespace, "RunAbstained", [("reason", str)]),
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
        tool_policy: str | None = None,
        snapshot_state: Callable[[State], Any] | None = None,
        workflow_invariant: Callable[[State], bool | str] | None = None,
        goal_satisfied: Callable[[State], bool] | None = None,
        limits: ModelLoopLimits | None = None,
        middleware: tuple[AgentMiddleware, ...] = (),
        artifacts: str | None = None,
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
        self.tool_policy_key = tool_policy
        self.snapshot_state = snapshot_state
        self.workflow_invariant = workflow_invariant
        self.goal_satisfied = goal_satisfied
        self.limits = limits or ModelLoopLimits()
        self.middleware = tuple(middleware)
        validate_middleware(self.middleware)
        self.artifacts_key = artifacts
        self.events = _build_events(namespace)
        self._installed = False

    def install(self, agent: Agent) -> None:
        if self._installed:
            raise DefinitionError("DurableModelLoop instance is already installed")
        policy_name = self.name
        provider_key = self.provider_key
        tools_key = self.tools_key
        tool_policy_key = self.tool_policy_key
        snapshot_state = self.snapshot_state
        workflow_invariant = self.workflow_invariant
        goal_satisfied = self.goal_satisfied
        tool_definitions = self.tool_definitions
        build_request = self.build_request
        limits = self.limits
        middleware = self.middleware
        artifacts_key = self.artifacts_key
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
                tool_policy_key=tool_policy_key,
                artifacts_key=artifacts_key,
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
            except InstructionResolutionError as error:
                return events.InstructionResolutionFailed(str(error))
            if not isinstance(request, ModelRequest):
                raise TypeError("build_request must return ModelRequest")
            try:
                request = apply_before_model(
                    middleware,
                    ModelCallContext(policy_name, 1, 0),
                    request,
                )
                _validate_selected_tools(request.tools, installed_fingerprints)
            except MiddlewareExecutionError as error:
                return events.MiddlewareFailed(
                    error.middleware_id, error.phase, error.reason
                )
            control = _initial_control(snapshot_state, state)
            return events.ModelCallRequested(request.to_data(), 1, 0, control=control)

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
                "control": event.control,
            }
            try:
                if request.artifacts:
                    if artifacts_key is None:
                        return events.ArtifactResolutionFailed(
                            "model request contains artifacts but no artifact store is configured"
                        )
                    artifact_store = _resource(
                        resources, artifacts_key, policy_name=policy_name
                    )
                    try:
                        await artifact_store.get_many(request.artifacts)
                    except (ArtifactNotFoundError, ArtifactDigestMismatchError) as error:
                        return events.ArtifactResolutionFailed(str(error))
                response = await provider.complete(request, operation_id=event.operation_id)
                if not isinstance(response, ModelResponse):
                    raise ModelOutputRejectedError("provider did not return ModelResponse")
            except ModelCallRejectedError as error:
                return events.ModelCallRejected(str(error))
            except ModelCallFailedError as error:
                return events.ModelCallFailed(str(error))
            except ModelOutputRejectedError as error:
                return events.ModelOutputRejected(str(error))
            try:
                response = await apply_after_model(
                    middleware,
                    ModelCallContext(
                        policy_name,
                        event.model_step,
                        event.tool_calls_used,
                        event.operation_id,
                    ),
                    response,
                )
            except MiddlewareExecutionError as error:
                return events.MiddlewareFailed(
                    error.middleware_id, error.phase, error.reason
                )
            return events.ModelCallSucceeded(response.to_data(), continuation)

        @agent.react(events.ModelCallSucceeded)
        def interpret_model(state, event):
            response = ModelResponse.from_data(event.response)
            if len(response.tool_calls) > 1:
                return events.ModelOutputRejected(
                    "single-tool policy does not support multiple tool calls"
                )
            if not response.tool_calls:
                if not response.message.content:
                    return events.ModelOutputRejected("final model response is empty")
                invariant_reason = _invariant_failure(workflow_invariant, state)
                if invariant_reason is not None:
                    return events.WorkflowInvariantViolated(invariant_reason)
                if goal_satisfied is not None and not goal_satisfied(state):
                    return events.GoalNotSatisfied("workflow goal is not satisfied")
                completed = (
                    events.AnswerProduced(response.message.content),
                    events.RunCompleted(),
                )
                if goal_satisfied is None:
                    return completed
                return (
                    events.GoalSatisfied({"checked": True}),
                    *completed,
                )
            continuation = dict(event.continuation)
            cycle_reason = _cycle_failure(continuation, limits.max_state_visits)
            if cycle_reason is not None:
                return events.WorkflowCycleDetected(cycle_reason)
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
            try:
                tool_request = apply_before_tool(
                    middleware,
                    ToolCallContext(
                        policy_name,
                        int(continuation["model_step"]),
                        int(continuation["tool_calls_used"]),
                    ),
                    ToolRequest(call, expected),
                )
            except MiddlewareExecutionError as error:
                return events.MiddlewareFailed(
                    error.middleware_id, error.phase, error.reason
                )
            return events.ToolCallRequested(
                tool_request.call.to_data(),
                tool_request.expected_definition.to_data(),
                continuation,
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
                policy = None
                evidence = {}
                result_evidence = {}
                request_validation_event = None
                validation_context = ToolValidationContext(
                    policy_name,
                    int(event.continuation["model_step"]),
                    int(event.continuation["tool_calls_used"]),
                    event.operation_id,
                    _control_snapshot(event.continuation),
                )
                if tool_policy_key is not None:
                    policy = _resource(resources, tool_policy_key, policy_name=policy_name)
                    execution_policy = callable(getattr(policy, "validate_input", None))
                    if execution_policy:
                        evidence_parts = {}
                        for phase, hook_name in (
                            ("input", "validate_input"),
                            ("transition", "validate_transition"),
                        ):
                            decision = await getattr(policy, hook_name)(
                                call, validation_context
                            )
                            if not isinstance(decision, ValidationDecision):
                                raise TypeError(
                                    f"{hook_name} must return ValidationDecision"
                                )
                            if decision.status != "accept":
                                return events.ToolValidationFailed(
                                    call.call_id,
                                    call.name,
                                    decision.message or decision.code or decision.status,
                                    phase,
                                    decision.status in {"retry", "replan"},
                                    {
                                        "status": decision.status,
                                        "code": decision.code,
                                        "evidence": decision.evidence,
                                    },
                                    event.continuation,
                                )
                            evidence_parts[phase] = decision.evidence
                            if decision.normalized_value is not None:
                                if not isinstance(decision.normalized_value, Mapping):
                                    raise TypeError(
                                        f"{hook_name} normalized_value must be an object"
                                    )
                                call = ToolCall(
                                    call.call_id, call.name, decision.normalized_value
                                )
                        evidence = evidence_parts
                        request_validation_event = events.ToolValidationSucceeded(
                            call.call_id, call.name, "request", evidence
                        )
                        decision = None
                    else:
                        decision = await policy.validate_request(
                            call, validation_context
                        )
                    if decision is None:
                        pass
                    elif isinstance(decision, ValidationDecision):
                        if decision.status != "accept":
                            return events.ToolValidationFailed(
                                call.call_id,
                                call.name,
                                decision.message or decision.code or decision.status,
                                "request",
                                decision.status in {"retry", "replan"},
                                {
                                    "status": decision.status,
                                    "code": decision.code,
                                    "evidence": decision.evidence,
                                },
                                event.continuation,
                            )
                        evidence = {"value": decision.evidence}
                        if decision.normalized_value is not None:
                            if not isinstance(decision.normalized_value, Mapping):
                                raise TypeError(
                                    "request normalized_value must be an object"
                                )
                            call = ToolCall(
                                call.call_id, call.name, decision.normalized_value
                            )
                        request_validation_event = events.ToolValidationSucceeded(
                            call.call_id, call.name, "request", evidence
                        )
                    elif isinstance(decision, ValidationRejected):
                        return events.ToolValidationFailed(
                            call.call_id,
                            call.name,
                            decision.reason,
                            "request",
                            decision.retryable,
                            {},
                            event.continuation,
                        )
                    elif isinstance(decision, ValidationAmbiguous):
                        return events.ToolValidationFailed(
                            call.call_id,
                            call.name,
                            decision.reason,
                            "request",
                            decision.retryable,
                            {"candidates": decision.candidates},
                            event.continuation,
                        )
                    elif not isinstance(decision, ValidationAccepted):
                        raise TypeError("validate_request must return a validation outcome")
                    else:
                        evidence = decision.evidence
                        request_validation_event = events.ToolValidationSucceeded(
                            call.call_id, call.name, "request", evidence
                        )
                if policy is not None and callable(
                    getattr(policy, "capture_pre_state", None)
                ):
                    captured = await policy.capture_pre_state(call, validation_context)
                    captured = Event(
                        "ToolPreStateCaptured", {"evidence": captured}
                    ).data["evidence"]
                    evidence = {
                        "validation": evidence,
                        "pre_state": captured,
                    }
                    request_validation_event = events.ToolValidationSucceeded(
                        call.call_id, call.name, "request", evidence
                    )
                result = await tool.execute(call.arguments, operation_id=event.operation_id)
                if policy is not None:
                    output_hook = getattr(policy, "validate_output", None)
                    if callable(output_hook):
                        decision = await output_hook(
                            call, result, evidence, validation_context
                        )
                    else:
                        decision = await policy.validate_result(
                            call, result, evidence, validation_context
                        )
                    if isinstance(decision, ValidationDecision):
                        if decision.status != "accept":
                            failure = events.ToolValidationFailed(
                                call.call_id,
                                call.name,
                                decision.message or decision.code or decision.status,
                                "result",
                                False,
                                {
                                    "status": decision.status,
                                    "code": decision.code,
                                    "evidence": decision.evidence,
                                },
                                event.continuation,
                            )
                            assert request_validation_event is not None
                            return request_validation_event, failure
                        result_evidence = {"value": decision.evidence}
                        if decision.normalized_value is not None:
                            result = decision.normalized_value
                    elif isinstance(decision, PostconditionFailed):
                        failure = events.ToolValidationFailed(
                            call.call_id,
                            call.name,
                            decision.reason,
                            "result",
                            False,
                            {},
                            event.continuation,
                        )
                        assert request_validation_event is not None
                        return request_validation_event, failure
                    elif not isinstance(decision, ValidationAccepted):
                        raise TypeError("validate_result must return a validation outcome")
                    else:
                        result_evidence = decision.evidence
            except (LookupError, ToolArgumentsRejected) as error:
                return events.ToolCallRejected(str(error))
            except ToolExecutionFailed as error:
                return events.ToolCallFailed(str(error))
            try:
                result = await apply_after_tool(
                    middleware,
                    ToolCallContext(
                        policy_name,
                        int(event.continuation["model_step"]),
                        int(event.continuation["tool_calls_used"]),
                        event.operation_id,
                    ),
                    result,
                )
            except MiddlewareExecutionError as error:
                return events.MiddlewareFailed(
                    error.middleware_id, error.phase, error.reason
                )
            succeeded = events.ToolCallSucceeded(
                call.call_id,
                call.name,
                result,
                event.continuation,
            )
            if request_validation_event is None:
                return succeeded
            return (
                request_validation_event,
                events.ToolValidationSucceeded(
                    call.call_id, call.name, "result", result_evidence
                ),
                succeeded,
            )

        @agent.react(events.ToolValidationFailed)
        def handle_validation_failure(state, event):
            del state
            if event.details.get("status") == "abstain":
                return events.RunAbstained(event.reason)
            if not event.retryable:
                return events.RunFailed(
                    f"tool validation failed in {event.phase}: {event.reason}"
                )
            continuation = event.continuation
            step = int(continuation["model_step"])
            if step >= limits.max_model_steps:
                return events.ModelLoopLimitExceeded("maximum model steps exceeded")
            request = ModelRequest.from_data(continuation["request"])
            assistant = ModelResponse.from_data(
                {"message": continuation["assistant_message"], "tool_calls": ()}
            ).message
            from .llm import tool_result_message

            feedback = tool_result_message(
                Event(
                    "ToolCallSucceeded",
                    {
                        "call_id": event.call_id,
                        "name": event.name,
                        "result": {
                            "validation": "rejected",
                            "phase": event.phase,
                            "reason": event.reason,
                            "details": event.details,
                        },
                    },
                )
            )
            next_request = ModelRequest(
                request.messages + (assistant, feedback),
                request.tools,
                request.model,
                request.artifacts,
                request.instruction,
            )
            return events.ModelCallRequested(
                next_request.to_data(),
                step + 1,
                int(continuation["tool_calls_used"]),
                control=continuation.get("control", {}),
            )

        @agent.react(events.ToolCallSucceeded)
        def continue_model(state, event):
            continuation = dict(event.continuation)
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
                request.messages + (assistant, tool_message),
                request.tools,
                request.model,
                request.artifacts,
                request.instruction,
            )
            try:
                next_request = apply_before_model(
                    middleware,
                    ModelCallContext(
                        policy_name,
                        step + 1,
                        int(continuation["tool_calls_used"]),
                    ),
                    next_request,
                )
                _validate_selected_tools(next_request.tools, installed_fingerprints)
            except MiddlewareExecutionError as error:
                return events.MiddlewareFailed(
                    error.middleware_id, error.phase, error.reason
                )
            control = _next_control(
                continuation.get("control", {}), snapshot_state, state
            )
            return events.ModelCallRequested(
                next_request.to_data(),
                step + 1,
                int(continuation["tool_calls_used"]),
                control=control,
            )

        for failure_type in (
            events.ModelCallRejected,
            events.ModelCallFailed,
            events.ModelOutputRejected,
            events.ToolCallRejected,
            events.ToolCallFailed,
            events.ModelLoopLimitExceeded,
            events.WorkflowInvariantViolated,
            events.GoalNotSatisfied,
            events.WorkflowCycleDetected,
            events.MiddlewareFailed,
            events.ArtifactResolutionFailed,
            events.InstructionResolutionFailed,
        ):
            agent.react(failure_type)(
                lambda state, event: events.RunFailed(str(event.reason))
            )
        agent.terminal(events.RunCompleted, status="completed")
        agent.terminal(events.RunFailed, status="failed")
        agent.terminal(events.RunAbstained, status="abstained")

def _validate_selected_tools(
    selected: tuple[ToolDefinition, ...], installed: Mapping[str, str]
) -> None:
    for definition in selected:
        if installed.get(definition.name) != tool_definition_fingerprint(definition):
            raise DefinitionError(
                f"build_request selected unknown or changed tool {definition.name!r}"
            )


def _plain_control_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_control_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_control_json(item) for item in value]
    return value


def _initial_control(
    snapshot_state: Callable[[Any], Any] | None, state: Any
) -> Mapping[str, Any]:
    if snapshot_state is None:
        return {}
    snapshot = _validated_snapshot(snapshot_state, state)
    return {
        "state_fingerprints": (_fingerprint_snapshot(snapshot),),
        "workflow_state": snapshot,
    }


def _next_control(
    control: Any,
    snapshot_state: Callable[[Any], Any] | None,
    state: Any,
) -> Mapping[str, Any]:
    if snapshot_state is None:
        return {}
    existing = control.get("state_fingerprints", ()) if isinstance(control, Mapping) else ()
    snapshot = _validated_snapshot(snapshot_state, state)
    return {
        "state_fingerprints": tuple(existing)
        + (_fingerprint_snapshot(snapshot),),
        "workflow_state": snapshot,
    }


def _validated_snapshot(snapshot_state: Callable[[Any], Any], state: Any) -> Any:
    return Event(
        "WorkflowStateSnapshotValidated", {"snapshot": snapshot_state(state)}
    ).data["snapshot"]


def _fingerprint_snapshot(snapshot: Any) -> str:
    return json.dumps(_plain_control_json(snapshot), sort_keys=True, separators=(",", ":"))


def _control_snapshot(continuation: Mapping[str, Any]) -> Any:
    control = continuation.get("control", {})
    if not isinstance(control, Mapping):
        return None
    return control.get("workflow_state")


def _cycle_failure(continuation: Mapping[str, Any], max_visits: int) -> str | None:
    control = continuation.get("control", {})
    if not isinstance(control, Mapping):
        return "invalid workflow control state"
    fingerprints = control.get("state_fingerprints", ())
    if not isinstance(fingerprints, tuple) or not fingerprints:
        return None
    current = fingerprints[-1]
    visits = sum(item == current for item in fingerprints)
    if visits > max_visits:
        return f"workflow state repeated {visits} times (limit {max_visits})"
    return None


def _invariant_failure(
    workflow_invariant: Callable[[Any], bool | str] | None, state: Any
) -> str | None:
    if workflow_invariant is None:
        return None
    outcome = workflow_invariant(state)
    if outcome is True:
        return None
    if isinstance(outcome, str) and outcome:
        return outcome
    return "workflow invariant is not satisfied"


def _validate_resource_contract(
    resources: object,
    *,
    policy_name: str,
    provider_key: str,
    tools_key: str,
    tool_policy_key: str | None,
    artifacts_key: str | None,
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
    if artifacts_key is not None:
        artifact_store = _resource(resources, artifacts_key, policy_name=policy_name)
        if not callable(getattr(artifact_store, "get_many", None)):
            raise DefinitionResourceMismatch(
                policy=policy_name,
                resource_key=artifacts_key,
                changed=("artifact_store",),
            )
    if tool_policy_key is not None:
        policy = _resource(resources, tool_policy_key, policy_name=policy_name)
        legacy = callable(getattr(policy, "validate_request", None)) and callable(
            getattr(policy, "validate_result", None)
        )
        execution = all(
            callable(getattr(policy, name, None))
            for name in ("validate_input", "validate_transition", "validate_output")
        )
        if not (legacy or execution):
            raise DefinitionResourceMismatch(
                policy=policy_name,
                resource_key=tool_policy_key,
                changed=("tool_policy",),
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
