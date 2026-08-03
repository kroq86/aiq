# SQLite lease/fencing bounded model

This local finite model covers one pending effect, two workers, DB-time lease
expiry, renewal, takeover, handler admission, and fenced commit.

## Reproduction

```bash
python3 formal/lease_gate/check.py
for mutant in non_atomic_claim missing_attempt_gate token_reuse \
  stale_commit expired_commit renew_after_expiry; do
  python3 formal/lease_gate/check.py --mutant "$mutant"
done
./.venv/bin/python formal/lease_gate/verify_runtime_mutants.py
```

Normal result:

```text
PASS bound=8 states=20361 transitions=20360 witnesses=10
```

All six bounded semantic mutants are killed. The separate runtime mutation
gate kills 12 real source mutants and verifies byte-for-byte SHA-256
restoration after every mutant and at the end.

## Evidence report

Protocol scope:
one request on one canonical effect subscription, two workers, one shared
SQLite authority, DB-time expiry, renewal, takeover, and fenced commit.

Formally established:
within transition bound 8, there is at most one DB-valid owner; a handler start
requires both an attempt fact and ownership confirmation; claim/takeover tokens strictly
increase while renewal retains the token; stale or expired workers cannot
commit outputs or checkpoint; and one request has at most one committed batch.

Bounds/domain and state/transition counts:
workers `{A, B}`, transition bound 8, 20,361 reachable normalized states and
20,360 explored transitions.

Non-vacuity and deadlocks:
normal claim, attempt recording, busy rejection, renewal, expiry, takeover,
ownership confirmation, handler start, valid commit, and stale rejection are
all witnessed.

Mutants killed/survived/equivalent/invalid:
6 bounded semantic mutants and 12 runtime source mutants killed; 0 survived,
0 equivalent, 0 invalid. Runtime source files are restored and hashed after
every mutation.

Runtime scenarios and unmatched boundaries:
runtime refinement is added separately and covers selected SQLite traces. The
bounded result alone is not evidence that the Python or SQL implementation
matches this model.

Composition obligations open:
composition with the base lifecycle, crash-window, completion-gate,
cycle-guard, and abstention models is not established.

Liveness/fairness established or open:
no progress, starvation-freedom, scheduling, or heartbeat fairness property is
established. A worker can remain busy forever in an unfair execution.

Trusted computing base:
Python, the BFS implementation, state encoding, safety predicates, and the
reviewer's interpretation of the lease abstraction.

Not proved:

- exactly-once physical execution or absence of overlapping physical calls
  after expiry;
- correctness of downstream idempotency;
- reliable cancellation of blocking or already accepted external I/O;
- multi-database, network-filesystem, or non-SQLite coordination;
- universal runtime refinement, composition, or liveness.
