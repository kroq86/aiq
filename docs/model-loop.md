# Durable model loop

`DurableModelLoop` is a composable declarative policy for an ordinary
`Agent`. It installs standard event types, reactions, and effects; it does not
create another executor or keep mutable session state.

```python
tools = ToolRegistry.from_functions(get_weather)

loop = DurableModelLoop(
    start_on=UserMessageAdded,
    build_request=build_request,
    tool_definitions=tools.definitions(),
    provider="ollama",
    tools="default",
    limits=ModelLoopLimits(max_model_steps=8, max_tool_calls=8),
)
loop.install(agent)

application.register(
    agent,
    resources={"ollama": OllamaProvider(model="llama3.2:1b"), "default": tools},
)
```

## Definition and resources

The versioned definition captures immutable `ToolDefinition` values. The
registration-specific resources contain executable providers and tools:

```text
D_v: lifecycle rules, limits, resource keys, tool definitions
W:   ModelProvider, ToolRegistry, HTTP/MCP clients
```

Registration fails with `DefinitionResourceMismatch` if registry definitions
differ by name, description, or canonical input schema. Each tool effect also
compares the persisted definition seen by the model with the currently
resolved executable tool before execution. Runtime drift is a configuration
failure and makes the worker unhealthy; the unsafe tool is not invoked.

## Event-carried continuation

The initial pure reaction calls:

```python
build_request(state, start_event, installed_tool_definitions)
```

`ModelCallRequested` stores the resulting immutable `ModelRequest`. If the
model selects one tool, `ToolCallRequested` stores its call, expected
definition, base request, assistant message, and counters. A committed tool
result therefore contains everything needed to deterministically produce the
next model request after restart.

The 0.2 policy accepts zero or one tool call per model response. Multiple tool
calls require explicit join semantics and are rejected. Model/tool limits are
persisted through lifecycle events; there is no hidden `while` loop.

`build_request` must be synchronous and perform no I/O. Raising
`ModelCallRejectedError` records an expected rejection. Other exceptions are
definition bugs and fail the worker.

## External execution guarantee

Effects remain at-least-once. A crash after an external provider/tool call but
before its result commit may repeat the physical call with the same stable
`operation_id`. Exactly-once behavior requires provider-side idempotency.

The mathematical transition system is checked against a pure reference
interpreter; see [executable model verification](model-verification.md).
