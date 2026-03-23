#!/usr/bin/env python3
"""
per_process_state.py — Per-process state file support for org-as-code.

PROBLEM: The monolithic registry/state.yaml is a merge-conflict bottleneck
when multiple AI agents update different processes concurrently. With 10+
active processes, concurrent writes will cause conflicts.

SOLUTION: Store each process's state in its own file:
    processes/{ID}/state.yaml

This allows multiple agents to update different processes without conflicts,
since Git can merge changes to different files automatically.

The registry/state.yaml is kept as a read-only index for quick overview.

USAGE:
    # Read state for a specific process
    state = ProcessState.read("PERF-001", repo_path)

    # Update state
    state.update(new_state="COMMITTED", notes="Done")

    # List all processes
    processes = ProcessState.list_all(repo_path)
"""
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ProcessState:
    """Manages per-process state files at processes/{ID}/state.yaml."""

    VALID_STATES = {"P_COMPLETE", "V_COMPLETE", "P_READY", "COMMITTED",
                    "ABANDONED", "REVERT_REQUESTED", "REVERTED"}

    def __init__(self, process_id: str, repo_path: Path):
        self.process_id = process_id
        self.repo_path = Path(repo_path)
        self.state_file = self.repo_path / "processes" / process_id / "state.yaml"

    def read(self) -> dict:
        """Read the process state. Returns empty dict if not found."""
        if not self.state_file.exists():
            return {}
        data = yaml.safe_load(self.state_file.read_text()) or {}
        return data

    def write(self, data: dict):
        """Write the process state."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        )

    def update(
        self,
        new_state: str,
        assigned_to: Optional[str] = None,
        notes: Optional[str] = None,
        priority: Optional[float] = None,
    ) -> dict:
        """Update the process state. Creates the file if it doesn't exist."""
        if new_state not in self.VALID_STATES:
            raise ValueError(f"Invalid state: {new_state}. Must be one of {self.VALID_STATES}")

        data = self.read()
        old_state = data.get("state", "UNKNOWN")
        data["state"] = new_state
        data["last_updated"] = _now_iso()
        if assigned_to:
            data["assigned_to"] = assigned_to
        if notes:
            data["notes"] = notes
        if priority is not None:
            data["priority"] = priority
        self.write(data)
        return {"old_state": old_state, "new_state": new_state}

    def create(
        self,
        template: str,
        title: str,
        agent_id: str,
        priority: float = 0.5,
        tension_id: Optional[str] = None,
    ) -> dict:
        """Create a new process state file."""
        if self.state_file.exists():
            raise FileExistsError(f"Process {self.process_id} already exists")

        data = {
            "process_id": self.process_id,
            "title": title,
            "state": "P_COMPLETE",
            "p_step": 0,
            "v_step": 0,
            "assigned_to": agent_id,
            "priority": priority,
            "template": template,
            "created_at": _now_iso(),
            "last_updated": _now_iso(),
        }
        if tension_id:
            data["tension"] = tension_id

        self.write(data)
        return data

    @classmethod
    def list_all(cls, repo_path: Path) -> list:
        """List all processes with their current states."""
        processes_dir = Path(repo_path) / "processes"
        if not processes_dir.exists():
            return []

        results = []
        for proc_dir in sorted(processes_dir.iterdir()):
            if not proc_dir.is_dir():
                continue
            state_file = proc_dir / "state.yaml"
            if state_file.exists():
                data = yaml.safe_load(state_file.read_text()) or {}
                data["process_id"] = proc_dir.name
                results.append(data)
            else:
                # Process directory exists but no state file — infer from artifacts
                results.append({
                    "process_id": proc_dir.name,
                    "state": "UNKNOWN",
                    "notes": "No state.yaml found in process directory",
                })

        return results

    @classmethod
    def migrate_from_monolithic(cls, repo_path: Path, dry_run: bool = False) -> int:
        """
        Migrate from registry/state.yaml to per-process state files.
        Returns the number of processes migrated.
        """
        repo_path = Path(repo_path)
        monolithic_path = repo_path / "registry" / "state.yaml"

        if not monolithic_path.exists():
            print("No registry/state.yaml found — nothing to migrate")
            return 0

        data = yaml.safe_load(monolithic_path.read_text()) or {}
        processes = data.get("processes", {})

        if not processes:
            print("No processes found in registry/state.yaml")
            return 0

        migrated = 0
        for process_id, state_data in processes.items():
            target = repo_path / "processes" / process_id / "state.yaml"
            if target.exists():
                print(f"  SKIP {process_id}: state.yaml already exists")
                continue

            state_data["process_id"] = process_id
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    yaml.dump(state_data, default_flow_style=False,
                              allow_unicode=True, sort_keys=False)
                )
                print(f"  MIGRATED {process_id}: {state_data.get('state', '?')}")
            else:
                print(f"  [DRY RUN] Would migrate {process_id}: {state_data.get('state', '?')}")
            migrated += 1

        if not dry_run and migrated > 0:
            # Add deprecation notice to monolithic file
            with open(monolithic_path, "a") as f:
                f.write(
                    "\n# DEPRECATED: This file is kept for backward compatibility.\n"
                    "# Process states are now stored in processes/{ID}/state.yaml\n"
                    "# Run: python improvements/per_process_state.py --migrate\n"
                )

        return migrated


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Per-process state management")
    parser.add_argument("--repo", default=".", help="Path to org-as-code repo")
    parser.add_argument("--migrate", action="store_true",
                        help="Migrate registry/state.yaml to per-process files")
    parser.add_argument("--list", action="store_true",
                        help="List all processes and their states")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without modifying files")
    args = parser.parse_args()

    repo = Path(args.repo)

    if args.migrate:
        count = ProcessState.migrate_from_monolithic(repo, dry_run=args.dry_run)
        print(f"\nMigrated {count} processes")

    if args.list:
        processes = ProcessState.list_all(repo)
        if not processes:
            print("No processes found")
        else:
            print(f"\n{'ID':<15} {'STATE':<20} {'PRIORITY':<10} {'ASSIGNED':<15}")
            print("-" * 65)
            for p in processes:
                print(
                    f"{p.get('process_id', '?'):<15} "
                    f"{p.get('state', '?'):<20} "
                    f"{p.get('priority', '?'):<10} "
                    f"{p.get('assigned_to', '?'):<15}"
                )
