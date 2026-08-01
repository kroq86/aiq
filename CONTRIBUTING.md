# Contributing to Agentlog

Agentlog treats durable execution semantics as public contracts. Keep changes
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
[`docs/model-verification.md`](docs/model-verification.md) and
[`docs/release-evidence-0.3.md`](docs/release-evidence-0.3.md).

## Review invariant

> A guarantee exists only together with the appropriate executable evidence.
