# AIQ 0.2: formal safety proof record

The complete current model and its decomposition are specified in
`formal/FORMAL_MODEL.md`. This file is the machine-checked result record and
reproduction guide.

## 1. Claim

This document records the machine-checked AIQ 0.2 safety results: the
single-run trace model in `formal/setdb`, its runtime-refinement evidence, the
parameterized ModelLoopModel abstraction, and the dispatcher crash-window
safety abstraction.

Let `Reachable_10` be the states reachable from `Initial` by repeatedly applying
the formal transition relation while limiting event-history length to ten. Let
`SafeState` contain the state-level invariants and `SafeEdge` the invariants over
a parent/child transition. The checked result is:

\[
\forall s\in Reachable_{10}: SafeState(s)
\]

and:

\[
\forall (s,s')\in Transition,
s\in Reachable_{10}: SafeEdge(s,s')
\]

For the checked model:

```text
reachable states: 463
transitions:       1270
history bound:     10
safety invariants: 8
violations:        0
mutants killed:    8/8
```

This is an exhaustive bounded proof of the formal model. It is not an
unbounded proof for arbitrary policy limits and is not a proof of arbitrary
Python programs or external systems.

The fixed-limit explorer additionally reaches a closed graph at history length
12:

```text
reachable states: 552
transitions:       1509
base capacity:     12
guard capacity:    14
maximum append:    2 events
violations:        0
```

The normalized states and transitions produced at capacities 12 and 14 are
identical. Since every action appends at most two events, a successor hidden by
the capacity at 12 would necessarily appear at capacity 14. No such successor
exists. Therefore the 552-state graph is closed under `Next` for the current
fixed event-carried loop-counter semantics. This removes the history-length
bound for that specific configuration; it does not quantify over arbitrary
`max_model_steps` or `max_tool_calls` values.

## 2. Model boundary

The model contains one run, one immutable agent-definition identity, one
resource-fingerprint identity, and two subscriptions:

```text
reaction
effect
```

Event positions are 1-based. Position zero represents no event, no causation,
no operation, or an initial subscription checkpoint.

The model intentionally abstracts away:

- real UUID values;
- timestamps;
- prompts and answer text;
- concrete model/provider implementations;
- concrete tool arguments and results;
- HTTP and SSE transport details;
- SQLite page, connection, and process internals.

These values do not affect the checked safety properties. Model output is
reduced to finite observations such as answer, tool call, and failure.

## 3. State

The mathematical state is:

\[
M=(H,C_r,C_e,Status,Definition,Resource,I_m,I_t)
\]

where:

- `H` is the ordered event history;
- `C_r` is the reaction checkpoint;
- `C_e` is the effect checkpoint;
- `Status` is none, active, completed, or failed;
- `Definition` is the fixed definition identity;
- `Resource` is the resolved resource fingerprint identity;
- `I_m` counts modeled model invocations;
- `I_t` counts modeled tool invocations.

At the runtime-refinement bound of ten, the executable representation is a
packed 48-byte value:

```text
byte 0: history length
byte 1: reaction checkpoint
byte 2: effect checkpoint
byte 3: status
byte 4: definition identity
byte 5: model invocation count
byte 6: tool invocation count
byte 7: resource fingerprint identity

bytes 8..47: ten events, four bytes each
    event type
    causation position
    operation position
    finite flags
```

Every byte participates in structural state equality. Padding is not used.

The implementation is defined by:

- `formal/setdb/model_state.inc`;
- `formal/setdb/model_memory.inc`.

## 4. Events

The finite event vocabulary is:

```text
RunCreated
Start
ModelRequested
ModelSucceeded(answer)
ModelSucceeded(tool)
ModelFailed
ToolRequested
ToolSucceeded
ToolFailed
AnswerProduced
RunCompleted
RunFailed
ModelLoopLimitExceeded
```

An event is represented as:

\[
e=(type,cause,operation,flags)
\]

Its identity and position are the 1-based index in history. Consequently,
event IDs remain unique and positions remain ordered by construction.

Request types are:

```text
ModelRequested
ToolRequested
```

Result types are:

```text
ModelSucceeded(answer)
ModelSucceeded(tool)
ModelFailed
ToolSucceeded
ToolFailed
```

Terminal types are:

```text
RunCompleted
RunFailed
```

## 5. Initial state

`Initial` is the all-zero packed state:

\[
H=[]
\]

\[
C_r=C_e=0
\]

\[
Status=None
\]

\[
Definition=Resource=0
\]

The first valid transition is `CreateRun`, which appends `RunCreated`, fixes
definition/resource identity, and sets status active.

## 6. Transition relation

Exactly eleven actions define `Next`:

```text
CreateRun
AppendStart
Reaction
ModelAnswer
ModelTool
ModelFailure
ToolSuccess
ToolFailure
ForceComplete
ForceFail
Restart
```

The executable transition function is `apply_action(parent, action, child)` in
`formal/setdb/model_actions.inc`. It returns valid or invalid; invalid actions
do not create edges.

### 6.1 Reactions

A reaction consumes the next global event after `C_r`. It advances `C_r` to
the consumed position and may atomically append outputs.

The principal rules are:

```text
Start                  -> ModelRequested
ModelSucceeded(tool)   -> ToolRequested
ToolSucceeded          -> ModelRequested
failure or limit       -> RunFailed
```

For an answer, the reaction atomically appends a two-event batch:

```text
ModelSucceeded(answer)
├── AnswerProduced
└── RunCompleted
```

Both outputs have `ModelSucceeded` as their immediate cause:

\[
cause(AnswerProduced)=id(ModelSucceeded)
\]

\[
cause(RunCompleted)=id(ModelSucceeded)
\]

History order inside the batch does not create a causal relationship between
the sibling outputs. There is no reachable intermediate state containing the
answer without the terminal event.

### 6.2 Effects

An effect consumes the next global event after `C_e`. Non-request events only
advance the checkpoint. A matching request performs one modeled invocation and
atomically commits one result with the checkpoint advance:

```text
ModelRequested -> ModelSucceeded(answer)
ModelRequested -> ModelSucceeded(tool)
ModelRequested -> ModelFailed
ToolRequested  -> ToolSucceeded
ToolRequested  -> ToolFailed
```

For every result `o` produced from request `q`:

\[
cause(o)=id(q)
\]

and:

\[
operation(o)=operation(q)=id(q)
\]

### 6.3 Forced terminal

The normal terminal guard requires that the definition exists and no terminal
event is already present. It appends either `RunCompleted` or `RunFailed`.

### 6.4 Restart

Restart is an identity transition over the modeled durable state:

\[
Restart(H,C,Definition,Status)=(H,C,Definition,Status)
\]

It therefore produces a self-loop in `Transition`.

## 7. Bounded graph construction

`formal/setdb/model_bfs.inc` performs exhaustive breadth-first exploration:

```text
insert Initial
while an unexpanded state exists:
    for every action:
        child = apply_action(parent, action)
        reject invalid action
        reject append beyond history bound
        structurally deduplicate child
        record Transition(parent, child)
        record first Parent/ParentAction witness
```

Capacity failures are errors, not successful proof results:

```text
maximum states:      8192
maximum transitions: 65536
```

The successful graph remains below both capacities. All generated normal-model
states are reachable:

```text
States    = 463
Reachable = 463
```

For the saturated fixed-limit graph:

```text
States    = 552
Reachable = 552
Transition = 1509
```

FASM emits `SADD`/`RADD` facts. `setdb` loads them and computes reachability
from `Initial` over `Transition`. FASM also emits an exact 48-byte encoding for
each state as a sidecar used by runtime refinement; this sidecar does not define
reachability.

## 8. Safety invariants

The common checker in `formal/setdb/model_invariants.inc` derives violations
from raw packed states and parent/child edges. States are never manually marked
safe.

### 8.1 HistoryAppendOnly

For every transition:

\[
H_{parent}\preceq H_{child}
\]

The checker requires child history to be at least as long as parent history and
compares every byte of the parent event prefix.

### 8.2 CheckpointMonotonic

For every transition:

\[
C'_r\ge C_r
\]

\[
C'_e\ge C_e
\]

and:

\[
C'_r,C'_e\le |H'|
\]

### 8.3 ResultHasRequest

Every result has a nonzero cause referring to an earlier compatible request:

\[
cause(result)<position(result)
\]

Model results require `ModelRequested`; tool results require `ToolRequested`.

### 8.4 ResultOperationMatchesRequest

For every committed result:

\[
operation(result)=operation(request)
\]

### 8.5 AtMostOneResultPerRequest

For every request `q`:

\[
|\{o\mid cause(o)=id(q)\land type(o)\in Result\}|\le1
\]

### 8.6 AtMostOneTerminal

For every history:

\[
|\{e\in H\mid type(e)\in Terminal\}|\le1
\]

### 8.7 TerminalIsAbsorbing

If parent history already contains a terminal event, child history must be
identical. Checkpoint-only progress and restart self-loops remain allowed.

### 8.8 DefinitionResourceConsistent

Every modeled result requires the fixed definition and resource fingerprint to
match. Resource equality is a `CreateRun` precondition in the normal model;
resource drift is not modeled as an ordinary successful execution transition.

## 9. Violation computation

FASM derives these sets:

```text
BadHistoryAppendOnly
BadCheckpointMonotonic
BadResultHasRequest
BadResultOperationMatchesRequest
BadAtMostOneResultPerRequest
BadAtMostOneTerminal
BadTerminalIsAbsorbing
BadDefinitionResourceConsistent
```

For every property `P`, the verifier computes with setdb:

\[
Violation_P=Reachable\cap Bad_P
\]

and:

\[
Violations=\bigcup_P Violation_P
\]

The normal result is:

\[
Violations=\varnothing
\]

## 10. Non-vacuity and mutation sensitivity

The proof harness checks each predicate with a distinct compile-time mutation
of transition semantics:

| Invariant | Mutation |
| --- | --- |
| HistoryAppendOnly | restart rewrites an existing event |
| CheckpointMonotonic | restart rolls a checkpoint backward |
| ResultHasRequest | result loses request causation |
| ResultOperationMatchesRequest | result operation identity changes |
| AtMostOneResultPerRequest | an effect commits a second result |
| AtMostOneTerminal | terminal guard permits a second terminal |
| TerminalIsAbsorbing | restart appends meaningful post-terminal output |
| DefinitionResourceConsistent | execution uses a mismatched resource identity |

Each mutant uses the same BFS and invariant checker as the normal model. A
mutant does not insert its target state into a `Bad*` set manually.

The acceptance requirement for each mutant is:

```text
exit code = 1
exact expected property is reported
at least one reachable violation exists
a concrete violating edge and counterexample path are printed
```

Compile errors and capacity failures do not count as killed mutants.

The result is:

```text
MUTATION_MATRIX_PASS mutants=8
```

This establishes that every checked predicate is capable of detecting its
target class of transition violation. It does not prove that the predicates
cover every conceivable defect.

## 11. Runtime abstraction and refinement evidence

The formal proof above concerns the model. A separate executable layer checks
observed AIQ runtime traces against the proved graph.

The abstraction function is:

\[
\alpha:RuntimeSnapshot\rightarrow FormalState
\]

It maps:

```text
runtime history       -> formal event history
subscription offsets  -> reaction/effect checkpoints
RunCompleted/RunFailed -> formal status
definition version    -> formal definition identity
resolved resources    -> formal resource identity
UUID causation        -> normalized 1-based cause position
UUID operation ID     -> normalized 1-based operation position
committed results     -> modeled committed invocation counters
```

Python implements only extraction and encoding. It contains no implementation
of formal `Next`. FASM supplies the state encodings; setdb supplies
`Reachable` and `Transition`.

For every observed direct-runtime boundary:

\[
\alpha(r)\in Reachable
\]

and:

\[
(\alpha(r),\alpha(r'))\in Transition
\]

The checked evidence is:

```text
direct scenarios: 4
FastAPI runs:      1
snapshots:         122
formal states:     463
reachable states:  463
```

Direct scenarios cover:

- successful model/tool continuation;
- restart after every dispatch boundary;
- committed model failure;
- committed tool failure;
- forced terminal behavior.

The FastAPI scenario checks that its completed history and application
checkpoints map to an exact reachable formal state. Existing transport tests
separately check SSE order, state projection, and causal trace edges.

This is scenario-based refinement evidence. It is not a universal proof that
all executions of the Python runtime refine the formal model.

## 12. Reproduction

Prerequisites:

```bash
brew tap kroq86/fasm-mac https://github.com/kroq86/fasm-mac
brew install kroq86/fasm-mac/fasm-mac
```

Build `kroq86/setdb` and provide its executable through `SETDB_BIN`.

Run the bounded proof:

```bash
SETDB_BIN=/path/to/setdb ./formal/setdb/verify
```

Expected:

```text
PASS bound=10 states=463 transitions=1270 reachable=463 violations=0
```

Run the fixed-limit saturation proof:

```bash
SETDB_BIN=/path/to/setdb ./formal/setdb/verify-saturation
```

Expected:

```text
SATURATION_PASS base=12 guard=14 max_append_batch=2 states=552 transitions=1509
```

Run predicate mutation sensitivity:

```bash
SETDB_BIN=/path/to/setdb ./formal/setdb/verify-mutants
```

Expected final line:

```text
MUTATION_MATRIX_PASS mutants=8
```

Run runtime refinement evidence:

```bash
SETDB_BIN=/path/to/setdb PYTHONPATH=src:. \
  .venv/bin/python -m formal.refinement.verify_runtime
```

Expected:

```text
REFINEMENT_PASS scenarios=4 fastapi=1 snapshots=122 formal_states=463 reachable=463
```

Run the dispatcher crash-window safety proof:

```bash
SETDB_BIN=/path/to/setdb ./formal/crash_window/verify
SETDB_BIN=/path/to/setdb ./formal/crash_window/verify-mutants
SETDB_BIN=/path/to/setdb ./formal/crash_window/verify-runtime
```

Expected:

```text
CRASH_WINDOW_PASS states=216 inv=11 transitions=16 base_violations=0 step_violations=0
CRASH_WINDOW_MUTATION_MATRIX_PASS mutants=2
CRASH_RUNTIME_REFINEMENT_PASS scenarios=1 snapshots=5 physical_invocations=2
                              committed_results=1 stable_operation_id=1
                              unmatched_transitions=0
```

Run the local EventStoreModel safety proof:

```bash
SETDB_BIN=/path/to/setdb ./formal/store/verify
SETDB_BIN=/path/to/setdb ./formal/store/verify-mutants
SETDB_BIN=/path/to/setdb ./formal/store/verify-runtime
```

Expected:

```text
STORE_PASS states=384 inv=3 transitions=8 base_violations=0 step_violations=0
STORE_MUTATION_MATRIX_PASS mutants=8
STORE_RUNTIME_REFINEMENT_PASS scenarios=4 snapshots=12 formal_states=384
                              unmatched_transitions=0
```

## 13. Explicit limitations

The result does not prove:

- arbitrary values of `max_model_steps` and `max_tool_calls` (the saturated
  result covers the fixed counter semantics encoded by this model);
- multiple runs or scheduling between runs;
- global checkpoint interaction between agent versions;
- arbitrary user-defined reducers, reactions, effects, or policies;
- multi-process SQLite concurrency;
- correctness of Ollama, tools, HTTP clients, or external systems;
- correctness of all possible Python runtime executions;
- exactly-once physical external execution;
- liveness without an explicit fairness assumption;
- a composed proof connecting the model-loop, dispatcher, store, and resource
  abstractions to every Python runtime execution.

The general durable-state refinement layer does not itself observe this crash
window:

```text
external invocation
-> crash before result commit
-> repeated invocation after restart
```

Physical invocation count is operational and may change while durable
history/checkpoints remain identical. The dedicated crash-window verifier uses
an explicit operational observer for one cancellation/restart scenario; this
information must not be guessed by the general durable-state abstraction.

### Proof decomposition roadmap

Further proof work is decomposed as specified in `formal/DECOMPOSITION.md`:

```text
EventStoreModel
DispatcherModel
ModelLoopModel
ResourceModel
CompositionModel
```

The parameterized `low | before | at` counter abstraction belongs only to
`ModelLoopModel`. The fixed `2/2` trace model remains the integration witness
and runtime-refinement target.

### Parameterized ModelLoopModel result

The local finite over-approximation uses independent `low | before | at`
classes for arbitrary positive model-step and tool-call limits. Its complete
generated relation has 54 states, 49 strengthened-invariant states, 219 raw
action edges, and 159 unique setdb transition pairs.

The checked obligations are:

```text
GENERATE_OK states=54 transitions=219
EMIT_OK facts=4 bytes=104
ABSTRACT_GRAPH_PASS states=54 raw_transitions=219 transitions=159
BASE_PASS violations=0
STEP_PASS violations=0
ABSTRACT_MUTATION_MATRIX_PASS mutants=5
SIMULATION_PASS concrete_states=552 concrete_transitions=1509
                projected_transitions=33 unmatched_transitions=0
```

Therefore the strengthened invariant is inductive over the complete finite
abstract ModelLoopModel, allowing traces of unbounded length in that model.
The saturated concrete `2/2` transition relation forward-simulates into the
abstract relation. This is not an unbounded proof of the entire Python runtime
or of the other decomposed store/dispatcher/resource models.

### Dispatcher crash-window safety result

The local operational state is:

\[
O=(DurablePhase,OperationalPhase,OperationId,PhysicalInvocations,
CommittedResults)
\]

The two counters use the exact finite abstraction `zero | one | many`. FASM
enumerates all 216 values of this state type and generates `Next` from six
actions: commit request, invoke, crash, restart, commit result, and force fail.
The strengthened invariant contains 11 states and its generated relation has 16
unique transition pairs.

setdb establishes:

\[
Initial\subseteq Inv
\]

and:

\[
Inv(s)\land Next(s,s')\Rightarrow Inv(s')
\]

The invariant entails:

```text
committedResults(operation) <= 1
operationId(retry) = operationId(original)
committed result => physicalInvocations >= 1
terminal durable state => operational phase is idle
```

Two transition mutations are independently detected: permitting a second
committed result and replacing the operation ID on retry. This proves safety of
the finite crash-window abstraction without a trace-length bound. It does not
prove progress: without fairness, the environment may crash every invocation
before observation commit.

One real `DurableEffectDispatcher` scenario supplies runtime-refinement
evidence for the critical window. A controlled provider records the physical
invocation and blocks; the dispatcher task is cancelled before the result can
be committed. Fresh agent, resource, and dispatcher objects then consume the
same durable request from the surviving store. Across five observed boundaries,
both physical invocations carry the same operation ID, exactly one result is
committed, every snapshot belongs to the strengthened invariant, and every
adjacent pair belongs to the FASM-generated transition relation.

This is scenario evidence, not universal dispatcher refinement. In particular,
the observation uses task cancellation as the process-loss boundary and does
not prove every OS/process/store failure mode.

### EventStoreModel safety result

The local store abstraction retains `zero | one | many` pending distance and
seven ghost monitors for append-only history, monotonic positions, unique event
identity, atomic batches, both directions of the output/checkpoint batch
contract, and conflict atomicity. FASM generates the complete 384-state type
and transitions from the three strengthened-invariant states. setdb proves the
base case and inductive step with eight targeted mutants killed independently.

This result is unbounded in trace length for the finite local abstraction. It
is accompanied by scenario refinement of the public SQLite EventStore at 12
completed persisted boundaries: empty creation, one- and multi-event append,
expected-version success and conflict, reopen, and duplicate-ID rollback. It
does not cover multiple streams, subscriptions, concurrent connections, or
processes, and is not universal runtime refinement.

## 14. Conclusion

The established results are:

> All 463 states reachable in the bounded single-run AIQ 0.2 formal model
> with history length at most ten satisfy eight safety invariants, and every
> invariant independently detects a targeted transition mutation. In addition,
> 122 observed runtime snapshots across four direct scenarios and one FastAPI
> run map to reachable formal states, with every checked direct-runtime boundary
> corresponding to a formal transition.

> For the current fixed event-carried loop-counter semantics, exploration
> saturates at 552 reachable states and 1509 transitions. Identical normalized
> graphs at capacities 12 and 14, with a maximum atomic append batch of two,
> establish that the graph is closed under `Next` and that the same eight safety
> invariants hold without a remaining history-capacity restriction for this
> configuration.

The fixed configuration's formal model is proved within its stated scope.
Runtime refinement is supported by finite scenario evidence at the ten-event
ABI. Neither result is an unbounded proof of arbitrary policy limits or of the
complete AIQ runtime.

The parameterized ModelLoopModel, local dispatcher crash-window model, and
local EventStoreModel also have inductive safety proofs over their complete
finite abstractions. These remain decomposed proofs; a composition theorem and
universal runtime refinement have not been established.
