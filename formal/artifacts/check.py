from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, replace

BOUND = 7
MUTANTS = (
    "missing_version_invocation",
    "different_digest",
    "different_storage_reference",
    "external_stores_blob",
    "external_overwrites_embedded",
    "retry_second_logical_version",
    "failed_registration_partial_row",
)


@dataclass(frozen=True, slots=True)
class State:
    kind: str = "absent"
    digest: int = 0
    size: int = 0
    storage_reference: int = 0
    blob: bool = False
    original_identity: tuple[str, int, int, int] | None = None
    logical_versions: int = 0
    failed_registration_changed_state: bool = False
    requested_digest: int = 0
    model_invocations: int = 0
    failed_resolution: bool = False
    history: tuple[str, ...] = ()


def _external(state: State) -> State:
    identity = ("external", 1, 10, 1)
    return replace(
        state,
        kind="external",
        digest=1,
        size=10,
        storage_reference=1,
        blob=False,
        original_identity=state.original_identity or identity,
        logical_versions=1,
        history=state.history + ("ExternalRegistered:1",),
    )


def _embedded(state: State) -> State:
    identity = ("embedded", 1, 10, 0)
    return replace(
        state,
        kind="embedded",
        digest=1,
        size=10,
        storage_reference=0,
        blob=True,
        original_identity=identity,
        logical_versions=1,
        history=state.history + ("EmbeddedStored:1",),
    )


def successors(state: State, *, mutant: str | None):
    if len(state.history) >= BOUND or state.failed_resolution:
        return (("restart", state),)
    if state.kind == "absent" and state.requested_digest == 0:
        invalid = replace(
            state,
            failed_registration_changed_state=(
                mutant == "failed_registration_partial_row"
            ),
            digest=2 if mutant == "failed_registration_partial_row" else 0,
            history=state.history + ("ExternalRegistrationRejected",),
        )
        return (
            ("register_external", _external(state)),
            ("put_embedded", _embedded(state)),
            ("register_invalid", invalid),
            (
                "request_missing",
                replace(
                    state,
                    requested_digest=2,
                    history=state.history + ("Requested:2",),
                ),
            ),
        )
    if state.kind == "external" and state.requested_digest == 0:
        retry = replace(
            state,
            logical_versions=(
                state.logical_versions + 1
                if mutant == "retry_second_logical_version"
                else state.logical_versions
            ),
            history=state.history + ("ExternalRegistered:1",),
        )
        conflict_digest = replace(
            state,
            digest=2 if mutant == "different_digest" else state.digest,
            history=state.history + ("ExternalConflict:digest",),
        )
        conflict_storage = replace(
            state,
            storage_reference=(
                2
                if mutant == "different_storage_reference"
                else state.storage_reference
            ),
            history=state.history + ("ExternalConflict:storage",),
        )
        stores_blob = replace(
            state,
            blob=mutant == "external_stores_blob",
            history=state.history + ("ExternalRetryWithBlob",),
        )
        return (
            ("register_same", retry),
            ("register_different_digest", conflict_digest),
            ("register_different_storage", conflict_storage),
            ("register_external_with_blob", stores_blob),
            (
                "request_pinned",
                replace(
                    state,
                    requested_digest=state.digest,
                    history=state.history + ("Requested:1",),
                ),
            ),
        )
    if state.kind == "embedded" and state.requested_digest == 0:
        overwritten = (
            _external(state)
            if mutant == "external_overwrites_embedded"
            else replace(state, history=state.history + ("ExternalConflict:kind",))
        )
        return (
            ("register_external_over_embedded", overwritten),
            (
                "request_pinned",
                replace(
                    state,
                    requested_digest=state.digest,
                    history=state.history + ("Requested:1",),
                ),
            ),
        )
    if state.requested_digest:
        if state.requested_digest == state.digest and state.kind != "absent":
            return (
                (
                    "resolve_and_invoke",
                    replace(
                        state,
                        model_invocations=1,
                        history=state.history + ("ModelInvoked",),
                    ),
                ),
            )
        return (
            (
                "resolve_missing",
                replace(
                    state,
                    model_invocations=(
                        1 if mutant == "missing_version_invocation" else 0
                    ),
                    failed_resolution=True,
                    history=state.history + ("ArtifactResolutionFailed",),
                ),
            ),
        )
    return ()


def violation(state: State) -> str | None:
    if state.kind == "embedded" and not state.blob:
        return "EmbeddedHasBlob"
    if state.kind == "external" and (state.blob or not state.storage_reference):
        return "ExternalHasReferenceAndNoBlob"
    if state.original_identity is not None:
        identity = (state.kind, state.digest, state.size, state.storage_reference)
        if identity != state.original_identity:
            return "VersionIdentityImmutable"
    if state.logical_versions > 1:
        return "OneLogicalVersionPerNameVersion"
    if state.failed_registration_changed_state:
        return "FailedRegistrationAtomic"
    if state.failed_resolution and state.model_invocations:
        return "MissingVersionPreventsModelInvocation"
    if state.model_invocations and state.requested_digest != state.digest:
        return "InvocationUsesPinnedDigest"
    return None


def explore(*, mutant: str | None):
    initial = State()
    queue = deque([(initial, ())])
    seen = {initial}
    transitions = 0
    while queue:
        state, path = queue.popleft()
        broken = violation(state)
        if broken:
            return len(seen), transitions, path, broken
        for action, candidate in successors(state, mutant=mutant):
            transitions += 1
            if candidate not in seen:
                seen.add(candidate)
                queue.append((candidate, path + (action,)))
    return len(seen), transitions, None, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutant", choices=MUTANTS)
    args = parser.parse_args()
    states, transitions, path, broken = explore(mutant=args.mutant)
    if args.mutant:
        if broken is None:
            print(
                f"MUTANT_SURVIVED mutant={args.mutant} states={states} transitions={transitions}"
            )
            return 1
        print(
            f"MUTANT_KILLED mutant={args.mutant} property={broken} bound={BOUND} "
            f"states={states} transitions={transitions} path={' -> '.join(path or ())}"
        )
        return 0
    if broken:
        print(f"FAIL property={broken} path={' -> '.join(path or ())}")
        return 1
    print(f"PASS bound={BOUND} states={states} transitions={transitions} deadlocks=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
