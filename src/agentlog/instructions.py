"""Deterministic instruction templates over explicit durable bindings."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .artifacts import ArtifactDigestMismatchError, ArtifactRef, artifact_digest
from .core import Event, JsonValue


_TOKEN = re.compile(r"\{(input|artifact):([A-Za-z_][A-Za-z0-9_.-]*)\}")


class InstructionResolutionError(ValueError):
    pass


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _plain_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ArtifactBinding:
    ref: ArtifactRef
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("instruction artifact content must be text")
        encoded = self.content.encode()
        if artifact_digest(encoded) != self.ref.digest:
            raise ArtifactDigestMismatchError(
                f"artifact binding does not match ref digest: {self.ref.name!r}"
            )
        if self.ref.size is not None and self.ref.size != len(encoded):
            raise ArtifactDigestMismatchError(
                f"artifact binding does not match ref size: {self.ref.name!r}"
            )


@dataclass(frozen=True, slots=True)
class ResolvedInstruction:
    text: str
    template_id: str
    template_version: str
    template_digest: str
    artifact_refs: tuple[ArtifactRef, ...]
    input_bindings_digest: str
    resolved_payload_digest: str

    def __post_init__(self) -> None:
        if not self.template_id or not self.template_version:
            raise ValueError("instruction template identity must not be empty")
        if _digest_bytes(self.text.encode()) != self.resolved_payload_digest:
            raise ValueError("resolved instruction payload digest does not match text")

    def to_data(self) -> Mapping[str, JsonValue]:
        return Event(
            "ResolvedInstructionSerialized",
            {
                "text": self.text,
                "template_id": self.template_id,
                "template_version": self.template_version,
                "template_digest": self.template_digest,
                "artifact_refs": tuple(ref.to_data() for ref in self.artifact_refs),
                "input_bindings_digest": self.input_bindings_digest,
                "resolved_payload_digest": self.resolved_payload_digest,
            },
        ).data

    @classmethod
    def from_data(cls, data: object) -> ResolvedInstruction:
        if not isinstance(data, Mapping):
            raise TypeError("resolved instruction must be an object")
        refs = data.get("artifact_refs", ())
        if not isinstance(refs, tuple):
            raise TypeError("resolved instruction artifact_refs must be an array")
        return cls(
            text=str(data["text"]),
            template_id=str(data["template_id"]),
            template_version=str(data["template_version"]),
            template_digest=str(data["template_digest"]),
            artifact_refs=tuple(ArtifactRef.from_data(ref) for ref in refs),
            input_bindings_digest=str(data["input_bindings_digest"]),
            resolved_payload_digest=str(data["resolved_payload_digest"]),
        )


@dataclass(frozen=True, slots=True)
class InstructionTemplate:
    template: str
    template_id: str
    version: str

    def __post_init__(self) -> None:
        if not self.template or not self.template_id or not self.version:
            raise ValueError("instruction template, id, and version must not be empty")
        remainder = _TOKEN.sub("", self.template)
        if "{" in remainder or "}" in remainder:
            raise ValueError("instruction template contains invalid placeholder syntax")

    @property
    def digest(self) -> str:
        return _digest_bytes(
            _canonical_json(
                {
                    "template": self.template,
                    "template_id": self.template_id,
                    "version": self.version,
                }
            )
        )

    def resolve(
        self,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        artifacts: Mapping[str, ArtifactBinding] | None = None,
    ) -> ResolvedInstruction:
        input_values = dict(inputs or {})
        artifact_values = dict(artifacts or {})
        # Validate/freeze all input values with the canonical event rules.
        frozen_inputs = Event("InstructionInputsValidated", input_values).data
        tokens = tuple(_TOKEN.finditer(self.template))
        required_inputs = {match.group(2) for match in tokens if match.group(1) == "input"}
        required_artifacts = {
            match.group(2) for match in tokens if match.group(1) == "artifact"
        }
        missing_inputs = sorted(required_inputs - frozen_inputs.keys())
        missing_artifacts = sorted(required_artifacts - artifact_values.keys())
        unexpected_inputs = sorted(frozen_inputs.keys() - required_inputs)
        unexpected_artifacts = sorted(artifact_values.keys() - required_artifacts)
        if missing_inputs or missing_artifacts:
            raise InstructionResolutionError(
                f"missing instruction bindings: inputs={missing_inputs}, "
                f"artifacts={missing_artifacts}"
            )
        if unexpected_inputs or unexpected_artifacts:
            raise InstructionResolutionError(
                f"unexpected instruction bindings: inputs={unexpected_inputs}, "
                f"artifacts={unexpected_artifacts}"
            )

        def replacement(match: re.Match[str]) -> str:
            kind, name = match.groups()
            if kind == "artifact":
                return artifact_values[name].content
            value = frozen_inputs[name]
            if isinstance(value, str):
                return value
            return _canonical_json(_plain_json(value)).decode()

        resolved = _TOKEN.sub(replacement, self.template)
        referenced_artifacts = tuple(
            artifact_values[name].ref for name in sorted(required_artifacts)
        )
        bindings_payload: dict[str, Any] = {
            key: _plain_json(frozen_inputs[key]) for key in sorted(required_inputs)
        }
        return ResolvedInstruction(
            text=resolved,
            template_id=self.template_id,
            template_version=self.version,
            template_digest=self.digest,
            artifact_refs=referenced_artifacts,
            input_bindings_digest=_digest_bytes(_canonical_json(bindings_payload)),
            resolved_payload_digest=_digest_bytes(resolved.encode()),
        )
