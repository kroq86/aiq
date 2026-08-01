# Deterministic instruction templates

`InstructionTemplate` supports only explicit `{input:name}` and
`{artifact:name}` placeholders. Resolution is pure and synchronous: callers
must load artifact text through an explicit adapter before constructing an
`ArtifactBinding`.

```python
template = InstructionTemplate(
    "Use {artifact:policy} for customer {input:customer_id}",
    template_id="support-policy",
    version="1",
)
resolved = template.resolve(
    inputs={"customer_id": "C-17"},
    artifacts={"policy": ArtifactBinding(policy_ref, policy_text)},
)
request = ModelRequest(
    messages=(ModelMessage("user", question),),
    artifacts=(policy_ref,),
    instruction=resolved,
)
```

The durable request stores resolved text, template identity/version and digest,
exact artifact refs, input-binding digest, and resolved-payload digest. A model
loop continuation copies this committed value; it never resolves mutable
bindings or a current artifact version again.

Missing and unexpected bindings fail explicitly before `ModelCallRequested`.
Arbitrary Python expressions, environment lookup, timestamps, randomness, and
hidden I/O are not part of the grammar.
