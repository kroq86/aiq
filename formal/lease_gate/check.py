"""Bounded safety model for SQLite effect leases and fencing."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, replace

BOUND = 8
WORKERS = ("A", "B")
MUTANTS = {
    "non_atomic_claim": "AtMostOneValidOwner",
    "missing_attempt_gate": "HandlerRequiresAttempt",
    "token_reuse": "FencingTokenMonotonic",
    "stale_commit": "StaleOrExpiredCannotCommit",
    "expired_commit": "StaleOrExpiredCannotCommit",
    "renew_after_expiry": "RenewRequiresValidLease",
}
WITNESSES = frozenset(
    {
        "Claimed(A)",
        "AttemptRecorded(A)",
        "BusyRejected(B)",
        "Renewed(A)",
        "Expired(A)",
        "Claimed(B)",
        "HandlerStarted(A)",
        "OwnershipConfirmed(A)",
        "Committed(A)",
        "StaleRejected(A)",
    }
)


@dataclass(frozen=True, slots=True)
class State:
    owners: frozenset[str]
    token: int
    expiry: str
    local_tokens: tuple[int, int]
    attempts: frozenset[tuple[str, int]]
    confirmations: frozenset[tuple[str, int]]
    handlers: frozenset[tuple[str, int]]
    committed: bool
    claim_tokens: tuple[int, ...]
    invalid_commit: bool
    invalid_renewal: bool
    history: tuple[str, ...]


INITIAL = State(
    owners=frozenset(),
    token=0,
    expiry="none",
    local_tokens=(0, 0),
    attempts=frozenset(),
    confirmations=frozenset(),
    handlers=frozenset(),
    committed=False,
    claim_tokens=(),
    invalid_commit=False,
    invalid_renewal=False,
    history=(),
)


def _local_token(state: State, worker: str) -> int:
    return state.local_tokens[WORKERS.index(worker)]


def _set_local_token(state: State, worker: str, token: int) -> tuple[int, int]:
    tokens = list(state.local_tokens)
    tokens[WORKERS.index(worker)] = token
    return tokens[0], tokens[1]


def successors(
    state: State, *, mutant: str | None
) -> tuple[tuple[str, State], ...]:
    if state.committed:
        return ()
    options: list[tuple[str, State]] = []
    for worker in WORKERS:
        other = WORKERS[1 - WORKERS.index(worker)]
        claimable = not state.owners or state.expiry == "expired"
        if claimable:
            token = (
                state.token
                if mutant == "token_reuse" and state.token > 0
                else state.token + 1
            )
            attempt = (
                frozenset()
                if mutant == "missing_attempt_gate"
                else frozenset({(worker, token)})
            )
            history = state.history + (
                f"Claimed({worker})",
                *(
                    ()
                    if not attempt
                    else (f"AttemptRecorded({worker})",)
                ),
            )
            options.append(
                (
                    f"claim_{worker}",
                    replace(
                        state,
                        owners=frozenset({worker}),
                        token=token,
                        expiry="valid",
                        local_tokens=_set_local_token(state, worker, token),
                        attempts=state.attempts | attempt,
                        claim_tokens=state.claim_tokens + (token,),
                        history=history,
                    ),
                )
            )
        elif worker not in state.owners:
            options.append(
                (
                    f"busy_{worker}",
                    replace(
                        state,
                        history=state.history + (f"BusyRejected({worker})",),
                    ),
                )
            )
            if mutant == "non_atomic_claim":
                options.append(
                    (
                        f"racy_claim_{worker}",
                        replace(
                            state,
                            owners=state.owners | {worker},
                            local_tokens=_set_local_token(
                                state, worker, state.token
                            ),
                            history=state.history + (f"Claimed({worker})",),
                        ),
                    )
                )

        token = _local_token(state, worker)
        is_current = (
            state.owners == frozenset({worker})
            and token == state.token
            and state.expiry == "valid"
        )
        if is_current:
            options.append(
                (
                    f"renew_{worker}",
                    replace(
                        state,
                        history=state.history + (f"Renewed({worker})",),
                    ),
                )
            )
        elif mutant == "renew_after_expiry" and token and state.expiry == "expired":
            options.append(
                (
                    f"invalid_renew_{worker}",
                    replace(
                        state,
                        invalid_renewal=True,
                        history=state.history + (f"Renewed({worker})",),
                    ),
                )
            )

        handler_key = (worker, token)
        if is_current and handler_key not in state.confirmations:
            options.append(
                (
                    f"confirm_{worker}",
                    replace(
                        state,
                        confirmations=state.confirmations | {handler_key},
                        history=state.history
                        + (f"OwnershipConfirmed({worker})",),
                    ),
                )
            )
        can_start = is_current and handler_key in state.confirmations and (
            handler_key in state.attempts or mutant == "missing_attempt_gate"
        )
        if can_start and handler_key not in state.handlers:
            options.append(
                (
                    f"start_{worker}",
                    replace(
                        state,
                        handlers=state.handlers | {handler_key},
                        history=state.history
                        + (f"HandlerStarted({worker})",),
                    ),
                )
            )

        if handler_key in state.handlers:
            if is_current:
                options.append(
                    (
                        f"commit_{worker}",
                        replace(
                            state,
                            committed=True,
                            history=state.history + (f"Committed({worker})",),
                        ),
                    )
                )
            else:
                options.append(
                    (
                        f"reject_stale_{worker}",
                        replace(
                            state,
                            history=state.history
                            + (f"StaleRejected({worker})",),
                        ),
                    )
                )
                if mutant in {"stale_commit", "expired_commit"}:
                    stale = token != state.token or worker not in state.owners
                    expired = state.expiry == "expired"
                    enabled = (
                        mutant == "stale_commit" and stale
                    ) or (mutant == "expired_commit" and expired)
                    if enabled:
                        options.append(
                            (
                                f"invalid_commit_{worker}",
                                replace(
                                    state,
                                    committed=True,
                                    invalid_commit=True,
                                    history=state.history
                                    + (f"Committed({worker})",),
                                ),
                            )
                        )

        if worker in state.owners and state.expiry == "valid":
            options.append(
                (
                    f"expire_{worker}",
                    replace(
                        state,
                        expiry="expired",
                        history=state.history + (f"Expired({worker})",),
                    ),
                )
            )
        _ = other
    return tuple(options)


def violation(state: State) -> str | None:
    if len(state.owners) > 1:
        return "AtMostOneValidOwner"
    if not state.handlers.issubset(state.attempts):
        return "HandlerRequiresAttempt"
    if any(
        later <= earlier
        for earlier, later in zip(state.claim_tokens, state.claim_tokens[1:])
    ):
        return "FencingTokenMonotonic"
    if state.invalid_commit:
        return "StaleOrExpiredCannotCommit"
    if state.invalid_renewal:
        return "RenewRequiresValidLease"
    return None


def explore(
    *, mutant: str | None
) -> tuple[int, int, tuple[str, ...] | None, str | None, frozenset[str]]:
    queue = deque([(INITIAL, ("initial",), 0)])
    seen = {INITIAL}
    transitions = 0
    witnesses: set[str] = set()
    while queue:
        state, path, depth = queue.popleft()
        witnesses.update(state.history)
        broken = violation(state)
        if broken:
            return len(seen), transitions, path, broken, frozenset(witnesses)
        if depth >= BOUND:
            continue
        for action, candidate in successors(state, mutant=mutant):
            transitions += 1
            if candidate not in seen:
                seen.add(candidate)
                queue.append((candidate, path + (action,), depth + 1))
    return len(seen), transitions, None, None, frozenset(witnesses)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutant", choices=tuple(MUTANTS))
    args = parser.parse_args()
    states, transitions, path, broken, witnesses = explore(mutant=args.mutant)
    if args.mutant:
        if broken != MUTANTS[args.mutant]:
            print(
                f"MUTANT_SURVIVED mutant={args.mutant} states={states} "
                f"transitions={transitions}"
            )
            return 1
        print(
            f"MUTANT_KILLED mutant={args.mutant} property={broken} "
            f"bound={BOUND} states={states} transitions={transitions} "
            f"path={' -> '.join(path or ())}"
        )
        return 0
    if broken:
        print(f"FAIL property={broken} path={' -> '.join(path or ())}")
        return 1
    missing = WITNESSES - witnesses
    if missing:
        print(f"VACUOUS missing_witnesses={sorted(missing)}")
        return 1
    print(
        f"PASS bound={BOUND} states={states} transitions={transitions} "
        f"witnesses={len(WITNESSES)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
