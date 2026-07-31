from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .core import Event, JsonValue
from .runtime import AgentDefinition, EffectContext, EffectRegistry


def default_serialize_state(state: Any) -> JsonValue:
    if dataclasses.is_dataclass(state) and not isinstance(state, type):
        return {
            field.name: default_serialize_state(getattr(state, field.name))
            for field in dataclasses.fields(state)
        }
    if isinstance(state, tuple):
        return [default_serialize_state(item) for item in state]
    if isinstance(state, Mapping):
        return {key: default_serialize_state(item) for key, item in state.items()}
    return state


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    agent: AgentDefinition[Any]
    effects: EffectRegistry[Any]
    context: EffectContext
    serialize_state: Callable[[Any], JsonValue] = default_serialize_state
    command_handler: Callable[[str, Any], Sequence[Event]] | None = None
    definition_version: str | None = None


__all__ = ["AgentRuntime", "default_serialize_state"]
