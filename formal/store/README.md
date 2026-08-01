# EventStore safety abstraction

This directory contains the local finite EventStoreModel safety abstraction.
FASM generates the transition relation and setdb checks the inductive base and
step obligations.

The state is:

```text
(pending class,
 append-only monitor,
 position-monotonic monitor,
 unique-event-id monitor,
 atomic-batch monitor,
 outputs-require-checkpoint monitor,
 batch-checkpoint-requires-outputs monitor,
 conflict-unchanged monitor)
```

`pending = zero | one | many` abstracts the distance between the global event
position and one subscription checkpoint. The seven monitor fields are ghost
state used to evaluate safety; they are not SQLite columns or runtime fields.

Actions model one-event and two-or-more-event append, one/two-output atomic subscription commits,
checkpoint-only consumption, version/checkpoint conflicts, and restart.
Checkpoint-only consumption intentionally does not violate the batch
checkpoint/output equivalence: it is a distinct operation that asserts no
stream version and appends no output.

Run:

```bash
formal/store/verify
formal/store/verify-mutants
formal/store/verify-runtime
```

The eight mutants independently violate history append-only behavior, position
monotonicity, event-ID uniqueness, batch atomicity, each direction of the
output/checkpoint batch contract, conflict atomicity, and multi-event append
classification.

The runtime verifier covers one SQLite database, one stream, one process,
sequential connection-per-operation access, and completed persisted operation
boundaries. It checks empty creation, sequential appends, stale-version
rejection, atomic multi-event append, reopen durability, and rollback after a
duplicate-ID transaction. Checkpoint/subscription storage is outside this
refinement slice; `pending` is projected against a fixed external zero
checkpoint and only append/conflict/restart edges are exercised.

This is an unbounded inductive proof of the finite local abstraction. It is not
a proof of SQLite code, transactions, multiple streams/subscriptions, or
concurrent processes. Those require runtime refinement and composition work.
