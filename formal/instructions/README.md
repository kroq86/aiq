# Instruction resolution finite-domain checker

The checker exhaustively covers two template versions, two pinned artifact
versions, and two explicit input values. It checks deterministic resolution,
missing-binding rejection, template-version identity, artifact digest
validation, pinned artifact preservation, and reuse of committed resolution
after restart.

```bash
python3 formal/instructions/check.py
python3 formal/instructions/check.py --mutant latest_artifact
python3 formal/instructions/check.py --mutant missing_empty
python3 formal/instructions/check.py --mutant omit_template_version
python3 formal/instructions/check.py --mutant ignore_digest
python3 formal/instructions/check.py --mutant reresolve_restart
```

This is exhaustive evidence for the stated finite domain, not proof of Python,
SHA-256, storage adapters, arbitrary templates, or universal runtime
refinement.
