# Durable Sequence

`Sequence` is a linear, fail-fast parent run. Each child remains a separate
`AgentlogApplication` run with its own history. The parent commits only child-start
identity, terminal outcome, and exact output `ArtifactRef` values.

For 0.3 there is no branching, loop, parallel execution, compensation, or retry
policy. A repeated physical dispatch may occur after a crash, but the committed
`ChildStartRequested.operation_id` remains the child run identity and only one
terminal outcome advances the parent.
