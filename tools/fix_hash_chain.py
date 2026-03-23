#!/usr/bin/env python3
"""
fix_hash_chain.py — Rebuild artifacts.jsonl with correct SHA-256 hashes.

BUG: The original artifacts.jsonl shipped with 62-character hex strings
instead of valid 64-character SHA-256 hashes. The org_verify_chain tool
reports all entries as invalid because the recomputed hashes never match.

USAGE:
    python fix_hash_chain.py [--repo /path/to/org-as-code] [--dry-run]

WHAT IT DOES:
    1. Reads all entries from artifacts.jsonl (preserving content)
    2. Strips the old prev_hash and entry_hash fields
    3. Recomputes the full chain from genesis (prev_hash = "0" * 64)
    4. Writes the corrected chain back to artifacts.jsonl
    5. Verifies the rebuilt chain

SAFETY:
    - Creates a backup at artifacts.jsonl.bak before modifying
    - --dry-run flag shows what would change without writing
"""
import argparse
import hashlib
import json
import shutil
from pathlib import Path


def canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def rebuild_chain(artifacts_path: Path, dry_run: bool = False) -> bool:
    """
    Rebuild the hash chain in artifacts.jsonl.
    Returns True if successful, False if errors encountered.
    """
    if not artifacts_path.exists():
        print(f"ERROR: {artifacts_path} does not exist")
        return False

    # Read all entries
    entries = []
    with open(artifacts_path) as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"ERROR: Line {i+1} is not valid JSON: {e}")
                return False

    if not entries:
        print("WARNING: artifacts.jsonl is empty")
        return True

    print(f"Read {len(entries)} entries from {artifacts_path}")

    # Check if chain is already valid
    print("\nChecking current chain validity...")
    broken = _verify_chain(entries)
    if not broken:
        print("Chain is already valid. No rebuild needed.")
        return True

    print(f"Found {len(broken)} broken entries:")
    for b in broken[:5]:
        print(f"  {b}")
    if len(broken) > 5:
        print(f"  ... and {len(broken) - 5} more")

    if dry_run:
        print("\n[DRY RUN] Would rebuild chain. No files modified.")
        return True

    # Create backup
    backup_path = artifacts_path.with_suffix(".jsonl.bak")
    shutil.copy2(artifacts_path, backup_path)
    print(f"\nBackup created: {backup_path}")

    # Rebuild chain
    print("Rebuilding chain...")
    prev_hash = "0" * 64
    rebuilt = []
    for entry in entries:
        # Remove old hash fields
        clean_entry = {k: v for k, v in entry.items()
                       if k not in ("prev_hash", "entry_hash")}
        clean_entry["prev_hash"] = prev_hash
        chain_input = prev_hash + canonical_json(
            {k: v for k, v in clean_entry.items() if k != "entry_hash"}
        )
        clean_entry["entry_hash"] = hash_content(chain_input)
        rebuilt.append(clean_entry)
        prev_hash = clean_entry["entry_hash"]

    # Write rebuilt chain
    with open(artifacts_path, "w") as f:
        for entry in rebuilt:
            f.write(json.dumps(entry) + "\n")

    print(f"Written {len(rebuilt)} entries")
    print(f"Final chain tip: {prev_hash}")

    # Verify rebuilt chain
    print("\nVerifying rebuilt chain...")
    broken = _verify_chain(rebuilt)
    if broken:
        print(f"ERROR: Rebuilt chain still broken: {broken}")
        print(f"Restoring backup from {backup_path}")
        shutil.copy2(backup_path, artifacts_path)
        return False

    print(f"Chain VALID: {len(rebuilt)} entries, all SHA-256 hashes correct")
    print(f"All entry_hash lengths: {set(len(e['entry_hash']) for e in rebuilt)} (should be {{64}})")
    return True


def _verify_chain(entries: list) -> list:
    """Returns list of error strings. Empty list means chain is valid."""
    broken = []
    prev_hash = "0" * 64
    for i, entry in enumerate(entries):
        stored_prev = entry.get("prev_hash", "")
        stored_hash = entry.get("entry_hash", "")

        if stored_prev != prev_hash:
            broken.append(f"Entry {i} ({entry.get('action', '?')}): prev_hash mismatch")

        chain_input = entry.get("prev_hash", "") + canonical_json(
            {k: v for k, v in entry.items() if k != "entry_hash"}
        )
        expected_hash = hash_content(chain_input)
        if expected_hash != stored_hash:
            broken.append(
                f"Entry {i} ({entry.get('action', '?')}): "
                f"entry_hash mismatch (stored len={len(stored_hash)}, expected len=64)"
            )

        prev_hash = stored_hash

    return broken


def main():
    parser = argparse.ArgumentParser(description="Rebuild org-as-code hash chain")
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to org-as-code repository (default: current directory)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying files"
    )
    args = parser.parse_args()

    repo = Path(args.repo)
    artifacts_path = repo / "registry" / "artifacts.jsonl"

    success = rebuild_chain(artifacts_path, dry_run=args.dry_run)
    exit(0 if success else 1)


if __name__ == "__main__":
    main()
