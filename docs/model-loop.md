# Durable model loop

`DurableModelLoop` is a composable declarative policy for an ordinary
`Agent`. It installs standard event types, reactions, and effects; it does not
create another executor or keep mutable session state.

`ModelLoopEvents` remains importable for compatibility, but its constructor is
an internal implementation detail and is not covered by public compatibility
guarantees. Obtain the lifecycle event types from `DurableModelLoop.events`;
do not construct a partial `ModelLoopEvents` value directly.

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

`OllamaProvider(..., think=None)` leaves Ollama's thinking setting unspecified.
Pass `think=True` or `think=False` to send that exact boolean in the chat API
request. The default preserves the behavior of existing callers.

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

The 0.3 policy accepts zero or one tool call per model response. Multiple tool
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

## Domain tool validation

`DurableModelLoop` can enforce an application-owned semantic policy around a
tool call. The runtime owns when the hooks run, durable outcomes, evidence,
and retry feedback; the application owns relevance predicates, thresholds,
candidate sets, freshness rules, and postconditions.

```python
loop = DurableModelLoop(
    # ...
    tool_policy="calendar_policy",
)

application.register(
    agent,
    resources={
        # ...
        "calendar_policy": MoveEventPolicy(),
    },
)
```

`validate_request` returns `ValidationAccepted(evidence)`,
`ValidationRejected(reason, retryable=...)`, or
`ValidationAmbiguous(candidates)`. Accepted evidence is passed unchanged to
`validate_result`, which returns `ValidationAccepted(evidence)` or
`PostconditionFailed(reason)`.

A retryable request rejection is committed and returned to the model as tool
feedback so it can propose a new call. A non-retryable rejection fails the
run. A postcondition failure is always non-retryable: the external side effect
may already have happened, so AIQ does not invoke it again implicitly.
Provider-side idempotency remains necessary for crash retries of effects.

This seam does not establish relevance by itself. Without an application
policy, AIQ still validates only the structural tool contract.

### Validation is not planning

The policy answers one local question:

```text
Is this proposed transition allowed, and did its local postconditions hold?
```

It does not answer either of these global questions:

```text
Which allowed transition should be selected next?
Has the user's complete goal been achieved?
```

These are three separate contracts:

```text
transition validation  application predicates around one proposed tool call
planning / control      application strategy for choosing the next action
goal verification      application predicate over the resulting workflow state
```

Locally accepted calls do not imply a globally correct trajectory. An agent
can execute several structurally valid calls whose guards and postconditions
all pass while making no progress toward the user's goal. `ModelLoopLimits`
bounds model steps and tool calls, which is a budget, not planning or proof
of goal completion.

AIQ currently provides durable proposals, local validation hooks,
execution, observations, replay, and execution budgets. On their own these do
not provide a generic goal representation or an action-selection algorithm.
The v0.4 candidate below adds an opt-in, application-supplied JSON workflow
snapshot, a bounded repeated-state guard, and a single boolean goal
predicate gated before completion — a specific, narrow mechanism, not a
general workflow-state model, progress measure, or planner. Applications that
need more than a visit-count cycle guard or a single goal predicate must
still supply their own state, action contracts, and completion criteria. The
runtime cannot reconstruct that richer semantics from JSON, messages, or
validation evidence.

In short, tool validation turns unconstrained proposals into constrained
execution. It does not turn model output into correct planning.

## v0.4 candidate: constrained multistep control

The opt-in execution contract adds application-owned workflow state and
completion gates:

```python
loop = DurableModelLoop(
    # ...
    tool_policy="execution_policy",
    snapshot_state=lambda state: state.to_data(),
    workflow_invariant=lambda state: state.invariant_ok,
    goal_satisfied=lambda state: state.goal_complete,
    limits=ModelLoopLimits(max_state_visits=2),
)
```

An `ExecutionPolicy` separates three tool boundaries:

```text
validate_input      validate and optionally normalize external/model input
validate_transition guard the proposed call against the workflow snapshot
validate_output     verify and optionally normalize the tool observation
```

`capture_pre_state` is optional. Its JSON evidence is committed with request
validation and passed unchanged to `validate_output`. Every hook receives a
`ToolValidationContext` containing stable operation identity, counters, and
the application-produced JSON workflow snapshot.

Hooks return one `ValidationDecision` status:

```text
accept   continue and optionally use normalized_value
reject   fail the proposed transition
retry    ask the model for another proposal; do not repeat an executed tool
replan   ask the model for a different next step
abstain  terminate as RunAbstained
fail     terminate as RunFailed
```

Request-side `retry` and `replan` share a safe control transition back to the
model while retaining distinct durable status evidence. Result-side failures
are never automatically retried because the external effect may have occurred.

When `goal_satisfied` is configured, `RunCompleted` is emitted only after a
durable `GoalSatisfied` event. A false result emits `GoalNotSatisfied` and
fails the run. `workflow_invariant` is checked at the same completion boundary.
State snapshots are canonicalized and counted; exceeding `max_state_visits`
emits `WorkflowCycleDetected` before another tool is executed.

These controls constrain execution against application rules. They do not
select an optimal action or prove planning correctness.
