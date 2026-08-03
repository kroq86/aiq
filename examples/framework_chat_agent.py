"""Reference chat agent, declared through `aiq.framework.Agent`
instead of hand-assembled `AgentDefinition`/`EffectRegistry`/`EffectContext`.

    PYTHONPATH=src python3 examples/framework_chat_agent.py

Drives one run through the *existing* core (`DurableDispatcher`,
`DurableEffectDispatcher`, `InMemoryEventStore`) -- `Agent.build_runtime()`
only builds the same `AgentRuntime` a hand-assembled agent would use;
nothing here is a second execution path. No real LLM/MCP: the "model" is
an in-process fake.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from aiq import DurableDispatcher, DurableEffectDispatcher, InMemoryEventStore, run_stream_id
from aiq.framework import Agent


@dataclass(frozen=True)
class ChatState:
    messages: tuple[str, ...] = ()
    answer: str | None = None
    completed: bool = False


agent = Agent(name="framework-assistant", initial_state=ChatState())


# --- domain events (5) -----------------------------------------------


@agent.event
@dataclass(frozen=True)
class UserMessageAdded:
    text: str


@agent.event
@dataclass(frozen=True)
class ModelCallRequested:
    prompt: str
    # Transport field: left with a default so a reaction can construct this
    # without supplying it -- the framework fills the real value in from the
    # durable effect request's own identity once the event actually exists.
    operation_id: str = ""


@agent.event
@dataclass(frozen=True)
class ModelCallSucceeded:
    text: str
    operation_id: str = ""


@agent.event
@dataclass(frozen=True)
class AnswerProduced:
    text: str


@agent.event
@dataclass(frozen=True)
class RunCompleted:
    pass


# --- reducers ----------------------------------------------------------


@agent.reduce(UserMessageAdded)
def on_user_message(state: ChatState, event: UserMessageAdded) -> ChatState:
    return replace(state, messages=state.messages + (event.text,))


@agent.reduce(AnswerProduced)
def on_answer(state: ChatState, event: AnswerProduced) -> ChatState:
    return replace(state, answer=event.text)


@agent.reduce(RunCompleted)
def on_completed(state: ChatState, event: RunCompleted) -> ChatState:
    return replace(state, completed=True)


# ModelCallRequested/ModelCallSucceeded have no reducer: state is simply
# left unchanged when one arrives, per the framework's dispatch contract.


# --- reactions -----------------------------------------------------------


@agent.react(UserMessageAdded)
def request_model(state: ChatState, event: UserMessageAdded):
    return ModelCallRequested(prompt=event.text)


@agent.react(ModelCallSucceeded)
def finish_run(state: ChatState, event: ModelCallSucceeded):
    # A reaction may return a sequence of events, not just zero-or-one.
    return (AnswerProduced(text=event.text), RunCompleted())


# --- durable effect: fake model, no network -----------------------------


class FakeModel:
    async def complete(self, prompt: str) -> str:
        return f"answer to: {prompt!r}"


class Context:
    def __init__(self) -> None:
        self.model = FakeModel()


@agent.effect(ModelCallRequested)
async def call_model(effect: ModelCallRequested, context: Context) -> ModelCallSucceeded:
    result = await context.model.complete(effect.prompt)
    return ModelCallSucceeded(text=result, operation_id=effect.operation_id)


# --- command: how a host turns an incoming request into the first event ---


@agent.command("send_message")
def send_message(payload: dict) -> UserMessageAdded:
    return UserMessageAdded(text=payload["text"])


async def main() -> None:
    runtime = agent.build_runtime(context=Context())

    store = InMemoryEventStore()
    stream_id = run_stream_id(agent.name, "framework-demo-run")

    initial_events = agent.handle_command("send_message", {"text": "Show me the weather"})
    await store.append(stream_id, -1, list(initial_events))

    reactions = DurableDispatcher(
        agent=runtime.agent, store=store, subscription_name=f"{agent.name}:reactions"
    )
    effects = DurableEffectDispatcher(
        agent=runtime.agent,
        store=store,
        effects=runtime.effects,
        context=runtime.context,
        subscription_name=f"{agent.name}:effects",
    )

    for _ in range(20):
        reaction_progress = await reactions.run_once()
        effect_progress = await effects.run_once()
        if not reaction_progress and not effect_progress:
            break
    else:
        raise RuntimeError("workers did not reach a stable checkpoint")

    history = await store.load(stream_id)
    final_state = runtime.agent.rebuild(history)

    print("Event types:", [envelope.event.event_type for envelope in history])
    print("Final state:", final_state)


if __name__ == "__main__":
    asyncio.run(main())
