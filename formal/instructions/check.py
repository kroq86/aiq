from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass

MUTANTS = {
    "latest_artifact": "ArtifactVersionPreserved",
    "missing_empty": "MissingBindingRejected",
    "omit_template_version": "TemplateVersionInIdentity",
    "ignore_digest": "ArtifactDigestChecked",
    "reresolve_restart": "CommittedResolutionStableAfterRestart",
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Resolution:
    text: str
    identity: str
    artifact_version: int


@dataclass(frozen=True, slots=True)
class State:
    committed: Resolution | None
    latest_artifact_version: int
    latest_artifact_content: str


def resolve(
    *,
    template_version: int,
    artifact_version: int,
    artifact_content: str,
    expected_digest: str,
    input_value: str | None,
    mutant: str | None,
) -> Resolution | None:
    if input_value is None:
        if mutant != "missing_empty":
            return None
        input_value = ""
    if digest(artifact_content) != expected_digest and mutant != "ignore_digest":
        return None
    selected_version = 2 if mutant == "latest_artifact" else artifact_version
    identity_version = 0 if mutant == "omit_template_version" else template_version
    text = f"policy={artifact_content};input={input_value}"
    identity = digest(f"template:{identity_version}:{text}:{selected_version}")
    return Resolution(text, identity, selected_version)


def restart(state: State, *, mutant: str | None) -> Resolution | None:
    if state.committed is None:
        return None
    if mutant == "reresolve_restart":
        return Resolution(
            f"policy={state.latest_artifact_content};input=A",
            digest(f"latest:{state.latest_artifact_version}"),
            state.latest_artifact_version,
        )
    return state.committed


def check(mutant: str | None) -> tuple[str | None, int]:
    checks = 0
    for template_version in (1, 2):
        for artifact_version in (1, 2):
            for input_value in ("A", "B"):
                kwargs = {
                    "template_version": template_version,
                    "artifact_version": artifact_version,
                    "artifact_content": "v1",
                    "expected_digest": digest("v1"),
                    "input_value": input_value,
                    "mutant": mutant,
                }
                first = resolve(**kwargs)
                second = resolve(**kwargs)
                checks += 1
                if first != second:
                    return "DeterministicResolution", checks

    missing = resolve(
        template_version=1,
        artifact_version=1,
        artifact_content="v1",
        expected_digest=digest("v1"),
        input_value=None,
        mutant=mutant,
    )
    checks += 1
    if missing is not None:
        return "MissingBindingRejected", checks

    version_one = resolve(
        template_version=1,
        artifact_version=1,
        artifact_content="v1",
        expected_digest=digest("v1"),
        input_value="A",
        mutant=mutant,
    )
    version_two = resolve(
        template_version=2,
        artifact_version=1,
        artifact_content="v1",
        expected_digest=digest("v1"),
        input_value="A",
        mutant=mutant,
    )
    checks += 1
    if version_one is not None and version_two is not None and version_one.identity == version_two.identity:
        return "TemplateVersionInIdentity", checks

    bad_digest = resolve(
        template_version=1,
        artifact_version=1,
        artifact_content="changed",
        expected_digest=digest("v1"),
        input_value="A",
        mutant=mutant,
    )
    checks += 1
    if bad_digest is not None:
        return "ArtifactDigestChecked", checks

    committed = resolve(
        template_version=1,
        artifact_version=1,
        artifact_content="v1",
        expected_digest=digest("v1"),
        input_value="A",
        mutant=mutant,
    )
    checks += 1
    if committed is None:
        return "NormalPathReachable", checks
    if committed.artifact_version != 1:
        return "ArtifactVersionPreserved", checks
    state = State(committed, 2, "v2")
    after_restart = restart(state, mutant=mutant)
    checks += 1
    if after_restart != committed:
        return "CommittedResolutionStableAfterRestart", checks
    return None, checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutant", choices=tuple(MUTANTS))
    args = parser.parse_args()
    broken, checks = check(args.mutant)
    if args.mutant:
        expected = MUTANTS[args.mutant]
        if broken != expected:
            print(
                f"MUTANT_SURVIVED mutant={args.mutant} expected={expected} "
                f"actual={broken} checks={checks}"
            )
            return 1
        print(
            f"MUTANT_KILLED mutant={args.mutant} property={broken} checks={checks}"
        )
        return 0
    if broken:
        print(f"FAIL property={broken} checks={checks}")
        return 1
    print(f"PASS domain=2x2x2 checks={checks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
