# Middleware lifecycle bounded model

This local finite model covers the four `AgentMiddleware` failure boundaries
inside `DurableModelLoop`. It deliberately excludes middleware payload content,
provider/tool semantics, persistence implementation, concurrency, and hidden
middleware I/O.

```bash
python3 formal/middleware/check.py
python3 formal/middleware/check.py --mutant before_invokes
python3 formal/middleware/check.py --mutant rewrite_response_identity
```

The normal model checks that `before_*` failure precedes any corresponding
external invocation, `after_*` failure follows exactly one invocation, and a
terminal state is absorbing, and `after_model` preserves provider response
identity. The targeted mutants invoke after a `before_*` failure or rewrite
response identity; the unchanged checker must kill each with a counterexample.

This is bounded exhaustive evidence for this finite abstraction, not universal
runtime refinement. Runtime conformance is tested separately.
