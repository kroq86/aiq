# Executable model verification

AIQ 0.2 is checked against a small pure reference interpreter in
`tests/model/reference.py`. The interpreter does not import AIQ, SQLite,
FastAPI, Ollama, or runtime classes. It models an ordered history, the two
subscription checkpoints, terminal status, causal identity, and the durable
single-tool continuation.

This is executable specification and differential testing, not a theorem
prover or a proof of arbitrary user definitions.

## Model

For a versioned definition and history:

\[
Run_t=(\mathcal D_v,H_t)
\]

\[
s_t=\operatorname{fold}(R,s_0,H_t)
\]

The pure reaction and explicit effect boundaries are:

\[
F(s_t,e_t)\rightarrow I^{*}
\]

\[
\Gamma(I_k,W^{explicit})\rightarrow O_k
\]

The verification state is:

\[
M=(H,P_r,P_e,T)
\]

where `H` is normalized append-only history, `P_r` and `P_e` are reaction and
effect subscription checkpoints, and `T` is the terminal predicate derived
from history. Continuation and counters are event payload, not a separate
mutable component.

The reference interpreter accepts test-world actions:

```text
reaction
effect
effect_model_failure
effect_tool_failure
force_terminal
restart
```

The same action is applied to the reference state and a real
`AgentDefinition`/`DurableDispatcher`/`DurableEffectDispatcher` harness.

## Normalization

UUIDs, timestamps, and database positions are transport details. Runtime
history is normalized to sequential symbolic identities (`e1`, `e2`, ...),
while preserving:

- event type;
- relevant lifecycle payload and counters;
- causation relation;
- operation relation.

Consequently, equality means graph-and-payload equivalence rather than merely
an equal list of event names.

## Executable properties

| Property | Executable check |
| --- | --- |
| `state = fold(H)` | `test_model_loop_state_machine.state_is_fold_of_history` |
| append-only `H_t` | `reference.assert_invariants` and `test_oracle_rejects_history_rewrite_and_checkpoint_rollback` |
| monotonic checkpoints | `runtime_matches_reference` after every generated action |
| restart bisimulation | `test_restart_after_every_dispatch_boundary_is_bisimilar` |
| one committed result per request | `test_request_result_relations_are_unique_and_causal` |
| causal result/request relation | normalized `causation` and `operation` equality |
| terminal uniqueness | `reference.assert_invariants` |
| terminal cutoff with pending work | generated `force_terminal` followed by effect/reaction actions |
| committed tool is not repeated after restart | `test_restart_after_committed_tool_result_resumes_without_reexecution` |
| stable operation identity across failed commit | `test_result_and_checkpoint_roll_back_then_retry_with_same_operation_id` |
| at-least-once retry | `test_uncommitted_effect_failure_is_retried_at_least_once` |
| definition/resource drift | model-loop policy startup and persisted-drift tests |
| policy object is not runtime state | `test_compiled_runtime_handlers_do_not_retain_policy_instance` |
| HTTP is a projection of core semantics | `test_http_is_a_projection_of_direct_runtime_semantics` |

The Hypothesis state machine generates arbitrary orderings of reaction/effect
dispatch, expected failures, forced terminal events, and runtime recreation.
It compares normalized history, checkpoints, and folded state after every
action rather than only at the end of a run.

## FastAPI equivalence

The same deterministic definition is executed directly and through
`AIQApplication`:

\[
Normalize(H_{direct})=Normalize(H_{http})
\]

The test additionally proves that SSE order equals history order, reconnect
from `Last-Event-ID` does not mutate history, the HTTP state is the same fold,
and trace edge count equals normalized causation relations.

## Mutation boundary

`test_invariants.py` mutates normalized facts and proves that the invariant
oracle rejects:

- changed operation/causation relation;
- duplicate committed result;
- duplicate terminal event;
- rewritten history prefix;
- checkpoint rollback.

This validates the oracle. It is **not** source mutation testing of production
code. A CI mutation job (for example, mutmut or an equivalent tool) is not
configured yet, so AIQ does not claim a mutation score. The source-level
mutation matrix to add is:

```text
remove terminal cutoff
advance checkpoint before commit
drop/replace causation_id
drop tool result from continuation
repeat committed tool after restart
exclude tool definitions from drift signature
allow changed executable registry
reset model/tool counters after restart
```

Each mutation must be killed by the property named in the table above before
source mutation coverage can be called complete.

## Scope and limits

The reference definition covers the 0.2 single-tool policy: first model call
may select one tool; a subsequent model call produces an answer. Expected
model/tool failures and forced terminal races are generated. It does not model
multiple-tool joins, streaming providers, child runs, or arbitrary user
reducers.

Agent definition version remains explicit and user-owned. AIQ validates
tool-definition/resource drift but does not automatically derive the complete
`definition_version` from Python code.

Run the executable model suite:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/model -t . -v
```

## Standalone local bounded models

Three optional v0.4 controls are checked in small pure-Python finite models that
do not require `setdb`:

- `formal/cycle_guard/`: repeated-state guard, 30 reachable states, two killed
  targeted mutants;
- `formal/completion_gate/`: independent invariant/goal configuration axes,
  15 reachable states, all material gate events witnessed, five killed
  targeted mutants;
- `formal/run_abstained/`: request/result validation-failure routing,
  8 reachable states, both terminal outcomes witnessed, five killed targeted
  mutants;
- `formal/lease_gate/`: two-worker SQLite ownership, confirmation, expiry,
  takeover, and fenced commit, 20,361 states within bound 8, six bounded
  semantic mutants, plus 12 killed runtime source mutants with SHA-256
  restoration.

These are local bounded-exhaustive checks. They do not modify the trace
reference interpreter, prove arbitrary Python predicates, or establish
composition with the base lifecycle.

## Bounded exhaustive exploration with setdb

`formal/setdb/check_aiq_model.py` exhaustively explores every reachable
state of the complete finite reference policy and records its state graph in
`kroq86/setdb`. The normal model must have an empty `violations` set; an
intentional duplicate-terminal mutation must produce a shortest counterexample.

This closes the gap between randomly generated traces and exhaustive coverage
of this finite policy model. It does not prove arbitrary Python definitions,
external systems, or concurrent storage implementations. Build instructions are in
`formal/setdb/README.md`.
