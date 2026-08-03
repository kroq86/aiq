# AIQ 0.5.0 release evidence

Base release: annotated tag `v0.4.3`, target commit
`feadc29be8720e0d48fc7fbacdb99c0b01893e52`.

Distribution/import/CLI identity: `aiq` / `aiq` / `aiq`.

## Protocol scope

One shared SQLite file, WAL, connection per operation, `BEGIN IMMEDIATE`,
SQLite database time, canonical effect subscription, multiple effect workers,
atomic claim+attempt, pre-handler confirmation, renewal, takeover, and fenced
result/checkpoint commit.

## Implemented properties

- atomic claim plus durable attempt recording;
- full-stream terminal and committed-result admission checks;
- fresh UUID lease identity for every claim/takeover;
- SQLite database-time expiry and heartbeat renewal;
- monotonic fencing tokens across release and repeated takeover;
- immediate pre-handler ownership confirmation;
- atomic fenced result/checkpoint commit;
- stale ownership and stale commit rejection;
- append-only claim/busy/expiry/renewal/takeover/stale observations;
- stable `operation_id` across physical retries;
- at most one committed result per request within the SQLite protocol.

## Formally established

`formal/lease_gate/check.py` exhaustively explores 20,361 normalized states and
20,360 transitions within bound 8. It checks one DB-valid owner, confirmed
handler admission, attempt-before-handler, monotonic fencing generations,
expired/stale commit rejection, and at most one committed result.

Non-vacuity witnesses: 10, including claim, attempt, busy, renewal, expiry,
takeover, ownership confirmation, handler start, stale rejection, and commit.

Bounded semantic mutants: 6 killed, 0 survived.

## Runtime mutation adequacy

`formal/lease_gate/verify_runtime_mutants.py` applies 12 real source mutants to
`src/aiq/sqlite.py` and `src/aiq/runtime.py`. Every mutant is killed
by an unchanged targeted test. Source bytes are restored in `finally`, checked
after every mutant, and checked again at completion.

Normal source SHA-256 values at the mutation gate:

- `src/aiq/sqlite.py`:
  `a98600c8044dfe5f8553bf66396308b1a28ea9d7332bff885ad6afd6dae3a362`
- `src/aiq/runtime.py`:
  `3808015b82d936e0145396c85fab12b5629bad0a453d390d6c88ee936a6cc296`

The manifest covers split claim/attempt transactions, premature claim,
released/takeover token reuse, missing token/fence/expiry checks, expired and
foreign renewal, claim after result commit, omitted pre-handler confirmation,
worker-clock authority, and stale commit.

## Runtime scenarios

`formal/lease_gate/verify_runtime.py` executes eight controlled SQLite
boundaries: claim, contention, renewal, expiry/takeover, stale ownership,
stale commit rejection, fenced commit, and durable observation alignment.

Stress evidence in `tests/test_effect_lease_stress.py`:

- 10 independent operations × 4 simultaneous claimers;
- 30 deterministic seeded crash/release/expiry schedules;
- 100 shared-file claim+commit operations across four worker identities;
- final `PRAGMA integrity_check = ok`, contiguous attempts, monotonic tokens,
  one valid owner, and one committed result per operation.

These workloads are bounded regression evidence, not production capacity or a
Postgres benchmark.

## Package and test gates

```text
full suite: 356 passed, 2 skipped, 115 subtests passed
focused lease RC suite: 89 passed, 6 subtests passed
Ruff: PASS
diff check: PASS
sdist SHA-256: 7239859483067ed53871915ce4f03037df93cbce88a1df872afbb76dd69125e7
wheel SHA-256: ad772a8892839a02073df616fd445c110ac2d00c5b059ed5f7244ea9fd517d17
clean-wheel SQLite smoke: PASS; distribution/import/CLI `aiq`, metadata 0.5.0
```

## Composition and liveness

Composition with crash-window, completion-gate, cycle-guard, abstention, and
base lifecycle models remains open. No scheduler fairness, starvation freedom,
or unconditional progress theorem is established.

## Trusted computing base

Python, SQLite locking/time semantics, the BFS checker and predicates, pytest,
the source-mutant patch/restore harness, SHA-256 implementation, filesystem
restoration, and the reviewer’s runtime-to-model mapping.

## Not proved

- exactly-once physical execution or absence of overlap after expiry;
- downstream provider deduplication: external effects remain at-least-once
  and require downstream idempotency keyed by stable `operation_id`;
- PostgreSQL claim/lease support; the implemented coordination backend is one
  shared SQLite database only;
- safe SQLite locking on arbitrary network filesystems;
- universal runtime refinement or distributed liveness;
- production worker-count capacity;
- correctness of application terminal types or idempotency policy.
