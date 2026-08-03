# AIQ — Durable AI Agent Runtime for Python

[![PyPI](https://img.shields.io/pypi/v/aiq)](https://pypi.org/project/aiq/)
[![Python](https://img.shields.io/pypi/pyversions/aiq)](https://pypi.org/project/aiq/)
[![License](https://img.shields.io/pypi/l/aiq)](https://github.com/kroq86/aiq/blob/main/LICENSE)

AIQ is an event-sourced Python runtime for building durable AI agents and
bounded LLM workflows. It keeps agent decisions, tool requests, validation
results, checkpoints, retries, and terminal outcomes in an explicit,
replayable history.

AIQ is designed for systems where a model can propose an action, but
deterministic application code must decide whether that action is valid,
execute it durably, and prove what was committed after a restart.

## Core capabilities

- Durable event log with optimistic stream versions and global ordering.
- Restart, replay, subscription checkpoints, and causal traces.
- Guarded model/tool loop with request and result validation.
- Workflow invariants, goal-gated completion, cycle detection, retry, replan,
  abstain, and terminal-state protection.
- Stable operation IDs and append-only physical attempt telemetry.
- Coordinated multi-worker effect execution over one shared SQLite database.
- Atomic lease claims, SQLite database-time expiry, fresh lease identities,
  monotonic fencing tokens, heartbeat renewal, and stale-worker rejection.
- FastAPI embedding, Server-Sent Events, Ollama provider, and MCP tool adapter.
- Bounded executable models, targeted semantic mutants, runtime refinement
  scenarios, and crash-window tests.

## Installation

```bash
pip install aiq
```

Optional integrations:

```bash
pip install "aiq[fastapi]"
pip install "aiq[mcp]"
pip install "aiq[ollama]"
```

The Python package and command-line entry point are both named `aiq`:

```python
from aiq import Event, InMemoryEventStore
```

## Execution model

```text
model or provider proposal
→ schema and policy validation
→ guarded durable transition
→ tool/effect execution
→ normalized durable result
→ invariant and goal checks
→ committed terminal outcome
```

For SQLite multi-worker effects:

```text
pending effect
→ atomic lease claim + attempt record
→ pre-handler ownership confirmation
→ handler with heartbeat renewal
→ fenced result/checkpoint commit
```

## Guarantee boundary

AIQ prevents stale workers from committing durable outputs and preserves at
most one committed result for the covered SQLite protocol. It does **not**
guarantee exactly-once physical external I/O. A worker can perform an external
effect and crash before committing its result, so integrations must use the
stable `operation_id` as a downstream idempotency key or provide equivalent
deduplication.

SQLite lease coordination currently assumes:

- one shared SQLite file;
- reliable filesystem locking;
- WAL mode and short coordination transactions;
- one canonical effect subscription;
- cooperative heartbeat scheduling.

PostgreSQL lease claims, transactional outbox delivery, universal composition
proofs, and production-scale capacity guarantees are not included.

## Documentation

- [Repository and quick start](https://github.com/kroq86/aiq)
- [Effect execution and idempotency](https://github.com/kroq86/aiq/blob/main/docs/effects.md)
- [Durable model loop](https://github.com/kroq86/aiq/blob/main/docs/model-loop.md)
- [FastAPI integration](https://github.com/kroq86/aiq/blob/main/docs/fastapi.md)
- [MCP adapter](https://github.com/kroq86/aiq/blob/main/docs/mcp.md)
- [Formal model boundaries](https://github.com/kroq86/aiq/blob/main/formal/FORMAL_MODEL.md)
- [Changelog](https://github.com/kroq86/aiq/blob/main/CHANGELOG.md)

AIQ is alpha software. Its strongest current deployment contract is a
controlled Python application using SQLite, explicit downstream idempotency,
and the documented worker lifecycle.
