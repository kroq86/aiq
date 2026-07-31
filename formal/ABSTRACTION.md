# Parameterized ModelLoopModel abstraction

## 1. Scope

This is the finite local abstraction of model-loop phases and independent
positive model/tool limits. It is not a universal abstraction of the event
store, subscription dispatcher, resources, crash window, or their composition.
Those responsibilities are separated in `formal/DECOMPOSITION.md`.

The fixed trace model uses concrete limits `2/2` for exact histories,
causation, operation identity, counterexamples, and runtime refinement. This
model removes exact counter values to cover arbitrary positive limits.

## 2. State

The complete abstract state is:

\[
A=(Phase,ModelClass,ToolClass)
\]

with six phases:

```text
idle
model_pending
tool_pending
limit
completed
failed
```

and two independent three-valued counter classes. There is no history,
checkpoint, subscription queue, provider object, or tool registry in this
local state.

The strengthened invariant contains all well-formed phase/counter tuples,
except an idle state whose zero counter has already been classified `at`.

## 3. Counter abstraction

For every positive limit `L`:

\[
class(c,L)=
\begin{cases}
low,&c<L-1\\
before,&c=L-1\\
at,&c\ge L
\end{cases}
\]

Increment is over-approximated by:

```text
low    → low | before
before → at
at     → at
```

`low` is nondeterministic because the exact distance to the limit is not
retained. The model is therefore a sound over-approximation, not an exact
quotient. It may admit extra paths; proving safety over the larger relation is
useful only after concrete transitions are shown to simulate into it.

The independent fields are:

```text
model_step_class
tool_calls_used_class
```

A model-step transition must not modify the tool class, and a tool-call
transition must not modify the model class.

## 4. Transition relation

The finite action vocabulary is:

```text
StartLow
StartAdvance
ModelAnswer
ModelToolLow
ModelToolAdvance
ModelFailure
ToolSuccessLow
ToolSuccessAdvance
ToolFailure
LimitFailure
Restart
ForceComplete
ForceFail
```

The two low/advance variants materialize the nondeterministic abstract
successors. `Restart` is a self-loop. Completed and failed phases allow no
progress other than restart, so terminal phases are absorbing in this local
model.

Tool-call guard:

```text
tool_calls_used_class = at
→ limit

tool_calls_used_class = low | before
→ tool_pending with an allowed increment successor
```

Model-continuation guard:

```text
model_step_class = at
→ limit

model_step_class = low | before
→ model_pending with an allowed increment successor
```

## 5. Inductive proof

FASM enumerates all 54 phase/counter tuples and generates transitions from
every one of the 49 strengthened-invariant states. This is not bounded by
history length.

setdb checks:

\[
AInitial\setminus AInv=\varnothing
\]

and:

\[
range(ATransition)\setminus AInv=\varnothing
\]

Established result:

```text
GENERATE_OK states=54 transitions=219
EMIT_OK facts=4 bytes=104
ABSTRACT_GRAPH_PASS states=54 raw_transitions=219 transitions=159
BASE_PASS violations=0
STEP_PASS violations=0
```

The 219 edges retain action variants; setdb deduplicates them to 159 unique
state pairs.

## 6. Mutation sensitivity

The common oracle re-evaluates every mutant edge with mutation mode disabled
and rejects successors outside normal semantics.

Killed mutations:

```text
before remains before
at permits another call
tool transition mutates model counter
model transition mutates tool counter
ToolCallRequested resets tool counter
```

Result:

```text
ABSTRACT_MUTATION_MATRIX_PASS mutants=5
```

Compile errors, process failures, and empty fact streams do not count as killed
mutants.

## 7. Concrete simulation

The concrete FASM trace generator emits `Beta(concrete, abstract)` directly
from each packed state. For fixed limits `2/2`:

```text
0 → low
1 → before
2+ → at
```

The phase is derived from terminal status, the latest request kind, and an
unhandled model-limit event. Checkpoint-only and other locally invisible
concrete transitions map to abstract restart/self-loop edges.

setdb computes:

\[
Projected=
Beta^{-1};ConcreteTransition;Beta
\]

and verifies:

\[
Projected\setminus ATransition=\varnothing
\]

Result:

```text
SIMULATION_PASS
concrete_states=552
concrete_transitions=1509
projected_transitions=33
unmatched_initial=0
unmatched_transitions=0
```

The normal concrete graph has no bad states, so bad-state preservation is
vacuous in this simulation command. The eight concrete mutants separately
establish trace-predicate sensitivity; projecting mutant bad states requires a
future property abstraction and is not claimed here.

## 8. Trusted boundary

The trusted computing base contains:

- FASM state/action encodings and transition generators;
- the FASM implementation of `Beta`;
- the strengthened-invariant classification;
- setdb finite set/relation operations;
- shell scripts that load and compare generated relations.

The local FASM Mach wrapper does not expose a reliable reusable raw-syscall
status in this emitter call path. Each short fact is written with one syscall;
completeness is checked externally by exact fixture diff, emitted fact counts,
and successful setdb loading.

This result proves the unbounded finite abstract ModelLoopModel and its forward
simulation for the saturated concrete `2/2` graph. It does not prove the whole
Python runtime, external systems, or the other decomposed formal models.
