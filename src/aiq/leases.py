"""Effect ownership leases and fenced-commit storage contract."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from .attempts import EffectDispatchAttempt
from .core import Event, EventEnvelope, EventStore


class LeaseLostError(RuntimeError):
    """The caller no longer owns a live lease for this effect request."""


@dataclass(frozen=True, slots=True)
class EffectLeaseOptions:
    worker_id: str
    ttl_seconds: float = 30.0
    renewal_interval_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id must not be empty")
        if self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        if self.renewal_interval_seconds <= 0:
            raise ValueError("renewal_interval_seconds must be > 0")
        if self.renewal_interval_seconds >= self.ttl_seconds:
            raise ValueError("renewal_interval_seconds must be less than ttl_seconds")


@dataclass(frozen=True, slots=True)
class EffectLease:
    lease_id: UUID
    subscription_name: str
    request_global_position: int
    operation_id: str
    stream_id: str
    request_event_type: str
    worker_id: str
    fencing_token: int
    lease_expires_at: datetime
    attempt: EffectDispatchAttempt

    def __post_init__(self) -> None:
        if not self.subscription_name:
            raise ValueError("subscription_name must not be empty")
        if self.request_global_position <= 0:
            raise ValueError("request_global_position must be positive")
        if not self.operation_id or not self.stream_id or not self.request_event_type:
            raise ValueError("effect lease request identity must not be empty")
        if not self.worker_id:
            raise ValueError("worker_id must not be empty")
        if self.fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if self.lease_expires_at.tzinfo is None:
            raise ValueError("lease_expires_at must be timezone-aware")


ClaimStatus = Literal[
    "acquired",
    "busy",
    "terminal",
    "already_completed",
]


@dataclass(frozen=True, slots=True)
class EffectClaimResult:
    status: ClaimStatus
    lease: EffectLease | None = None

    def __post_init__(self) -> None:
        if (self.status == "acquired") != (self.lease is not None):
            raise ValueError("only an acquired claim carries a lease")


ConfirmationStatus = Literal["confirmed", "terminal", "already_completed"]


@dataclass(frozen=True, slots=True)
class EffectClaimConfirmation:
    status: ConfirmationStatus


LeaseObservationKind = Literal[
    "claim_acquired",
    "busy",
    "expiry",
    "renewal",
    "takeover",
    "stale_ownership",
    "stale_commit_rejection",
]


@dataclass(frozen=True, slots=True)
class EffectLeaseObservation:
    observation_sequence: int
    observed_at: datetime
    observation_kind: LeaseObservationKind
    subscription_name: str
    request_global_position: int
    operation_id: str
    stream_id: str
    request_event_type: str
    worker_id: str
    lease_id: UUID
    fencing_token: int
    attempt_id: UUID | None = None
    attempt_number: int | None = None


@runtime_checkable
class FencedEffectStore(EventStore, Protocol):
    async def try_claim_effect(
        self,
        *,
        subscription_name: str,
        expected_checkpoint: int,
        request_global_position: int,
        operation_id: str,
        stream_id: str,
        request_event_type: str,
        worker_id: str,
        lease_ttl_seconds: float,
        terminal_event_types: frozenset[str],
    ) -> EffectClaimResult: ...

    async def confirm_effect_claim(
        self,
        lease: EffectLease,
        *,
        terminal_event_types: frozenset[str],
    ) -> EffectClaimConfirmation: ...

    async def renew_effect_claim(
        self,
        lease: EffectLease,
        *,
        lease_ttl_seconds: float,
    ) -> EffectLease: ...

    async def release_effect_claim(self, lease: EffectLease) -> bool: ...

    async def commit_fenced_subscription_batch(
        self,
        lease: EffectLease,
        *,
        expected_checkpoint: int,
        stream_id: str,
        expected_stream_version: int,
        events: Sequence[Event],
        new_checkpoint: int,
        terminal_event_types: frozenset[str],
    ) -> tuple[EventEnvelope, ...]: ...
