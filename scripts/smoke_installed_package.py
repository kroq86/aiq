"""Smoke-test an installed Agentlog wheel through public APIs only."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from agentlog import (
    Agent,
    DurableDispatcher,
    DurableEffectDispatcher,
    DurableModelLoop,
    ExecutionPolicy,
    InMemoryEventStore,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolPolicy,
    ToolRegistry,
    ValidationAccepted,
    ValidationDecision,
    run_stream_id,
)


@dataclass(frozen=True)
class WorkflowState:
    tool_succeeded: bool = False


class ToolThenAnswerProvider:
    async def complete(self, request, *, operation_id):
        del operation_id
        if request.messages[-1].role == "tool":
            return ModelResponse(ModelMessage("assistant", "done"))
        return ModelResponse(
            ModelMessage("assistant", "calling tool"),
            (ToolCall("echo-1", "echo", {"value": "ok"}),),
        )


class LegacyPolicy:
    async def validate_request(self, call, context):
        return ValidationAccepted({"contract": "legacy"})

    async def validate_result(self, call, result, evidence, context):
        return ValidationAccepted({"result_checked": True})


class AcceptingExecutionPolicy:
    async def validate_input(self, call, context):
        return ValidationDecision("accept", evidence={"input_checked": True})

    async def validate_transition(self, call, context):
        return ValidationDecision("accept", evidence={"transition_checked": True})

    async def capture_pre_state(self, call, context):
        return {"workflow_state": context.workflow_state}

    async def validate_output(self, call, result, evidence, context):
        return ValidationDecision("accept", evidence={"output_checked": True})


class AbstainingExecutionPolicy(AcceptingExecutionPolicy):
    async def validate_transition(self, call, context):
        return ValidationDecision("abstain", code="insufficient_evidence")


def build_agent(name, tracker, policy, *, goal_gated):
    def echo(value: str) -> dict:
        tracker.append(value)
        return {"value": value}

    tools = ToolRegistry.from_functions(echo)
    agent = Agent(name=name, version="wheel-smoke", initial_state=WorkflowState)

    @agent.event
    @dataclass(frozen=True)
    class UserRequestAdded:
        text: str

    loop = DurableModelLoop(
        start_on=UserRequestAdded,
        build_request=lambda state, event, definitions: ModelRequest(
            (ModelMessage("user", event.text),), definitions
        ),
        tool_definitions=tools.definitions(),
        provider="model",
        tools="tools",
        tool_policy="policy",
        snapshot_state=lambda state: {"tool_succeeded": state.tool_succeeded},
        goal_satisfied=(lambda state: state.tool_succeeded) if goal_gated else None,
    )
    loop.install(agent)

    @agent.reduce(loop.events.ToolCallSucceeded)
    def record_tool_success(state, event):
        return replace(state, tool_succeeded=True)

    @agent.command("request")
    def request(payload):
        return UserRequestAdded(str(payload["text"]))

    runtime = agent.build_runtime(
        context={"model": ToolThenAnswerProvider(), "tools": tools, "policy": policy}
    )
    return agent, loop, runtime


async def execute(name, policy, *, goal_gated):
    tracker = []
    agent, loop, runtime = build_agent(name, tracker, policy, goal_gated=goal_gated)
    store = InMemoryEventStore()
    stream_id = run_stream_id(name, "installed-wheel-smoke")
    await store.append(stream_id, -1, agent.handle_command("request", {"text": "run"}))

    reactions = DurableDispatcher(
        agent=runtime.agent,
        store=store,
        subscription_name=f"{name}:reactions",
    )
    effects = DurableEffectDispatcher(
        agent=runtime.agent,
        store=store,
        effects=runtime.effects,
        context=runtime.context,
        subscription_name=f"{name}:effects",
    )
    for _ in range(30):
        await reactions.run_once()
        await effects.run_once()
        history = await store.load(stream_id)
        final_type = history[-1].event.event_type
        if final_type in {"RunCompleted", "RunFailed", "RunAbstained"}:
            status = runtime.agent.terminal_status_by_event_type[final_type]
            return tuple(item.event.event_type for item in history), tuple(tracker), status
    raise AssertionError(f"{name} did not terminate")


async def main():
    legacy_policy: ToolPolicy = LegacyPolicy()
    legacy_types, legacy_calls, legacy_status = await execute(
        "legacy-policy-smoke", legacy_policy, goal_gated=False
    )
    assert legacy_calls == ("ok",)
    assert legacy_types[-1] == "RunCompleted"
    assert legacy_status == "completed"

    execution_policy: ExecutionPolicy = AcceptingExecutionPolicy()
    execution_types, execution_calls, execution_status = await execute(
        "execution-policy-smoke", execution_policy, goal_gated=True
    )
    assert execution_calls == ("ok",)
    assert "GoalSatisfied" in execution_types
    assert execution_types.index("GoalSatisfied") < execution_types.index("RunCompleted")
    assert execution_status == "completed"

    abstain_types, abstain_calls, abstain_status = await execute(
        "abstain-policy-smoke", AbstainingExecutionPolicy(), goal_gated=True
    )
    assert abstain_calls == ()
    assert abstain_types[-1] == "RunAbstained"
    assert "RunCompleted" not in abstain_types
    assert abstain_status == "abstained"

    print("legacy ToolPolicy: ok")
    print("ExecutionPolicy goal gate: ok")
    print("RunAbstained terminal: ok")


if __name__ == "__main__":
    asyncio.run(main())
