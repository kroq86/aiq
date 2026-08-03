# Contributing to AIQ

AIQ treats durable execution semantics as public contracts. Keep changes
small enough that their state boundary, invariants and evidence can be reviewed
together.

## Workflow

Before changing behavior:

1. Read `README.md` and the relevant contract in `docs/`.
2. Choose one independent change set.
3. List touched files, affected transitions and preserved invariants.
4. Do not change a public contract without explicit review.
5. Add implementation tests and the appropriate executable-model evidence.
6. Update documentation when a guarantee or limitation changes.

## Pre-1.0 version policy

Before 1.0, AIQ versions follow the reviewed roadmap rather than claiming
a stable public API:

- `0.y.z` may add backward-compatible, opt-in capabilities within the current
  milestone;
- `0.y.0` marks a new runtime/state-model milestone;
- an intentional compatibility break requires an explicit migration note and
  review of persisted-data/runtime implications, regardless of version size.

`0.4.3` therefore contains the opt-in attempt store API and additive RunReport
field while preserving existing execution when telemetry is not configured.
Lease/fencing changes the concurrency and commit model and belongs to the next
minor milestone.

## Coordination boundaries

Coordinate before parallel changes to:

- `EventStore` contracts;
- the SQLite schema;
- atomic subscription transactions;
- reducer or reaction signatures;
- effect failure semantics;
- checkpoint semantics;
- runtime/model abstraction and normalization.

## Evidence rule

A guarantee must name the evidence that supports it. Keep these levels separate:

- implementation tests;
- selected runtime scenarios;
- bounded finite checks;
- checked inductive invariants;
- mutation sensitivity;
- runtime refinement;
- composition and liveness obligations.

Do not turn a bounded result into an unbounded claim, a model result into a
universal implementation claim, or a safety result into a liveness claim. The
full engineering protocol and current reproduction commands are documented in
[`docs/model-verification.md`](docs/model-verification.md),
[`docs/release-evidence-0.3.md`](docs/release-evidence-0.3.md), and
[`docs/release-evidence-0.4.md`](docs/release-evidence-0.4.md).

### Spec-driven safety-critical transitions

For a state transition where crash, retry, or model-error behavior matters
(not for CLI, formatting, configuration, or plain I/O glue), state the change
as a narrow spec before writing runtime code, then close it in this fixed
order:

```text
narrow contract
-> executable model
-> targeted semantic mutant
-> runtime/refinement scenario
-> explicit "not proved"
```

This is not a proposed process -- it is the one already repeated for
`formal/cycle_guard/`, `formal/completion_gate/`, and
`formal/run_abstained/`. Each started from a one-line contract, got a small
bounded-exhaustive model with a non-vacuity witness, got at least one targeted
mutant with a concrete counterexample, got a runtime/restart scenario check
against the real dispatcher, and shipped with an explicit boundary of what
remains unproved. The effect-dispatch attempt ledger
(`src/aiq/attempts.py`) reuses the existing crash-window model and its
runtime-refinement scenario; it does not claim a separate bounded model or an
attempt-specific semantic mutant.

Keep these four claims distinct and never substitute one for another:

- **model evidence** -- a bounded, finite abstraction has no violation within
  stated bounds, and every claimed action/event is actually reachable in it
  (an unreachable action makes its invariants vacuous, not proved);
- **runtime scenario evidence** -- selected real executions, including
  restart/crash points, match the model's transitions;
- **universal refinement** -- every possible runtime execution refines the
  model; this is not established by any bounded model or finite set of
  scenarios, and must not be implied by one;
- **composition** -- whether independently proved local models combine into
  one guarantee about the whole system; local proofs do not compose
  automatically and this must be argued separately, not assumed.

State only the invariant direction that is actually proved. For example, the
attempt ledger proves `HandlerInvoked -> AttemptRecorded` (fail-closed
recording guarantees no handler runs unrecorded); it does not prove the
converse (`AttemptRecorded -> HandlerInvoked` is not a safety property --
a crash between the recorded attempt and handler entry is an explicit,
disclosed gap, not something the ledger rules out).

## Review invariant

> A guarantee exists only together with the appropriate executable evidence.
