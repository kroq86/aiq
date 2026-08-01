# Versioned artifact bounded model

This finite abstraction covers absent, embedded, and external artifact versions;
immutable identity; idempotent registration; atomic conflicts; restart self-loops;
resolution failure; and the model-invocation boundary.

```bash
python3 formal/artifacts/check.py
for mutant in missing_version_invocation different_digest \
  different_storage_reference external_stores_blob \
  external_overwrites_embedded retry_second_logical_version \
  failed_registration_partial_row; do
  python3 formal/artifacts/check.py --mutant "$mutant"
done
```

The unchanged invariant checker must produce a counterexample for every targeted
semantic mutant. This does not prove that an external object exists or matches
the registered digest/size. That obligation belongs to the external-storage
adapter before registration. It also does not prove concurrency, ACLs,
cryptographic implementation, or universal runtime refinement.
