"""Embeddable FastAPI integration for Agentlog.

This is the canonical implementation of the HTTP/SSE boundary: routes,
broadcaster, catch-up lifecycle, and ownership wiring all live here exactly
once. `agentlog.http.create_app()` is a thin convenience wrapper around
`Agentlog` defined in this module, not a second implementation.

FastAPI is an optional dependency: importing plain `agentlog` never imports
this module, and importing this module without FastAPI installed raises a
clear `ImportError` instead of a bare `ModuleNotFoundError`.

Two supported styles:

    # Standalone convenience
    from agentlog.http import create_app
    app = create_app(store=store, runtimes={"energy-assistant": runtime})

    # Embedding into an existing application
    from fastapi import FastAPI
    from agentlog.fastapi import Agentlog

    agentlog = Agentlog(store=store, runtimes={"energy-assistant": runtime})
    app = FastAPI(lifespan=agentlog.lifespan)
    app.include_router(agentlog.router, prefix="/api")

If the host application already has its own lifespan, compose the two with
`compose_lifespans` rather than nesting `async with` by hand:

    from agentlog.fastapi import Agentlog, compose_lifespans

    agentlog = Agentlog(store=store, runtimes=runtimes)
    app = FastAPI(lifespan=compose_lifespans(existing_lifespan, agentlog.lifespan))
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

try:
    from fastapi import APIRouter, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from pydantic import BaseModel, Field
except ModuleNotFoundError as error:  # pragma: no cover - exercised via core-only smoke
    raise ImportError(
        "agentlog.fastapi requires the optional 'fastapi' extra. "
        "Install it with: pip install 'agentlog[fastapi]'"
    ) from error

from .application import AgentRuntime, default_serialize_state
from .attempts import EffectAttemptStore
from .core import Event, EventEnvelope, EventStore, JsonValue, VersionConflictError
from .runtime import (
    AgentDefinition,
    DefinitionMismatchError,
    DurableDispatcher,
    DurableEffectDispatcher,
)
from .streams import agent_owns_stream, run_stream_id
from .trace import RunNotFoundError, TraceService, trace_to_json

if TYPE_CHECKING:
    from .framework import Agent

logger = logging.getLogger("agentlog.fastapi")

POLL_INTERVAL_SECONDS = 5.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0

AgentlogStatus = Literal["stopped", "starting", "running", "unhealthy", "stopping"]

__all__ = [
    "Agentlog",
    "AgentlogApplication",
    "AgentlogHealth",
    "AgentRuntime",
    "CreateRunResponse",
    "compose_lifespans",
    "POLL_INTERVAL_SECONDS",
    "SHUTDOWN_TIMEOUT_SECONDS",
]


@dataclass(frozen=True, slots=True)
class AgentlogHealth:
    """A point-in-time snapshot -- not a live view. Re-read `agentlog.health`
    to get the current state; holding onto one of these does not update."""

    status: AgentlogStatus
    worker_error: str | None

    @property
    def healthy(self) -> bool:
        return self.status != "unhealthy"

class _Broadcaster:
    """Wakes SSE waiters after a commit. Never a source of truth by itself."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._generation = 0

    async def notify(self) -> None:
        async with self._condition:
            self._generation += 1
            self._condition.notify_all()

    async def generation(self) -> int:
        async with self._condition:
            return self._generation

    async def wait_for_change_since(self, baseline: int, *, timeout: float) -> None:
        """Returns immediately if a notify already landed after `baseline` was taken.

        `baseline` must be captured (via `generation()`) *before* the caller re-reads
        the store, not right before this call — otherwise a notify that lands while
        the caller is still reading/sending is silently absorbed as the new baseline
        and the caller waits out the full timeout for nothing.
        """
        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._generation != baseline),
                    timeout=timeout,
                )
            except TimeoutError:
                pass


class CreateRunRequest(BaseModel):
    """Canonical create-run body: generic, agent-agnostic. `run_id` lets a
    caller pick its own id (rejected with 409 if it already has a stream);
    `metadata` is merged into the `RunCreated` event's own metadata.
    Neither field carries any domain-event opinion -- that's the framework
    command endpoint's job (`POST .../runs/{run_id}/commands/{command_name}`)."""

    run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateRunResponse(BaseModel):
    agent_name: str
    run_id: str
    status: str
    stream_version: int


class CommandEventResponse(BaseModel):
    event_id: str
    event_type: str
    stream_version: int


class CommandResponse(BaseModel):
    agent_name: str
    run_id: str
    accepted: bool
    events: list[CommandEventResponse]


def _subscription_name(
    agent_name: str, definition_version: str | None, kind: str
) -> str:
    """Checkpoint identity belongs to (agent_name, definition_version,
    dispatcher_kind), not just agent_name -- otherwise one version's
    dispatcher skipping a different version's stream (see
    `DefinitionMismatchError` in `runtime.py`) permanently advances the
    *same* checkpoint that the other version would need to ever continue
    that stream itself. Unversioned agents (definition_version is None)
    keep the original bare naming -- changing it would reset every
    existing deployment's persisted checkpoint the moment this feature
    shipped, for hosts that never opted into versioning at all."""
    if definition_version is None:
        return f"{agent_name}:{kind}"
    return f"{agent_name}:{definition_version}:{kind}"


def _normalize_route_prefix(prefix: str) -> str:
    normalized = "/" + prefix.strip("/")
    if normalized == "/":
        raise ValueError("route_prefix must not be empty")
    return normalized


async def _load_run_history(
    store: EventStore,
    *,
    agent_name: str,
    run_id: str,
    agent: AgentDefinition[Any],
) -> tuple[EventEnvelope, ...]:
    history = await store.load(run_stream_id(agent_name, run_id))
    if not history:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        agent.assert_definition_matches(history)
    except DefinitionMismatchError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return history


def _normalize_command_events(
    events: Sequence[Event],
    *,
    run_id: str,
    command_id: str,
) -> tuple[Event, ...]:
    normalized: list[Event] = []
    previous_event: Event | None = None
    for event in events:
        metadata = dict(event.metadata)
        correlation_id = metadata.get("correlation_id")
        if correlation_id is not None and correlation_id != run_id:
            raise ValueError("command event correlation_id conflicts with run_id")
        metadata["correlation_id"] = run_id
        metadata["command_id"] = command_id

        if previous_event is not None:
            expected_causation_id = str(previous_event.event_id)
            causation_id = metadata.get("causation_id")
            if causation_id is not None and causation_id != expected_causation_id:
                raise ValueError(
                    "command event causation_id conflicts with batch order"
                )
            metadata["causation_id"] = expected_causation_id

        normalized_event = Event(
            event.event_type,
            event.data,
            metadata,
            event_id=event.event_id,
        )
        normalized.append(normalized_event)
        previous_event = normalized_event
    return tuple(normalized)


class Agentlog:
    """Embeddable FastAPI integration: one instance owns one router, one
    background catch-up task, and one SSE broadcaster over one event store.

    No background task is created in `__init__` -- it starts when
    `lifespan` is entered (or `start()` is called explicitly) and stops
    when `lifespan` exits (or `stop()` is called). The host application
    remains owned by the caller: this class only ever registers routes on
    its own `APIRouter` and manages its own background task, never the
    host `FastAPI` app itself.
    """

    def __init__(
        self,
        *,
        store: EventStore,
        runtimes: Mapping[str, AgentRuntime],
        route_prefix: str = "/agents",
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        shutdown_timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS,
        attempt_store: EffectAttemptStore | None = None,
    ) -> None:
        for name, runtime in runtimes.items():
            if runtime.agent.name != name:
                raise ValueError(
                    f"runtimes key {name!r} must match its AgentDefinition.name "
                    f"{runtime.agent.name!r} -- stream ownership and routing are "
                    "keyed by this name and must not silently diverge from the "
                    "agent's own identity"
                )
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be > 0")

        self._store = store
        self._runtimes = dict(runtimes)
        self._poll_interval_seconds = poll_interval_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._status: AgentlogStatus = "stopped"
        self._worker_error: BaseException | None = None
        self._broadcaster = _Broadcaster()
        self._trace_service = TraceService(
            store=store,
            agents={name: runtime.agent for name, runtime in self._runtimes.items()},
        )
        self._reaction_dispatchers = [
            DurableDispatcher(
                agent=runtime.agent,
                store=store,
                subscription_name=_subscription_name(
                    name, runtime.definition_version, "reactions"
                ),
                owns_stream=partial(agent_owns_stream, name),
            )
            for name, runtime in self._runtimes.items()
        ]
        self._effect_dispatchers = [
            DurableEffectDispatcher(
                agent=runtime.agent,
                store=store,
                effects=runtime.effects,
                context=runtime.context,
                subscription_name=_subscription_name(
                    name, runtime.definition_version, "effects"
                ),
                owns_stream=partial(agent_owns_stream, name),
                attempt_store=attempt_store,
            )
            for name, runtime in self._runtimes.items()
        ]

        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

        self._router = APIRouter(prefix=_normalize_route_prefix(route_prefix))
        self._register_routes()

    @property
    def router(self) -> APIRouter:
        return self._router

    @property
    def health(self) -> AgentlogHealth:
        """A fresh snapshot of current lifecycle state -- never the raw
        background `asyncio.Task`, which is not part of the public API."""
        return AgentlogHealth(
            status=self._status,
            worker_error=self._worker_error_summary(),
        )

    @property
    def is_healthy(self) -> bool:
        return self._status != "unhealthy"

    def _worker_error_summary(self) -> str | None:
        if self._worker_error is None:
            return None
        # Exception *class name* only -- never `str(error)`. An arbitrary
        # dispatcher/effect exception's message is application-controlled
        # and may itself contain secrets (connection strings, API keys,
        # etc.); there is no reliable way to redact unknown content, so the
        # only safe default is to not expose the message at all. The full
        # exception (message + traceback) is always in the log
        # (logger.exception), never in .health or the HTTP health endpoint.
        return type(self._worker_error).__name__

    def _runtime_for(self, agent_name: str) -> AgentRuntime:
        runtime = self._runtimes.get(agent_name)
        if runtime is None:
            raise HTTPException(status_code=404, detail="unknown agent")
        return runtime

    async def create_run(
        self,
        agent_name: str,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> CreateRunResponse:
        """Canonical, agent-agnostic run creation: appends only `RunCreated`.

        No domain event is ever created here -- a declared agent's own
        `@agent.command(...)` handler (via `POST .../commands/{command_name}`)
        is the only way an HTTP caller adds a domain event. This is the one
        implementation the `POST /{agent_name}/runs` route below and any
        compatibility wrapper (e.g. `agentlog.http`'s legacy chat endpoint)
        both call -- neither reimplements it.
        """
        runtime = self._runtime_for(agent_name)
        resolved_run_id = run_id or str(uuid4())
        event_metadata: dict[str, JsonValue] = {"correlation_id": resolved_run_id}
        if metadata:
            event_metadata.update(
                {key: value for key, value in metadata.items() if key != "correlation_id"}
            )
        run_created = Event(
            "RunCreated",
            {"agent": agent_name, "definition_version": runtime.definition_version},
            event_metadata,
        )
        try:
            appended = await self._store.append(
                run_stream_id(agent_name, resolved_run_id), -1, [run_created]
            )
        except VersionConflictError as error:
            raise HTTPException(
                status_code=409, detail="run_id already exists"
            ) from error
        await self._broadcaster.notify()
        return CreateRunResponse(
            agent_name=agent_name,
            run_id=resolved_run_id,
            status="active",
            stream_version=appended[-1].stream_version,
        )

    async def create_run_with_initial_event(
        self,
        agent_name: str,
        *,
        event_type: str,
        data: Mapping[str, JsonValue],
    ) -> CreateRunResponse:
        """Legacy one-shot creation: `RunCreated` plus a single event it
        causes, appended atomically. Exists only for backward compatibility
        with callers built against the old chat-specific create-run
        contract (see `agentlog.http`'s `/runs/chat` endpoint) -- the
        canonical `create_run` above no longer does this itself, and this
        method is not exposed as a route on `self.router`.
        """
        runtime = self._runtime_for(agent_name)
        run_id = str(uuid4())
        run_created = Event(
            "RunCreated",
            {"agent": agent_name, "definition_version": runtime.definition_version},
            {"correlation_id": run_id},
        )
        initial_event = Event(
            event_type,
            data,
            {
                "correlation_id": run_id,
                "causation_id": str(run_created.event_id),
            },
        )
        appended = await self._store.append(
            run_stream_id(agent_name, run_id), -1, [run_created, initial_event]
        )
        await self._broadcaster.notify()
        return CreateRunResponse(
            agent_name=agent_name,
            run_id=run_id,
            status="active",
            stream_version=appended[-1].stream_version,
        )

    def _register_routes(self) -> None:
        router = self._router
        store = self._store

        @router.get("/_health", name="agentlog:health")
        async def health_check() -> JSONResponse:
            """Framework-owned liveness/readiness endpoint. 200 for every
            status except `unhealthy`, which is 503 -- a dead background
            worker must be visible here, not just discoverable later via
            `stop()`. `worker_error` is class name + sanitized message
            only, never a traceback."""
            snapshot = self.health
            return JSONResponse(
                content={
                    "status": snapshot.status,
                    "healthy": snapshot.healthy,
                    "worker_error": snapshot.worker_error,
                },
                status_code=200 if snapshot.healthy else 503,
            )

        @router.post(
            "/{agent_name}/runs",
            response_model=CreateRunResponse,
            name="agentlog:create_run",
        )
        async def create_run(
            agent_name: str, body: CreateRunRequest | None = None
        ) -> CreateRunResponse:
            return await self.create_run(
                agent_name,
                run_id=body.run_id if body else None,
                metadata=body.metadata if body else None,
            )

        @router.post(
            "/{agent_name}/runs/{run_id}/commands/{command_name}",
            response_model=CommandResponse,
            name="agentlog:command",
        )
        async def submit_command(
            agent_name: str,
            run_id: str,
            command_name: str,
            payload: dict[str, Any],
        ) -> CommandResponse:
            runtime = self._runtime_for(agent_name)
            if runtime.command_handler is None:
                raise HTTPException(status_code=404, detail="unknown command")

            stream_id = run_stream_id(agent_name, run_id)
            command_events: tuple[Event, ...] | None = None
            command_id = str(uuid4())

            for attempt in range(3):
                history = await _load_run_history(
                    store,
                    agent_name=agent_name,
                    run_id=run_id,
                    agent=runtime.agent,
                )
                latest_stream_version = history[-1].stream_version
                if runtime.agent.is_terminal(
                    history,
                    through_version=latest_stream_version,
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="run is not accepting commands",
                    )

                if command_events is None:
                    try:
                        produced = tuple(runtime.command_handler(command_name, payload))
                    except KeyError as error:
                        if (
                            error.args
                            and isinstance(error.args[0], str)
                            and error.args[0].startswith("unknown command:")
                        ):
                            raise HTTPException(
                                status_code=404,
                                detail="unknown command",
                            ) from error
                        raise HTTPException(
                            status_code=422,
                            detail="invalid command payload",
                        ) from error
                    except (TypeError, ValueError) as error:
                        raise HTTPException(
                            status_code=422,
                            detail="invalid command payload",
                        ) from error
                    if not produced:
                        raise HTTPException(
                            status_code=422,
                            detail="command produced no events",
                        )
                    try:
                        command_events = _normalize_command_events(
                            produced,
                            run_id=run_id,
                            command_id=command_id,
                        )
                    except ValueError as error:
                        raise HTTPException(
                            status_code=422,
                            detail=str(error),
                        ) from error

                try:
                    appended = await store.append(
                        stream_id,
                        latest_stream_version,
                        command_events,
                    )
                except VersionConflictError as error:
                    if attempt == 2:
                        raise HTTPException(
                            status_code=409,
                            detail="run changed while accepting command",
                        ) from error
                    continue

                await self._broadcaster.notify()
                return CommandResponse(
                    agent_name=agent_name,
                    run_id=run_id,
                    accepted=True,
                    events=[
                        CommandEventResponse(
                            event_id=str(envelope.event.event_id),
                            event_type=envelope.event.event_type,
                            stream_version=envelope.stream_version,
                        )
                        for envelope in appended
                    ],
                )

            raise AssertionError("bounded command retry loop exhausted")

        @router.get(
            "/{agent_name}/runs/{run_id}",
            name="agentlog:read_run",
        )
        async def read_run(agent_name: str, run_id: str) -> dict[str, Any]:
            runtime = self._runtime_for(agent_name)
            history = await _load_run_history(
                store, agent_name=agent_name, run_id=run_id, agent=runtime.agent
            )
            state = runtime.agent.rebuild(history)
            return {
                "run_id": run_id,
                "state": runtime.serialize_state(state),
            }

        @router.get(
            "/{agent_name}/runs/{run_id}/trace",
            name="agentlog:get_trace",
        )
        async def get_trace(agent_name: str, run_id: str) -> dict[str, Any]:
            try:
                trace = await self._trace_service.export(agent_name, run_id)
            except RunNotFoundError as error:
                raise HTTPException(status_code=404, detail="run not found") from error
            return trace_to_json(trace)

        @router.get(
            "/{agent_name}/runs/{run_id}/stream",
            name="agentlog:stream_run",
        )
        async def stream_run(
            agent_name: str,
            run_id: str,
            request: Request,
        ) -> StreamingResponse:
            runtime = self._runtime_for(agent_name)
            history = await _load_run_history(
                store,
                agent_name=agent_name,
                run_id=run_id,
                agent=runtime.agent,
            )
            stream_id = run_stream_id(agent_name, run_id)
            latest_stream_version = history[-1].stream_version
            run_is_terminal = runtime.agent.is_terminal(
                history,
                through_version=latest_stream_version,
            )

            last_event_id = request.headers.get("last-event-id")
            try:
                cursor = int(last_event_id) if last_event_id is not None else -1
            except ValueError as error:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative integer",
                ) from error
            if last_event_id is not None and cursor < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative integer",
                )
            if cursor > latest_stream_version:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID exceeds the latest stream version",
                )

            async def event_source() -> AsyncIterator[str]:
                nonlocal cursor
                if cursor == latest_stream_version and run_is_terminal:
                    return
                while True:
                    if await request.is_disconnected():
                        return
                    # Snapshot the generation *before* reading, so a notify() that
                    # lands while we're reading/sending below is never silently
                    # absorbed.
                    baseline = await self._broadcaster.generation()
                    tail = await store.load(stream_id, after_version=cursor)
                    terminal_sent = False
                    for envelope in tail:
                        cursor = envelope.stream_version
                        payload = {
                            "event_type": envelope.event.event_type,
                            "data": default_serialize_state(envelope.event.data),
                        }
                        yield (
                            f"id: {envelope.stream_version}\n"
                            f"event: {envelope.event.event_type}\n"
                            f"data: {json.dumps(payload)}\n\n"
                        )
                        if runtime.agent.is_terminal(
                            (envelope,),
                            through_version=envelope.stream_version,
                        ):
                            terminal_sent = True
                    if terminal_sent:
                        return
                    await self._broadcaster.wait_for_change_since(
                        baseline,
                        timeout=self._poll_interval_seconds,
                    )

            return StreamingResponse(
                event_source(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

    async def _catch_up_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            progressed = False
            for dispatcher in (*self._reaction_dispatchers, *self._effect_dispatchers):
                if await dispatcher.run_once():
                    progressed = True
            if progressed:
                await self._broadcaster.notify()
            else:
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=self._poll_interval_seconds
                    )
                except TimeoutError:
                    pass

    async def _run_worker(self, stop: asyncio.Event) -> None:
        logger.info("agentlog: worker started")
        try:
            await self._catch_up_forever(stop)
        except asyncio.CancelledError:
            # Expected path: our own stop() cancelled us after the
            # graceful-shutdown timeout elapsed. Must propagate per asyncio
            # cancellation rules, and must *not* mark this as a failure --
            # forced cancellation during our own shutdown is not the same
            # thing as the dispatcher loop breaking on its own.
            raise
        except Exception as error:  # noqa: BLE001 - the whole point: capture *any* dispatcher/effect failure
            logger.exception("agentlog: worker failed")
            self._worker_error = error
            self._status = "unhealthy"
            return
        logger.info("agentlog: worker stopped")

    async def start(self) -> None:
        """Start the background catch-up task.

        Raises `RuntimeError` if already running, and a distinct
        `RuntimeError` if the previous worker failed and is still
        `unhealthy` -- call `stop()` first in either case. This `start()`
        call then clears the prior `worker_error`, giving the new worker a
        clean slate.
        """
        if self._status == "unhealthy":
            raise RuntimeError(
                "Agentlog worker previously failed and is unhealthy; "
                "call stop() before starting again"
            )
        if self._task is not None:
            raise RuntimeError("Agentlog is already started")
        self._status = "starting"
        self._worker_error = None  # fresh start: clear the prior diagnostic
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_worker(self._stop_event))
        self._status = "running"

    async def stop(self) -> None:
        """Stop the background catch-up task.

        Safe to call even if never started, and safe to call more than
        once. Bounded: waits up to `shutdown_timeout_seconds` for the
        worker to notice the cooperative stop signal and exit on its own;
        if it doesn't, the worker is cancelled and awaited so that no
        Agentlog task is ever left running after `stop()` returns.

        A worker failure recorded before or during `stop()` is preserved in
        `worker_error` for diagnostics -- `stop()` itself always completes
        without raising because of it, and always lands on `status ==
        "stopped"` once the task is confirmed done: "unhealthy" describes a
        worker that is (or was, moments ago) supposed to be running and
        isn't; once `stop()` has run, nothing is supposed to be running at
        all, so "stopped" is the accurate current state even though the
        last run ended in failure. Call `start()` to get a clean slate
        (`worker_error` reset to `None`).
        """
        if self._task is None:
            return
        task = self._task
        stop_event = self._stop_event
        assert stop_event is not None

        self._status = "stopping"
        logger.info("agentlog: graceful shutdown requested")
        stop_event.set()

        forced_cancellation = False
        try:
            # shield: a timeout here must not cancel `task` as a side effect
            # of wait_for's own bookkeeping -- cancellation is only ever
            # triggered explicitly, below, so the two code paths (bounded
            # wait vs. forced cancel) stay unambiguous.
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self._shutdown_timeout_seconds
            )
        except TimeoutError:
            forced_cancellation = True
            logger.warning(
                "agentlog: graceful shutdown timed out after %.1fs; cancelling worker",
                self._shutdown_timeout_seconds,
            )
            task.cancel()
        except Exception:
            # A worker failure already recorded itself into .health inside
            # _run_worker and returned normally, so this branch is not
            # expected to trigger in practice -- kept as a defensive
            # backstop so a future change here can never turn into a stuck
            # or re-raising stop().
            pass

        if not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if forced_cancellation:
            logger.info("agentlog: worker cancelled")

        self._status = "stopped"
        self._task = None
        self._stop_event = None
        logger.info("agentlog: worker stopped")

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        """Use as `FastAPI(lifespan=agentlog.lifespan)`, or combine with an
        existing host lifespan via `compose_lifespans()`."""
        await self.start()
        try:
            yield
        finally:
            await self.stop()


class AgentlogApplication:
    def __init__(
        self,
        *,
        store: EventStore,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        shutdown_timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be > 0")
        self._store = store
        self._poll_interval_seconds = poll_interval_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._runtimes: dict[str, AgentRuntime] = {}
        self._agentlog: Agentlog | None = None

    def register(
        self,
        agent: Agent,
        *,
        context: object = None,
        resources: object = None,
    ) -> None:
        if self._agentlog is not None:
            raise RuntimeError(
                "AgentlogApplication registration is closed after build or startup"
            )
        if agent.name in self._runtimes:
            raise ValueError(f"agent {agent.name!r} is already registered")
        if context is not None and resources is not None:
            raise TypeError("pass either context or resources, not both")
        resolved_resources = resources if resources is not None else context
        self._runtimes[agent.name] = agent.build_runtime(context=resolved_resources)

    def build(self) -> Agentlog:
        if self._agentlog is None:
            self._agentlog = Agentlog(
                store=self._store,
                runtimes=self._runtimes,
                poll_interval_seconds=self._poll_interval_seconds,
                shutdown_timeout_seconds=self._shutdown_timeout_seconds,
            )
        return self._agentlog

    @property
    def router(self) -> APIRouter:
        return self.build().router

    async def start(self) -> None:
        await self.build().start()

    async def stop(self) -> None:
        if self._agentlog is not None:
            await self._agentlog.stop()

    @asynccontextmanager
    async def lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        async with self.build().lifespan(app):
            yield


def compose_lifespans(
    *lifespans: Callable[[FastAPI], Any],
) -> Callable[[FastAPI], Any]:
    """Combine multiple FastAPI lifespan callables into one.

    Entered in the given order, exited in reverse -- so a host app's
    existing lifespan and `Agentlog.lifespan` can share the single
    `FastAPI(lifespan=...)` slot:

        app = FastAPI(lifespan=compose_lifespans(existing_lifespan, agentlog.lifespan))

    Built on `AsyncExitStack`, so if one lifespan's shutdown raises, the
    others still run their shutdown -- host cleanup isn't skipped just
    because Agentlog's shutdown (or vice versa) failed, and vice versa.
    """

    @asynccontextmanager
    async def composed(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for lifespan in lifespans:
                await stack.enter_async_context(lifespan(app))
            yield

    return composed
