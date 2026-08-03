from __future__ import annotations

from dataclasses import dataclass

from agentlog import (
    DurableDispatcher,
    DurableEffectDispatcher,
    Event,
    FunctionTool,
    InMemoryEventStore,
    ToolRegistry,
    ModelCallFailedError,
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolExecutionFailed,
    PostconditionFailed,
    ValidationAccepted,
    ValidationAmbiguous,
    ValidationRejected,
    run_stream_id,
)
from tests.test_model_loop_policy import define, get_weather, run


class ControlledProvider:
    def __init__(self) -> None:
        self.fail = False

    async def complete(self, request, *, operation_id):
        del operation_id
        if self.fail:
            raise ModelCallFailedError("injected model failure")
        if request.messages[-1].role == "tool":
            return ModelResponse(ModelMessage("assistant", "23 C"))
        return ModelResponse(
            ModelMessage("assistant", "Checking"),
            (ToolCall("weather-1", "get_weather", {"city": "Tbilisi"}),),
        )


@dataclass
class RuntimeHarness:
    store: InMemoryEventStore
    stream_id: str
    tools: ToolRegistry
    agent: object
    runtime: object
    reactions: DurableDispatcher
    effects: DurableEffectDispatcher
    provider: ControlledProvider
    tool_failure: list[bool]
    validation: bool = False
    validation_outcome: list[str] | None = None

    @classmethod
    def create(cls, *, validation: bool = False) -> "RuntimeHarness":
        store = InMemoryEventStore()
        stream_id = run_stream_id("assistant", "model-verification")
        tools = ToolRegistry.from_functions(get_weather)
        agent, _ = define(tools, tool_policy="policy" if validation else None)
        policy_outcome = ["accept"]

        class InitialPolicy:
            async def validate_request(self, call, context):
                return ValidationAccepted({})

            async def validate_result(self, call, result, evidence, context):
                return ValidationAccepted({})

        context = {"model": ControlledProvider(), "tools": tools}
        if validation:
            context["policy"] = InitialPolicy()
        runtime = agent.build_runtime(
            context=context
        )
        run(
            store.append(
                stream_id,
                -1,
                (
                    Event(
                        "RunCreated", {"agent": "assistant", "definition_version": "1"}
                    ),
                    agent.handle_command("message", {"text": "weather"})[0],
                ),
            )
        )
        harness = cls(
            store,
            stream_id,
            tools,
            agent,
            runtime,
            None,
            None,
            None,
            [False],
            validation,
            policy_outcome,
        )
        harness.restart()
        return harness

    def restart(self) -> None:
        definition = ToolRegistry.from_functions(get_weather).definitions()[0]
        failure = [False]

        async def controlled_weather(city: str):
            if failure[0]:
                raise ToolExecutionFailed("injected tool failure")
            return {"city": city, "temperature": 23}

        fresh_tools = ToolRegistry()
        fresh_tools.register(FunctionTool(definition, controlled_weather))
        provider = ControlledProvider()
        fresh_agent, _ = define(
            fresh_tools, tool_policy="policy" if self.validation else None
        )
        outcome = self.validation_outcome

        class ControlledValidationPolicy:
            async def validate_request(self, call, context):
                assert outcome is not None
                if outcome[0] == "reject":
                    return ValidationRejected("rejected by model policy")
                if outcome[0] == "retry":
                    return ValidationRejected("retry model decision", retryable=True)
                if outcome[0] == "ambiguous":
                    return ValidationAmbiguous(("candidate-a", "candidate-b"))
                return ValidationAccepted({"pre": True})

            async def validate_result(self, call, result, evidence, context):
                assert outcome is not None
                if outcome[0] == "postcondition_failure":
                    return PostconditionFailed("postcondition mismatch")
                return ValidationAccepted({"post": True})

        context = {"model": provider, "tools": fresh_tools}
        if self.validation:
            context["policy"] = ControlledValidationPolicy()
        fresh_runtime = fresh_agent.build_runtime(
            context=context
        )
        self.tools = fresh_tools
        self.agent = fresh_agent
        self.runtime = fresh_runtime
        self.provider = provider
        self.tool_failure = failure
        self.reactions = DurableDispatcher(
            agent=fresh_runtime.agent,
            store=self.store,
            subscription_name="assistant:1:reactions",
        )
        self.effects = DurableEffectDispatcher(
            agent=fresh_runtime.agent,
            store=self.store,
            effects=fresh_runtime.effects,
            context=fresh_runtime.context,
            subscription_name="assistant:1:effects",
        )

    def dispatch(self, action: str) -> None:
        if action == "reaction":
            run(self.reactions.run_once())
        elif action in {"effect", "effect_model_failure", "effect_tool_failure"} or action.startswith(
            "effect_validation_"
        ):
            self.provider.fail = action == "effect_model_failure"
            self.tool_failure[0] = action == "effect_tool_failure"
            if action.startswith("effect_validation_"):
                assert self.validation_outcome is not None
                self.validation_outcome[0] = action.removeprefix("effect_validation_")
            try:
                run(self.effects.run_once())
            finally:
                self.provider.fail = False
                self.tool_failure[0] = False
                if self.validation_outcome is not None:
                    self.validation_outcome[0] = "accept"
        elif action == "restart":
            self.restart()
        elif action == "force_terminal":
            history = self.history()
            if not self.runtime.agent.is_terminal(
                history, through_version=history[-1].stream_version
            ):
                run(
                    self.store.append(
                        self.stream_id,
                        history[-1].stream_version,
                        (Event("RunFailed", {"reason": "forced"}),),
                    )
                )
        else:
            raise ValueError(action)

    def history(self):
        return run(self.store.load(self.stream_id))

    def checkpoints(self) -> tuple[int, int]:
        return (
            run(self.store.load_checkpoint("assistant:1:reactions")),
            run(self.store.load_checkpoint("assistant:1:effects")),
        )
