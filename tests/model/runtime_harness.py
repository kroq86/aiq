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

    @classmethod
    def create(cls) -> "RuntimeHarness":
        store = InMemoryEventStore()
        stream_id = run_stream_id("assistant", "model-verification")
        tools = ToolRegistry.from_functions(get_weather)
        agent, _ = define(tools)
        runtime = agent.build_runtime(
            context={"model": ControlledProvider(), "tools": tools}
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
            store, stream_id, tools, agent, runtime, None, None, None, [False]
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
        fresh_agent, _ = define(fresh_tools)
        fresh_runtime = fresh_agent.build_runtime(
            context={"model": provider, "tools": fresh_tools}
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
        elif action in {"effect", "effect_model_failure", "effect_tool_failure"}:
            self.provider.fail = action == "effect_model_failure"
            self.tool_failure[0] = action == "effect_tool_failure"
            try:
                run(self.effects.run_once())
            finally:
                self.provider.fail = False
                self.tool_failure[0] = False
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
