# Dispatcher crash-window safety model

This directory contains a finite, parameter-free operational abstraction for
one durable external operation. FASM enumerates the state space and transition
relation; setdb checks the base case and inductive step.

The state is:

```text
(durable phase, operational phase, operation id,
 physical invocations, committed results)
```

Both counters use the exact abstraction `zero | one | many`. A crash loses the
in-memory `invoked` marker but preserves the committed request, operation ID,
and committed observation. Retrying may therefore increase physical
invocations, but it must reuse the original operation ID. The normal semantics
permits at most one committed result.

Checked safety properties:

```text
committedResults(operation) <= 1
operationId(retry) = operationId(original)
committed result implies at least one physical invocation
terminal durable state has no live operational invocation
```

Run:

```bash
formal/crash_window/verify
formal/crash_window/verify-mutants
formal/crash_window/verify-runtime
```

This is an unbounded proof of the finite counter abstraction, not a proof of
SQLite, process scheduling, provider behavior, or liveness. Progress requires
a separately stated fairness assumption; an execution may otherwise crash
forever before committing an observation.

`verify-runtime` supplies narrower refinement evidence for the real Python
`DurableEffectDispatcher`: a controlled provider is invoked, the dispatcher
task is cancelled before result commit, fresh agent/resources/dispatcher
objects are created over the same store, and the request is invoked again. The
five observed boundaries must belong to `CInv`, every adjacent pair must be in
the FASM-generated `CTransition`, both physical calls must carry the same
operation ID, and exactly one result must be committed. This is one executable
crash/restart scenario, not universal runtime refinement.
