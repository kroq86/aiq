"""Compatibility wrapper around `agentlog.fastapi`.

`create_app()` is a standalone convenience: it builds one `Agentlog`
integration and wraps it in its own `FastAPI` app. There is exactly one
implementation of routes, broadcaster, lifecycle and ownership wiring --
it lives in `agentlog.fastapi.Agentlog`; this module does not duplicate it.

For embedding Agentlog into an existing application (host owns the
`FastAPI` app, its own lifespan, and its own other routes), use
`agentlog.fastapi.Agentlog` directly instead of this module.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .core import EventStore
from .fastapi import (
    AgentRuntime,
    Agentlog,
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
) -> FastAPI:
    """Standalone convenience: one `Agentlog` integration, its own
    dedicated `FastAPI` app, routes at `/agents/...` with no extra prefix.

    For mounting into an existing application instead, construct
    `agentlog.fastapi.Agentlog` directly and use `.lifespan`/`.router`.
    """
    integration = Agentlog(
        store=store,
        runtimes=runtimes,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )
    app = FastAPI(lifespan=integration.lifespan)
    app.include_router(integration.router)

    @app.post(
        "/agents/{agent_name}/runs/chat",
        response_model=CreateRunResponse,
        name="agentlog:create_chat_run",
    )
    async def create_chat_run(agent_name: str, body: _ChatRunRequest) -> CreateRunResponse:
        """Legacy one-shot compatibility endpoint: `RunCreated` plus a
        `UserMessageAdded` it causes, in one call -- for callers built
        against the old chat-specific create-run contract. The canonical
        `POST /agents/{agent_name}/runs` (registered above via
        `integration.router`) no longer does this itself; this route only
        composes `Agentlog.create_run_with_initial_event`, the one
        implementation of that legacy behavior, which lives in
        `agentlog.fastapi` alongside everything else.
        """
        return await integration.create_run_with_initial_event(
            agent_name,
            event_type="UserMessageAdded",
            data={"text": body.message},
        )

    return app
