# Agentlog 0.2 formal model

## 1. Scope and claim boundary

Agentlog is modeled as several small transition systems rather than one
universal state machine:

```text
EventStoreModel
DispatcherModel
ModelLoopModel
ResourceModel
CompositionModel
```

The complete product state is described by:

\[
Run_t=(\mathcal D_v,H_t,C_t,W_t^{explicit})
\]

where `D_v` is the immutable agent definition, `H_t` is append-only domain
history, `C_t` contains durable subscription checkpoints, and
`W_t^{explicit}` contains explicitly registered operational resources. Agent
state is derived rather than independently persisted:

\[
State_t=Fold(\mathcal D_v.initial,H_t)
\]

This document distinguishes three claims:

1. a machine-checked safety proof of a formal transition system;
2. runtime-refinement evidence for observed Python/FastAPI executions;
3. specified but not yet discharged composition obligations.

It does not claim a universal proof of all Python executions, SQLite/process
failure modes, schedulers, providers, tools, or networks.

## 2. Agent and policy semantics

`DurableModelLoop.install(agent)` declaratively installs ordinary reducers,
reactions, and effects. The installed policy owns no mutable executor state:

\[
PolicyState=\varnothing
\]

Operational provider and tool objects are resolved from application resources,
not captured as hidden definition state. The durable loop is event-carried:

```text
Start
-> ModelRequested
-> ModelSucceeded(tool)
-> ToolRequested
-> ToolSucceeded
-> ModelRequested
-> ModelSucceeded(answer)
-> [AnswerProduced, RunCompleted]
```

The final two events form one atomic reaction batch. They are causal siblings:

\[
cause(AnswerProduced)=cause(RunCompleted)=id(ModelSucceeded)
\]

Their order in history does not create a causal edge between them.

## 3. Concrete single-run integration model

The concrete state is:

\[
M=(H,C_r,C_e,Status,Definition,Resource,I_m,I_t)
\]

with ordered history `H`, reaction/effect checkpoints `C_r` and `C_e`, run
status, immutable definition identity, resource fingerprint, and model/tool
invocation counters. An event is:

\[
e=(type,cause,operation,flags)
\]

Event identity is its 1-based history position. The finite vocabulary is:

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

The generated action relation contains:

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

For every committed result `r`, the corresponding request `q` must satisfy:

\[
Request(q)\land cause(r)=id(q)\land operation(r)=operation(q)
\]

and every request has stable operation identity:

\[
operation(q)=id(q)
\]

The eight checked concrete invariants are:

1. `HistoryAppendOnly`: \(H\preceq H'\).
2. `CheckpointMonotonic`: \(C_r'\ge C_r\land C_e'\ge C_e\).
3. `ResultHasRequest`.
4. `ResultOperationMatchesRequest`.
5. `AtMostOneResultPerRequest`.
6. `AtMostOneTerminal`.
7. `TerminalIsAbsorbing` for domain history.
8. `DefinitionResourceConsistent`.

The bounded ABI at history length ten is a packed 48-byte state. Exhaustive
generation produces:

```text
states=463
transitions=1270
violations=0
mutants=8/8
```

For the fixed event-carried limits `2/2`, exploration saturates:

```text
states=552
transitions=1509
base_capacity=12
guard_capacity=14
max_append_batch=2
```

The normalized graphs at capacities 12 and 14 are identical. Since one action
appends at most two events, the fixed `2/2` graph is closed under `Next`.

## 4. Parameterized ModelLoopModel

Arbitrary positive model/tool limits are represented by the finite
over-approximation:

\[
A=(Phase,ModelClass,ToolClass)
\]

where:

```text
Phase = idle | model_pending | tool_pending | limit | completed | failed
Class = low | before | at
```

For a positive limit `L`:

\[
class(c,L)=
\begin{cases}
low,&c<L-1\\
before,&c=L-1\\
at,&c\ge L
\end{cases}
\]

Increment is deliberately nondeterministic:

```text
low    -> low | before
before -> at
at     -> at
```

This admits extra abstract behavior and is therefore a sound
over-approximation, not an exact quotient. Model and tool counter classes are
independent. An `at` guard moves to `limit`; terminal phases are absorbing
apart from restart/self-loop behavior.

FASM enumerates all 54 tuples. setdb checks the inductive obligations:

\[
AInitial\subseteq AInv
\]

\[
AInv(s)\land ANext(s,s')\Rightarrow AInv(s')
\]

Result:

```text
states=54
invariant_states=49
raw_transitions=219
unique_transitions=159
base_violations=0
step_violations=0
mutants=5/5
```

## 5. Concrete-to-abstract simulation

FASM defines:

\[
\beta:Concrete_{2/2}\rightarrow Abstract
\]

using `0 -> low`, `1 -> before`, and `2+ -> at`. setdb computes:

\[
Projected=\beta^{-1};ConcreteTransition;\beta
\]

and verifies:

\[
Projected\setminus ATransition=\varnothing
\]

Result:

```text
concrete_states=552
concrete_transitions=1509
projected_transitions=33
unmatched_initial=0
unmatched_transitions=0
```

The normal concrete graph has no bad states. Therefore property reflection

\[
BadConcrete\Rightarrow BadAbstract
\]

is vacuous there and is not claimed as independently established. It requires
projection of concrete mutant bad states through a future property abstraction.

## 6. Dispatcher crash-window model

One durable external operation is represented by:

\[
O=(DurablePhase,OperationalPhase,OperationId,
PhysicalInvocations,CommittedResults)
\]

with:

```text
DurablePhase     = none | requested | result | failed
OperationalPhase = idle | invoked
OperationId      = none | original | other
counter          = zero | one | many
```

Actions are:

```text
CommitRequest
Invoke
Crash
Restart
CommitResult
ForceFail
```

A crash loses only the operational marker:

\[
(requested,invoked,k,n,0)
\rightarrow
(requested,idle,k,n,0)
\]

The durable request and operation ID survive. Retry may increase physical
invocations but must preserve identity:

\[
(requested,idle,k,n,0)
\rightarrow
(requested,invoked,k,n+1,0)
\]

The checked invariants entail:

\[
CommittedResults(k)\le1
\]

\[
OperationId_{retry}=OperationId_{original}
\]

\[
CommittedResults(k)=1\Rightarrow PhysicalInvocations(k)\ge1
\]

\[
TerminalDurableState\Rightarrow OperationalPhase=idle
\]

The complete finite abstraction contains 216 tuples. The strengthened
invariant and generated transition relation satisfy:

```text
states=216
invariant_states=11
transitions=16
base_violations=0
step_violations=0
mutants=2/2
```

This proves safety, not exactly-once physical execution. Repeated physical
invocation is expected after a crash before observation commit.

## 7. Runtime refinement evidence

The integration abstraction is:

\[
\alpha:RuntimeSnapshot\rightarrow ConcreteState
\]

It maps runtime history, reaction/effect checkpoints, terminal events,
definition/resource identities, causation UUIDs, and operation UUIDs into the
concrete packed state. For observed direct-runtime boundaries:

\[
\alpha(r)\in Reachable
\]

\[
(\alpha(r),\alpha(r'))\in ConcreteTransition
\]

Checked evidence:

```text
direct_scenarios=4
FastAPI_runs=1
snapshots=122
unmatched_transitions=0
```

The crash-window refinement scenario executes the real
`DurableEffectDispatcher`:

```text
request committed
-> provider invoked
-> dispatcher cancelled before result commit
-> fresh Agent/resources/dispatcher over the same store
-> provider invoked again
-> one result committed
```

Result:

```text
scenarios=1
snapshots=5
physical_invocations=2
committed_results=1
stable_operation_id=1
unmatched_transitions=0
```

These are scenario-based refinement checks, not a theorem over all Python
executions.

## 8. EventStoreModel

The concrete storage objects are projected into a local finite safety state:

\[
E=(PendingClass,A_{append},A_{position},A_{id},A_{batch},
A_{out\Rightarrow cp},A_{cp\Rightarrow out},A_{conflict})
\]

`PendingClass = zero | one | many` abstracts the distance between the global
position and one subscription checkpoint. The seven Boolean fields are ghost
safety monitors, not SQLite columns. Actions cover append, one/two-output
subscription commits, checkpoint-only consumption, version/checkpoint
conflicts, and restart.

The invariant requires append-only histories, monotonic positions, unique event
identities, atomic event batches, both directions of the subscription
output/checkpoint contract, and conflict atomicity:

\[
Conflict\Rightarrow E'=E
\]

FASM enumerates all 384 abstract tuples. setdb establishes:

\[
SInitial\subseteq SInv
\]

\[
SInv(s)\land SNext(s,s')\Rightarrow SInv(s')
\]

```text
states=384
invariant_states=3
unique_transitions=8
base_violations=0
step_violations=0
mutants=8/8
```

This proves the finite local abstraction without a trace-length bound. SQLite
runtime scenarios refine 12 persisted snapshots across four single-stream
scenarios with no unmatched transitions. Multi-stream/subscription/process
composition is not established.

## 9. ResourceModel obligations

The target resource state is:

\[
R=(DefinitionVersion,RequiredCapabilities,
CapturedFingerprints,ResolvedFingerprints)
\]

Invocation requires matching definition and resource identities:

\[
Definition_{run}=Definition_{runtime}
\]

\[
CapturedFingerprint=ResolvedFingerprint
\]

An independent inductive ResourceModel proof artifact has not yet been
implemented. Parts of its obligations are covered by the concrete model and
runtime scenarios.

## 10. Composition obligations

The local proofs do not compose automatically. The remaining interfaces are:

```text
Store.atomic_batch_checkpoint
-> dispatcher observes outputs and checkpoint together after restart

ModelLoop.atomic_reaction_outputs
-> dispatcher preserves ordered sibling output and causation

Dispatcher.one_committed_processing_per_checkpoint
-> one request has at most one committed result

Resource.definition_matches
-> dispatcher may invoke the selected capability

ModelLoop.single_terminal
and Dispatcher.fresh_terminal_recheck
and Store.atomic_commit
-> terminal history is absorbing under modeled races
```

Universal runtime refinement would require:

\[
\forall r,r':
r\rightarrow_{Python}r'
\Rightarrow
\alpha(r)\rightarrow_{Formal}^{*}\alpha(r')
\]

That theorem has not been established.

## 11. Established chain

The current checked chain is:

\[
RuntimeScenarios
\preceq
Concrete_{2/2}
\preceq
AbstractModelLoop
\models
ModelLoopSafety
\]

and independently:

\[
RuntimeCrashScenario
\preceq
CrashWindowModel
\models
CrashSafety
\]

and independently:

\[
EventStoreAbstract\models StoreSafety
\]

The correct product claim is:

> Agentlog 0.2 implements a durable model/tool loop for FastAPI agents. Its
> model-loop semantics has a saturated concrete witness, an inductively checked
> parameterized abstraction, and scenario-based runtime refinement. Its
> crash-window safety has a separate inductive abstraction and one real
> dispatcher refinement scenario.

The incorrect claim is:

> The entire Python framework and every external execution have been formally
> proved correct.

## 12. Complexity and trusted boundary

Explicit concrete exploration costs:

\[
Time=O(|S|\cdot|Actions|),\qquad Memory=O(|S|+|T|)
\]

The parameterized ModelLoopModel and crash-window model have fixed finite state
spaces, so their inductive checks do not grow with runtime history length.

The trusted computing base includes FASM encodings and transition generators,
the abstraction functions implemented in FASM, setdb set/relation operations,
and the shell drivers that materialize and compare facts. Python is used only
to extract runtime observations for refinement; it does not define formal
`Next`.

Machine-checked commands and exact expected output are recorded in
`formal/PROOF.md`.
