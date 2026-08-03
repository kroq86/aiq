# FastAPI integration

AIQ is an embedded runtime for FastAPI applications, not a separate
orchestration server. `aiq.fastapi.AIQ` is the one canonical
implementation of routes, broadcaster, background catch-up lifecycle, and
agent-ownership wiring; `aiq.http.create_app()` is a convenience
wrapper around it, not a second implementation.

FastAPI remains an **optional** dependency of the core package (see
[Optional dependency](#optional-dependency) below).

## Two styles

### Standalone convenience

```python
from aiq.http import create_app

app = create_app(store=store, runtimes={"energy-assistant": runtime})
```

Builds one `AIQ` integration and gives it its own dedicated `FastAPI`
app. Routes land at `/agents/...` with no extra prefix. Use this when
AIQ is the whole application.

### Embedding into an existing application

```python
from fastapi import FastAPI
from aiq.fastapi import AIQ

aiq = AIQ(
    store=store,
    runtimes={"energy-assistant": runtime},
)

app = FastAPI(lifespan=aiq.lifespan)
app.include_router(aiq.router, prefix="/api")
```

The host application owns `app`: its own routes, its own lifespan slot (see
[Lifespan composition](#lifespan-composition) if the host already has one),
its own middleware. AIQ only ever touches its own `APIRouter` and its
own background task -- it never mounts itself onto the host app directly,
and never takes over routes or lifecycle the host didn't hand it.

## Constructor

```python
class AIQ:
    def __init__(
        self,
        *,
        store: EventStore,
        runtimes: Mapping[str, AgentRuntime],
        route_prefix: str = "/agents",
        poll_interval_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 10.0,
    ) -> None: ...
```

`runtimes` keys must equal each `AgentRuntime.agent.name` -- a mismatch
raises `ValueError` at construction time, since stream ownership and
routing are both keyed by that name and must never silently diverge from
the agent's own identity. `poll_interval_seconds` and
`shutdown_timeout_seconds` must both be `> 0`, also checked at construction
time.

## Routes and prefix behavior

`aiq.router` is a plain `APIRouter` with its own internal prefix
(`route_prefix`, default `/agents`) already applied. `route_prefix`
controls *only* that AIQ-local root -- it never hardcodes or assumes
anything about the host's own prefix. Composition follows ordinary FastAPI
`include_router` semantics:

```python
app.include_router(aiq.router)                 # /agents/...
app.include_router(aiq.router, prefix="/api")   # /api/agents/...
```

Exposed route family (relative to wherever it's mounted):

```
POST /agents/{agent_name}/runs
GET  /agents/{agent_name}/runs/{run_id}
GET  /agents/{agent_name}/runs/{run_id}/stream
GET  /agents/{agent_name}/runs/{run_id}/trace
```

The same router object can be mounted more than once under distinct
prefixes on one app; each mount serves the same underlying store correctly.
Route names use the stable `aiq:*` namespace so generated OpenAPI
operation IDs remain distinct from similarly named host handlers.

### SSE reconnect cursor

Run SSE uses `stream_version` as its only public cursor; `global_position`
remains internal to global subscriptions and projections.

- missing `Last-Event-ID`: replay the full run;
- lower valid version: replay the missing tail;
- latest version of a completed run: close immediately;
- latest version of an active run: wait for a future stored event;
- malformed, negative, or greater-than-latest version: return `400`.

The server never silently clamps an invalid cursor.

## Health endpoint

```
GET /agents/_health
```

Framework-owned, registered under the same `route_prefix` as everything
else, at a path (`_health`) that can never collide with `{agent_name}`
routing. Response:

```json
{"status": "running", "healthy": true, "worker_error": null}
```

```json
{"status": "unhealthy", "healthy": false, "worker_error": "RuntimeError"}
```

Contract: **200** for every status except `unhealthy` (this endpoint is
informational for `stopped`/`starting`/`running`/`stopping`); **503** when
`unhealthy`. Point a host's liveness/readiness probe at this route to
detect a dead background dispatcher instead of only discovering it later
when `stop()` runs.

This endpoint describes only the AIQ background dispatcher lifecycle.
It is not a complete database, network, effect-adapter, or host-application
health check.

`worker_error` is the exception's **class name only** (e.g.
`"RuntimeError"`) -- never the exception message and never a traceback.
An arbitrary dispatcher/effect exception's message is
application-controlled and may itself contain secrets (API keys,
connection strings); there's no reliable way to redact unknown content, so
the only safe default is not exposing the message at all. The full
exception, message and traceback included, always goes to the log
(`logger.exception`), never to `.health` or this endpoint.

## Lifecycle

No background task is created in `__init__`. It starts when `lifespan`
(or `start()`) runs, and stops when `lifespan` exits (or `stop()` runs):

```python
async def start(self) -> None      # raises RuntimeError if already started or unhealthy
async def stop(self) -> None       # safe even if never started; safe to call twice
```

```python
@asynccontextmanager
async def lifespan(self, app: FastAPI): ...
```

### Health states

```python
class AIQHealth:
    status: Literal["stopped", "starting", "running", "unhealthy", "stopping"]
    worker_error: str | None

    @property
    def healthy(self) -> bool: ...  # status != "unhealthy"
```

```python
aiq.health       # -> AIQHealth, a fresh snapshot on every access
aiq.is_healthy    # -> bool, shorthand for health.status != "unhealthy"
```

The raw background `asyncio.Task` is never part of the public API --
`.health`/`.is_healthy` are the only supported way to observe lifecycle
state from outside.

### Worker failure

If `dispatcher.run_once()` (or an effect handler it calls) raises an
unexpected exception, the background task does **not** silently die while
everything else keeps reporting healthy:

- the exception is recorded and the task ends **without** re-raising it
  (so nothing ever logs an "exception was never retrieved" warning);
- `status` transitions to `"unhealthy"` immediately -- observable via
  `.health`/`.is_healthy` and the HTTP health endpoint *before* `stop()` is
  ever called;
- there is **no automatic retry or restart**. This is a deliberate MVP
  choice, not an oversight: a supervised-retry policy has to reason about
  effect idempotency and backoff, which is out of scope until there's
  concrete evidence a single-attempt worker isn't enough;
- `asyncio.CancelledError` is handled separately and is **not** treated as
  a failure -- see [Bounded shutdown](#bounded-shutdown) below. Ordinary
  `KeyboardInterrupt`/`SystemExit` are never caught here either, since the
  worker only catches `Exception`, which excludes both by Python's own
  exception hierarchy.

`worker_error` survives `stop()` -- `stop()` always lands on `status ==
"stopped"` once the task is confirmed done (nothing is "running" anymore,
so `"unhealthy"` would be a misleading *current* status), but the last
failure stays visible in `worker_error` for diagnostics until the *next*
`start()` clears it for a fresh run:

```python
await aiq.start()   # worker fails -> status="unhealthy", worker_error set
await aiq.start()   # raises RuntimeError: still unhealthy, call stop() first
await aiq.stop()    # status -> "stopped"; worker_error still set
await aiq.start()   # works: fresh task, worker_error reset to None
```

### Bounded shutdown

`stop()` is bounded by `shutdown_timeout_seconds` (default `10.0`):

1. transitions to `"stopping"`, signals the cooperative stop event;
2. waits up to `shutdown_timeout_seconds` for the worker to notice and
   exit on its own;
3. if it does, `stop()` returns normally;
4. if it times out, the worker is cancelled and *awaited* -- `stop()`
   does not return until the cancellation has actually completed, so no
   AIQ task is ever left running after `stop()` returns;
5. forced cancellation during `stop()` is normal, expected shutdown -- it
   does **not** mark the instance `"unhealthy"`; status still lands on
   `"stopped"`.

This bounds the case that motivated it: `stop()` can no longer block
forever on a stuck dispatcher or a hung effect call.

**Limitation, stated honestly:** cancellation only interrupts work that is
actually cooperating with asyncio (i.e. blocked at an `await`). It cannot
forcibly interrupt arbitrary blocking synchronous Python code (a tight CPU
loop, a blocking `socket.recv()` without a timeout, etc.) running inside a
dispatcher or effect handler -- that code has to bring its own timeout, or
run on a thread/process AIQ can actually cancel from the outside.
AIQ does not (and cannot) forcibly interrupt arbitrary Python code.

### Logging

All lifecycle events go through the standard `logging` module (logger
name `aiq.fastapi`), never `print`: worker started, worker failed
(with full traceback via `logger.exception`), graceful shutdown requested,
graceful shutdown timed out, worker cancelled, worker stopped.

## Lifespan composition

FastAPI accepts exactly one `lifespan` callable per app. If the host
already has one, compose the two explicitly instead of nesting `async with`
by hand:

```python
from aiq.fastapi import AIQ, compose_lifespans

aiq = AIQ(store=store, runtimes=runtimes)
app = FastAPI(
    lifespan=compose_lifespans(existing_lifespan, aiq.lifespan)
)
```

Entered in the given order, exited in reverse -- built on `AsyncExitStack`,
so if one lifespan's shutdown raises, the others still run theirs. AIQ
never monkey-patches `app.router.lifespan_context`; composition is always
explicit and visible at the call site.

## `create_app` compatibility

```python
def create_app(*, store, runtimes) -> FastAPI:
    integration = AIQ(store=store, runtimes=runtimes)
    app = FastAPI(lifespan=integration.lifespan)
    app.include_router(integration.router)
    return app
```

Same routes, same lifecycle, same broadcaster, same ownership wiring as
embedding -- `create_app` is sugar over `AIQ`, not a parallel
implementation that could drift from it.

## Optional dependency

```python
import aiq                          # always works, no FastAPI required
from aiq.fastapi import AIQ     # requires the `fastapi` extra
```

If FastAPI isn't installed, importing `aiq.fastapi` raises a plain
`ImportError` naming the extra to install (`pip install 'aiq[fastapi]'`)
instead of a bare `ModuleNotFoundError`. `aiq/__init__.py` never
imports `.fastapi` or `.http`, so a core-only installation stays usable.

## What this is not

- Not a required orchestration server -- AIQ runs embedded, in the
  host's own process.
- Not ownership of the host application -- the host's routes, middleware,
  and other lifespan concerns are untouched.
- Not a visualization layer -- [Flow Xray](flow-xray.md) is an optional,
  separate consumer of the trace JSON these routes (and `aiq.demo`)
  produce; nothing here depends on it.
- Not supervised or retried -- a failed worker stays `"unhealthy"` until a
  human (or an operator's own supervisor) calls `stop()`/`start()`; there
  is no backoff/retry policy in this MVP.
- Not multi-process aware -- one `AIQ` instance's catch-up task polls
  from a single process. Coordinating multiple worker processes over the
  same store is not implemented.
- Not exactly-once external execution -- durable effects remain at-least-once
  and require stable operation IDs plus idempotent external adapters.
- Not a production-grade distributed durability claim -- bounded shutdown
  and explicit health make a single-process embedding safer to operate,
  they do not turn this into a distributed system.
