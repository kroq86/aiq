"""Compatibility wrapper around `aiq.fastapi`.

`create_app()` is a standalone convenience: it builds one `AIQ`
integration and wraps it in its own `FastAPI` app. There is exactly one
implementation of routes, broadcaster, lifecycle and ownership wiring --
it lives in `aiq.fastapi.AIQ`; this module does not duplicate it.

For embedding AIQ into an existing application (host owns the
`FastAPI` app, its own lifespan, and its own other routes), use
`aiq.fastapi.AIQ` directly instead of this module.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .attempts import EffectAttemptStore
from .core import EventStore
from .leases import EffectLeaseOptions
from .fastapi import (
    AgentRuntime,
    AIQ,
    CreateRunResponse,
    POLL_INTERVAL_SECONDS,
    SHUTDOWN_TIMEOUT_SECONDS,
)

__all__ = ["AgentRuntime", "create_app", "POLL_INTERVAL_SECONDS", "SHUTDOWN_TIMEOUT_SECONDS"]


class _ChatRunRequest(BaseModel):
    message: str = Field(min_length=1)


def create_app(
    *,
    store: EventStore,
    runtimes: Mapping[str, AgentRuntime],
    shutdown_timeout_seconds: float = SHUTDOWN_TIMEOUT_SECONDS,
    attempt_store: EffectAttemptStore | None = None,
    lease_options: EffectLeaseOptions | None = None,
) -> FastAPI:
    """Standalone convenience: one `AIQ` integration, its own
    dedicated `FastAPI` app, routes at `/agents/...` with no extra prefix.

    For mounting into an existing application instead, construct
    `aiq.fastapi.AIQ` directly and use `.lifespan`/`.router`.
    """
    integration = AIQ(
        store=store,
        runtimes=runtimes,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
        attempt_store=attempt_store,
        lease_options=lease_options,
    )
    app = FastAPI(lifespan=integration.lifespan)
    app.include_router(integration.router)

    @app.post(
        "/agents/{agent_name}/runs/chat",
        response_model=CreateRunResponse,
        name="aiq:create_chat_run",
    )
    async def create_chat_run(agent_name: str, body: _ChatRunRequest) -> CreateRunResponse:
        """Legacy one-shot compatibility endpoint: `RunCreated` plus a
        `UserMessageAdded` it causes, in one call -- for callers built
        against the old chat-specific create-run contract. The canonical
        `POST /agents/{agent_name}/runs` (registered above via
        `integration.router`) no longer does this itself; this route only
        composes `AIQ.create_run_with_initial_event`, the one
        implementation of that legacy behavior, which lives in
        `aiq.fastapi` alongside everything else.
        """
        return await integration.create_run_with_initial_event(
            agent_name,
            event_type="UserMessageAdded",
            data={"text": body.message},
        )

    return app
