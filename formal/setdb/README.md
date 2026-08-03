# AIQ 0.2 bounded FASM + setdb model

The proof path contains no Python. FASM builds the reachable state graph from
one zero state by applying all eleven actions, structurally deduplicates packed
48-byte states, derives invariant violations from raw histories and edges, and
emits one setdb `SADD`/`RADD` fact stream. setdb computes `Reachable` and each
`Reachable ∩ BadProperty` intersection.

## Bound and state

`MODEL_BOUND=10` is the maximum event-history length. The finite state contains
history events (`type`, `cause`, `operation`, `flags`), reaction/effect
checkpoints, derived run status, fixed definition/resource identity, and bounded
model/tool invocation counters. Positions are 1-based; zero means absent.

The model covers `CreateRun`, `AppendStart`, reaction dispatch, model answer/tool
selection/failure, tool success/failure, forced completion/failure, and restart.
Restart is a durable-state self-loop. A model response contains at most one tool
call. Resource fingerprint equality is a `CreateRun` precondition; resource
drift is not a nondeterministic transition in this bounded model.

Reaction outputs are appended as one atomic batch. Every output in that batch
has the consumed event as its immediate cause. In particular,
`AnswerProduced` and `RunCompleted` are ordered siblings caused by the same
`ModelSucceeded`; their history order does not create a causal edge between
them, and no intermediate answer-only state is reachable.

## Properties

The common FASM checker derives these sets rather than trusting pre-labelled
states:

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

## Run

```bash
SETDB_BIN=/path/to/setdb ./formal/setdb/verify
SETDB_BIN=/path/to/setdb ./formal/setdb/verify-mutant
SETDB_BIN=/path/to/setdb ./formal/setdb/verify-mutants
SETDB_BIN=/path/to/setdb ./formal/setdb/verify-saturation
```

Expected results for the checked model:

```text
PASS bound=10 states=463 transitions=1270 reachable=463 violations=0

FAIL_EXPECTED property=AtMostOneTerminal violations=1
counterexample=s00000000 -> CreateRun -> ForceComplete -> ForceComplete
```

The mutant removes the normal single-terminal guard. It does not mark any state
as bad; the same `terminal_count(history) > 1` predicate discovers the violating
state. Exploration stops expanding a state once it contains two terminals,
because the shortest safety counterexample has already been reached.

`verify-mutants` additionally changes one transition rule at a time and requires
the common checker to derive a reachable counterexample for all eight
properties: history rewrite, checkpoint rollback, missing request causation,
operation mismatch, duplicate result, second terminal, post-terminal
progression, and definition/resource mismatch. A mutant is accepted as killed
only when it exits with code 1 and prints both the exact expected property and a
counterexample. Compile errors and capacity failures are rejected.

## Fixed-limit saturation

`verify-saturation` checks the fixed-limit model at history capacities 12 and
14. Two is the largest atomic append batch in `Next`. It requires identical
state identifiers, normalized semantic encodings, and `Transition` pairs at
both capacities. Equality means that increasing capacity by the largest
possible append batch exposes no additional successor from the complete graph
at 12.

This is a completeness result for the current fixed loop-counter semantics. It
is not a parameterized proof for arbitrary policy limits; that requires the
separate finite abstraction.

## Claim boundary

The result proves the eight properties for every state reachable in this
single-run abstract transition system with history length at most ten. It does
not prove arbitrary AIQ definitions, multi-run/global-position scheduling,
external system correctness, multi-process storage concurrency, or refinement
of the Python runtime. Those require separate models or refinement checks.
