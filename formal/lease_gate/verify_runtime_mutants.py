"""Kill lease source mutants and prove byte-for-byte restoration."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQLITE = ROOT / "src" / "aiq" / "sqlite.py"
RUNTIME = ROOT / "src" / "aiq" / "runtime.py"


@dataclass(frozen=True, slots=True)
class Mutant:
    name: str
    path: Path
    scope: str
    old: str
    new: str
    test: str


LEASE_TEST = "tests/test_effect_leases.py::SQLiteEffectLeaseTests::"
RUNTIME_TEST = (
    "tests/test_runtime.py::DurableEffectDispatcherTests::"
)

MUTANTS = (
    Mutant(
        "claim_not_atomic",
        SQLITE,
        "_try_claim_effect_sync",
        "            expires_unix = now_unix + lease_ttl_seconds\n",
        "            connection.commit()\n"
        "            expires_unix = now_unix + lease_ttl_seconds\n",
        LEASE_TEST
        + "test_claim_and_attempt_roll_back_when_lease_write_fails",
    ),
    Mutant(
        "claim_before_expiry",
        SQLITE,
        "_try_claim_effect_sync",
        "and float(expiry) > now_unix",
        "and float(expiry) < now_unix",
        LEASE_TEST
        + "test_claim_is_atomic_with_attempt_and_busy_is_not_attempt",
    ),
    Mutant(
        "released_token_not_incremented",
        SQLITE,
        "_next_released_fencing_token",
        "return current + 1",
        "return current",
        LEASE_TEST + "test_released_claim_gets_larger_token",
    ),
    Mutant(
        "commit_ignores_token",
        SQLITE,
        "_commit_fenced_subscription_batch_sync",
        'or int(lease_row["fencing_token"]) != lease.fencing_token',
        "or False",
        LEASE_TEST
        + "test_commit_requires_matching_token_and_unexpired_lease",
    ),
    Mutant(
        "expired_commit_allowed",
        SQLITE,
        "_commit_fenced_subscription_batch_sync",
        'or float(lease_row["lease_expires_at_unix"]) <= now_unix',
        "or False",
        LEASE_TEST
        + "test_commit_requires_matching_token_and_unexpired_lease",
    ),
    Mutant(
        "renew_after_expiry",
        SQLITE,
        "_renew_effect_claim_sync",
        "AND lease_expires_at_unix > ?",
        "AND ? IS NOT NULL",
        LEASE_TEST + "test_expired_or_foreign_lease_cannot_renew",
    ),
    Mutant(
        "renew_foreign_worker",
        SQLITE,
        "_renew_effect_claim_sync",
        "AND worker_id = ?",
        "AND ? IS NOT NULL",
        LEASE_TEST + "test_expired_or_foreign_lease_cannot_renew",
    ),
    Mutant(
        "claim_after_committed_result",
        SQLITE,
        "_try_claim_effect_sync",
        "if _operation_has_committed_result(",
        "if False and _operation_has_committed_result(",
        LEASE_TEST
        + "test_atomic_claim_rejects_terminal_and_committed_result",
    ),
    Mutant(
        "handler_without_confirmation",
        RUNTIME,
        "DurableEffectDispatcher.run_once",
        'if confirmation.status != "confirmed":',
        'if False and confirmation.status != "confirmed":',
        RUNTIME_TEST
        + "test_terminal_between_claim_and_confirmation_blocks_handler",
    ),
    Mutant(
        "commit_without_ownership_check",
        SQLITE,
        "_commit_fenced_subscription_batch_sync",
        "            if (\n                lease_row is None\n",
        "            if False and (\n                lease_row is None\n",
        LEASE_TEST
        + "test_stale_worker_cannot_append_or_advance_checkpoint",
    ),
    Mutant(
        "worker_clock_authoritative",
        SQLITE,
        "_database_now_unix",
        "    return float(\n"
        "        connection.execute(\n"
        '            f"SELECT {_DB_NOW_UNIX} AS now_unix"\n'
        '        ).fetchone()["now_unix"]\n'
        "    )",
        "    return datetime.now(timezone.utc).timestamp()",
        LEASE_TEST
        + "test_worker_clock_skew_does_not_expire_live_db_lease",
    ),
    Mutant(
        "takeover_reuses_token",
        SQLITE,
        "_next_takeover_fencing_token",
        "return current + 1",
        "return current",
        LEASE_TEST
        + "test_repeated_takeover_has_fresh_ids_and_monotonic_tokens",
    ),
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _replace_in_scope(source: str, mutant: Mutant) -> str:
    scope_name = mutant.scope
    search_from = 0
    if "." in scope_name:
        class_name, scope_name = scope_name.split(".", 1)
        search_from = source.find(f"class {class_name}")
        if search_from < 0:
            raise RuntimeError(
                f"{mutant.name}: class {class_name!r} not found"
            )
    marker = (
        f"def {scope_name}("
        if scope_name.startswith("_")
        else f"    async def {scope_name}("
    )
    start = source.find(marker, search_from)
    if start < 0:
        raise RuntimeError(
            f"{mutant.name}: scope {mutant.scope!r} not found"
        )
    next_top = source.find("\ndef ", start + len(marker))
    next_method = source.find("\n    async def ", start + len(marker))
    next_sync_method = source.find("\n    def ", start + len(marker))
    candidates = [
        position
        for position in (next_top, next_method, next_sync_method)
        if position >= 0
    ]
    end = min(candidates, default=len(source))
    scoped = source[start:end]
    if scoped.count(mutant.old) != 1:
        raise RuntimeError(
            f"{mutant.name}: expected one target in {mutant.scope}, "
            f"found {scoped.count(mutant.old)}"
        )
    return source[:start] + scoped.replace(
        mutant.old, mutant.new, 1
    ) + source[end:]


def main() -> int:
    snapshots = {
        path: path.read_bytes() for path in {item.path for item in MUTANTS}
    }
    original_hashes = {
        path: _digest(payload) for path, payload in snapshots.items()
    }
    killed = 0
    try:
        for mutant in MUTANTS:
            original = snapshots[mutant.path]
            source = original.decode()
            mutated = _replace_in_scope(source, mutant).encode()
            mutant.path.write_bytes(mutated)
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        mutant.test,
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
            finally:
                mutant.path.write_bytes(original)
            restored = _digest(mutant.path.read_bytes())
            if restored != original_hashes[mutant.path]:
                raise RuntimeError(
                    f"{mutant.name}: source restoration hash mismatch"
                )
            if completed.returncode == 0:
                print(
                    f"MUTANT_SURVIVED mutant={mutant.name} "
                    f"file={mutant.path.relative_to(ROOT)}"
                )
                return 1
            killed += 1
            print(
                f"MUTANT_KILLED mutant={mutant.name} "
                f"test={mutant.test} "
                f"hash={restored}"
            )
    finally:
        for path, payload in snapshots.items():
            path.write_bytes(payload)
        for path, expected in original_hashes.items():
            actual = _digest(path.read_bytes())
            if actual != expected:
                raise RuntimeError(
                    f"final restoration failed for {path}"
                )
    print(
        f"PASS killed={killed} restored_files={len(snapshots)} "
        "final_hashes_match=true"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
