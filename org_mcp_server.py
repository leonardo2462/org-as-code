#!/usr/bin/env python3
"""
Org-as-Code MCP Server
=======================
Gives AI agents (Claude Code, Windsurf) native tools to interact with
the org-as-code registry, processes, and protocol.

Tools:
  org_read_state        — Read current process states
  org_read_tensions     — Read open tensions
  org_read_attractors   — Read strategic attractors/goals
  org_read_agents       — Read registered agents
  org_read_health       — Read health metrics
  org_read_process      — Read a specific process and its artifacts
  org_read_artifacts    — Read recent entries from audit log
  org_read_convergence  — Read convergence history for a process
  org_update_state      — Update a process state
  org_create_process    — Create a new process (P.0)
  org_log_artifact      — Append to artifacts.jsonl (auto-calculates E(x) for V-steps)
  org_create_tension    — Register a new tension
  org_resolve_tension   — Mark a tension as resolved
  org_calculate_priority — Calculate Hamiltonian priority H(s)
  org_calculate_energy  — Calculate semantic energy E(x) (quadratic convergence)
  org_verify_chain      — Verify hash-chain integrity of audit log
  org_git_sync          — Pull latest, optionally commit+push changes

Transport: stdio (JSON-RPC 2.0 over stdin/stdout)
Logging:   stderr only (stdout reserved for MCP protocol)

Repo:      Expects ORG_REPO_PATH env var or defaults to the script directory
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

from fdm import DependencyGraph

# ---------------------------------------------------------------------------
# Cross-platform file locking (Unix: fcntl, Windows: msvcrt)
# ---------------------------------------------------------------------------
try:
    import fcntl

    def _lock(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)
except ImportError:
    import msvcrt

    def _lock(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

# ---------------------------------------------------------------------------
# Logging — MUST go to stderr (stdout = MCP protocol)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("mcp-org")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ORG_REPO = Path(os.environ.get("ORG_REPO_PATH", str(Path(__file__).parent)))
REGISTRY = ORG_REPO / "registry"
PROCESSES = ORG_REPO / "processes"
PROTOCOL = ORG_REPO / "protocol"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_yaml(path: Path) -> dict:
    """Read a YAML file and return its contents."""
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        logger.warning(f"Failed to parse {path}: {e}")
        return {}


def _write_yaml(path: Path, data: dict):
    """Write data to a YAML file atomically (write-then-rename).

    Uses a temporary file in the same directory to avoid partial writes.
    On success the temp file is renamed over the target (atomic on POSIX).
    On failure the target file is left untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".yaml.tmp")
    try:
        with open(tmp_path, "w") as f:
            _lock(f)
            try:
                f.write(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False))
            finally:
                _unlock(f)
        tmp_path.rename(path)
    except Exception:
        # Clean up partial temp file on failure
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _canonical_json(obj: dict) -> str:
    """Deterministic JSON for hashing (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash_content(content: str) -> str:
    """SHA-256 hex digest of a string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _get_chain_tip(path: Path) -> str:
    """Read the entry_hash of the last entry in the chain. Genesis = 64 zeros.

    Uses buffered reverse reading (8 KB blocks) instead of character-by-character
    seeking, which is both faster and safer with large JSONL files and multi-byte
    UTF-8 encodings.
    """
    if not path.exists():
        return "0" * 64
    BLOCK = 8192
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return "0" * 64
        # Read tail in blocks until we find a complete last line
        tail = b""
        remaining = size
        while remaining > 0:
            read_size = min(BLOCK, remaining)
            remaining -= read_size
            f.seek(remaining)
            tail = f.read(read_size) + tail
            # Strip trailing whitespace and look for a newline separator
            stripped = tail.rstrip(b"\n\r\t ")
            if b"\n" in stripped:
                break  # We have at least one complete line
    # Take the last non-empty line
    last_line = tail.rstrip(b"\n\r\t ").split(b"\n")[-1]
    if not last_line.strip():
        return "0" * 64
    try:
        return json.loads(last_line.decode("utf-8", errors="replace")).get(
            "entry_hash", "0" * 64
        )
    except json.JSONDecodeError:
        return "0" * 64


def _append_jsonl(path: Path, entry: dict):
    """Append a JSON line with hash-chain linking.

    Each entry gets:
      prev_hash  — entry_hash of the previous entry (or 64 zeros for genesis)
      entry_hash — SHA-256(prev_hash + canonical_json(entry without hashes))
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    prev_hash = _get_chain_tip(path)
    entry["prev_hash"] = prev_hash
    # Hash over prev_hash + canonical content (without entry_hash itself)
    chain_input = prev_hash + _canonical_json(
        {k: v for k, v in entry.items() if k != "entry_hash"}
    )
    entry["entry_hash"] = _hash_content(chain_input)
    with open(path, "a") as f:
        _lock(f)
        try:
            f.write(json.dumps(entry) + "\n")
        finally:
            _unlock(f)


def _read_jsonl(path: Path) -> list:
    """Read all entries from a JSONL file."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().strip().split("\n"):
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _safe_audit_append(entry: dict):
    """Append to artifacts.jsonl with error logging instead of raising.

    Used after state YAML has already been written (atomic rename).
    If the audit append fails, the state change is still valid but we log
    a CRITICAL warning so operators can detect the inconsistency and
    rebuild the chain with tools/fix_hash_chain.py.
    """
    try:
        _append_jsonl(REGISTRY / "artifacts.jsonl", entry)
    except Exception as exc:
        logger.critical(
            "AUDIT INCONSISTENCY: state was written but audit trail append "
            "failed for process %s: %s — run tools/fix_hash_chain.py to repair",
            entry.get("process_id", "unknown"),
            exc,
        )


def _load_config() -> dict:
    """Load protocol/config.yaml."""
    return _read_yaml(PROTOCOL / "config.yaml")


def _get_state_storage_mode() -> str:
    """Return the state storage mode from config: 'per_process' or 'monolithic'.

    Reads state_storage.mode from protocol/config.yaml.
    Defaults to 'monolithic' if the key is missing.
    """
    config = _load_config()
    return config.get("state_storage", {}).get("mode", "monolithic")


def _aggregate_per_process_states() -> dict:
    """Scan PROCESSES/*/state.yaml and return aggregated state in monolithic format.

    Returns dict like {"processes": {"FEAT-001": {...}, "BUG-002": {...}}}.
    Per-process state files include a 'process_id' field which is used as the key
    and stripped from the value dict.
    """
    processes = {}
    if PROCESSES.exists():
        for subdir in sorted(PROCESSES.iterdir()):
            if not subdir.is_dir():
                continue
            state_file = subdir / "state.yaml"
            if not state_file.exists():
                continue
            state = _read_yaml(state_file)
            if not state:
                continue
            pid = state.get("process_id", subdir.name)
            entry = {k: v for k, v in state.items() if k != "process_id"}
            processes[pid] = entry
    return {"processes": processes}


def _regenerate_state_index():
    """Regenerate registry/state.yaml as a read-only index from per-process state files.

    Called after every write in per_process mode to keep the index current
    for external tools that read registry/state.yaml.
    """
    aggregated = _aggregate_per_process_states()
    _write_yaml(REGISTRY / "state.yaml", aggregated)
    count = len(aggregated.get("processes", {}))
    logger.info(f"Regenerated state index: {count} processes")


def _regenerate_fdm_register():
    """Regenerate registry/fdm.json from current process dependency state.

    Called after every dependency mutation (add, remove, create-with-deps)
    to keep the FDM register current as a git-versioned snapshot.
    Non-fatal: logs warning on failure so the mutation still succeeds.
    """
    try:
        data = _aggregate_per_process_states()
        processes = data.get("processes", {})

        graph = DependencyGraph()

        # Add all processes as nodes
        for pid in processes:
            graph.add_node(pid)

        # Add edges from depends_on
        for pid, pdata in processes.items():
            deps = pdata.get("depends_on", [])
            for dep in deps:
                graph.add_edge(pid, dep)

        analysis = graph.analyze()

        # Build nodes dict with state info
        nodes = {}
        for pid, pdata in processes.items():
            nodes[pid] = {"state": pdata.get("state", "unknown")}

        # Build edges list
        edges = []
        for pid, pdata in processes.items():
            for dep in pdata.get("depends_on", []):
                edges.append([pid, dep])

        register = {
            "generated_at": _now_iso(),
            "nodes": nodes,
            "edges": edges,
            "parallel_groups": analysis["parallel_groups"],
            "cycles": analysis["cycles"],
            "critical_path": analysis["critical_path"],
            "bottleneck": analysis["bottleneck"],
            "impact_scores": analysis["impact_scores"],
        }

        REGISTRY.mkdir(parents=True, exist_ok=True)
        (REGISTRY / "fdm.json").write_text(json.dumps(register, indent=2))
        logger.info(f"Regenerated FDM register: {len(nodes)} nodes, {len(edges)} edges")
    except Exception as e:
        logger.warning(f"Failed to regenerate FDM register: {e}")


def _auto_migrate_if_needed():
    """Auto-migrate monolithic state.yaml to per-process state files.

    Triggers when: mode=per_process AND monolithic state.yaml has processes
    AND no per-process state files exist yet.
    After migration, regenerates the state index.
    """
    if _get_state_storage_mode() != "per_process":
        return

    data = _read_yaml(REGISTRY / "state.yaml")
    processes = data.get("processes", {})
    if not processes:
        return

    # If any per-process state file already exists, skip migration
    if PROCESSES.exists():
        for subdir in PROCESSES.iterdir():
            if subdir.is_dir() and (subdir / "state.yaml").exists():
                return

    # Migrate each process from monolithic to per-process
    count = 0
    for process_id, state_data in processes.items():
        per_process_data = {"process_id": process_id, **state_data}
        _write_yaml(PROCESSES / process_id / "state.yaml", per_process_data)
        logger.info(f"Auto-migrated {process_id} to per-process state")
        count += 1

    _regenerate_state_index()
    logger.info(f"Auto-migration complete: {count} processes migrated")


def _git_timeout() -> int:
    """Read git subprocess timeout from config, default 30s."""
    config = _load_config()
    return config.get("process_engine", {}).get("git_timeout_seconds", 30)


def _git_run(*args, check=True) -> str:
    """Run a git command in the org repo."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=str(ORG_REPO),
        capture_output=True,
        text=True,
        timeout=_git_timeout(),
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

_PROCESS_ID_RE = re.compile(r'^[A-Z]+-[0-9]+$')


def _sanitize_commit_message(msg: str) -> str:
    """Strip newlines to prevent git trailer injection.
    subprocess.run() with a list does not invoke a shell, so shell metacharacters
    are not a vector. The real risk is embedded newlines creating fake git trailers.
    """
    return msg.replace('\n', ' ').replace('\r', ' ').strip()


def _validate_process_id(process_id: str) -> tuple:
    """Validate process_id format: must match [A-Z]+-[0-9]+."""
    if not _PROCESS_ID_RE.match(process_id):
        return False, (
            f"Invalid process_id '{process_id}': must match [A-Z]+-[0-9]+ "
            f"(e.g., FEAT-001, BUG-042)"
        )
    return True, ""


def _validate_dependencies(process_id: str, depends_on: list) -> tuple:
    """Validate dependency list for a process.

    Checks:
      1. No self-references (process cannot depend on itself)
      2. Each dependency ID has valid format ([A-Z]+-[0-9]+)
      3. Each dependency exists (per-process state file or monolithic registry)
    """
    # Self-reference check
    if process_id in depends_on:
        return False, f"Process {process_id} cannot depend on itself"
    # Format and existence checks
    for dep in depends_on:
        ok, msg = _validate_process_id(dep)
        if not ok:
            return False, msg
        # Existence: check per-process state file or monolithic registry
        per_process_file = PROCESSES / dep / "state.yaml"
        if per_process_file.exists():
            continue
        # Fall back to monolithic registry
        mono_state = _read_yaml(REGISTRY / "state.yaml")
        if dep in mono_state.get("processes", {}):
            continue
        return False, f"Dependency {dep} does not exist"
    return True, ""


def _get_known_agent_ids() -> set:
    """Load registered agent IDs from agents.yaml."""
    agents_data = _read_yaml(REGISTRY / "agents.yaml")
    return {a["id"] for a in agents_data.get("agents", [])}


def _validate_agent_id(agent_id: str, allow_bootstrap: bool = False) -> tuple:
    """Validate agent_id against registered agents in agents.yaml.

    Behaviour depends on security.mode in protocol/config.yaml:
      - permissive (default): log warning, proceed
      - strict: reject unknown agents (unless allow_bootstrap=True)
    """
    known_ids = _get_known_agent_ids()
    if agent_id in known_ids:
        return True, ""

    config = _load_config()
    security_mode = config.get("security", {}).get("mode", "permissive")
    warning = f"agent_id '{agent_id}' not registered in agents.yaml"

    if security_mode == "strict" and not allow_bootstrap:
        _log_security_event(agent_id, "unknown_agent_rejected", warning)
        return False, f"SecurityError: {warning}"

    _log_security_event(agent_id, "unknown_agent_warning", warning)
    logger.warning(warning)
    return True, warning


def _log_security_event(agent_id: str, event_type: str, detail: str):
    """Log a security event to artifacts.jsonl."""
    _append_jsonl(REGISTRY / "artifacts.jsonl", {
        "type": "security_event",
        "agent": agent_id,
        "action": event_type,
        "description": detail,
        "timestamp": _now_iso(),
    })


def _has_v_step(process_id: str) -> bool:
    """Check if process has at least one V-step in the audit trail."""
    entries = _read_jsonl(REGISTRY / "artifacts.jsonl")
    return any(
        e.get("process_id") == process_id and e.get("type") == "v_step"
        for e in entries
    )


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("org-as-code")


# --- READ tools ---

@mcp.tool()
def org_read_state() -> str:
    """Read all process states.
    In per_process mode, aggregates from individual processes/*/state.yaml files.
    In monolithic mode, reads from registry/state.yaml.
    Returns the current state of all tracked processes."""
    storage_mode = _get_state_storage_mode()
    if storage_mode == "per_process":
        data = _aggregate_per_process_states()
    else:
        data = _read_yaml(REGISTRY / "state.yaml")
    processes = data.get("processes", {})
    if not processes:
        return "No active processes."
    lines = []
    for pid, info in processes.items():
        state = info.get("state", "?")
        assigned = info.get("assigned_to", "?")
        priority = info.get("priority", 0)
        notes = info.get("notes", "")
        line = f"- **{pid}** [{state}] assigned={assigned} priority={priority}"
        if notes:
            line += f" — {notes}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def org_read_tensions() -> str:
    """Read all open tensions from registry/tensions.yaml.
    Tensions are unresolved problems or opportunities."""
    data = _read_yaml(REGISTRY / "tensions.yaml")
    tensions = data.get("tensions", [])
    if not tensions:
        return "No open tensions."
    lines = []
    for t in tensions:
        status = t.get("status", "?")
        lines.append(
            f"- **{t['id']}** [{status}] priority={t.get('priority', 0)} — {t.get('title', '?')}\n"
            f"  {t.get('description', '').strip()}"
        )
    return "\n".join(lines)


@mcp.tool()
def org_read_attractors() -> str:
    """Read strategic attractors/goals from registry/attractors.yaml."""
    data = _read_yaml(REGISTRY / "attractors.yaml")
    attractors = data.get("attractors", [])
    if not attractors:
        return "No attractors defined."
    lines = []
    for a in attractors:
        lines.append(
            f"- **{a['id']}** weight={a.get('weight', 0)} [{a.get('status', '?')}] — {a.get('title', '?')}\n"
            f"  {a.get('description', '').strip()}"
        )
    return "\n".join(lines)


@mcp.tool()
def org_read_agents() -> str:
    """Read registered agents from registry/agents.yaml."""
    data = _read_yaml(REGISTRY / "agents.yaml")
    agents = data.get("agents", [])
    if not agents:
        return "No agents registered."
    lines = []
    for a in agents:
        skills = ", ".join(a.get("skills", []))
        lines.append(
            f"- **{a['id']}** ({a.get('type', '?')}) [{a.get('status', '?')}] "
            f"capacity={a.get('capacity', '?')} — {a.get('name', '?')}\n"
            f"  skills: {skills}"
        )
    return "\n".join(lines)


@mcp.tool()
def org_read_health() -> str:
    """Calculate live health metrics from registry data.

    Computes metrics from state.yaml, artifacts.jsonl, and tensions.yaml
    instead of reading a static file.
    """
    # Process states
    state_data = _read_yaml(REGISTRY / "state.yaml")
    processes = state_data.get("processes", {})
    active = sum(1 for p in processes.values() if p.get("state") not in ("COMMITTED", "ABANDONED"))
    committed = sum(1 for p in processes.values() if p.get("state") == "COMMITTED")

    # Audit chain
    entries = _read_jsonl(REGISTRY / "artifacts.jsonl")
    chain_length = len(entries)

    # Chain integrity (quick check)
    chained = [e for e in entries if "entry_hash" in e]
    chain_valid = True
    for i, entry in enumerate(chained):
        if i > 0 and entry.get("prev_hash") != chained[i - 1].get("entry_hash"):
            chain_valid = False
            break
        content = {k: v for k, v in entry.items() if k != "entry_hash"}
        chain_input = entry.get("prev_hash", "") + _canonical_json(content)
        if _hash_content(chain_input) != entry.get("entry_hash"):
            chain_valid = False
            break

    # Average cycle time for COMMITTED processes
    cycle_times = []
    for pid, info in processes.items():
        if info.get("state") != "COMMITTED":
            continue
        proc_entries = [e for e in entries if e.get("process_id") == pid and "timestamp" in e]
        if len(proc_entries) >= 2:
            try:
                first = datetime.fromisoformat(proc_entries[0]["timestamp"].replace("Z", "+00:00"))
                last = datetime.fromisoformat(proc_entries[-1]["timestamp"].replace("Z", "+00:00"))
                hours = (last - first).total_seconds() / 3600
                if hours > 0:
                    cycle_times.append(hours)
            except (ValueError, KeyError):
                continue
    avg_cycle = round(sum(cycle_times) / len(cycle_times), 1) if cycle_times else 0

    # Open tensions
    tension_data = _read_yaml(REGISTRY / "tensions.yaml")
    open_tensions = sum(1 for t in tension_data.get("tensions", []) if t.get("status") == "open")

    lines = [
        f"- **active_processes**: {active}",
        f"- **committed_total**: {committed}",
        f"- **avg_cycle_time_hours**: {avg_cycle}",
        f"- **chain_length**: {chain_length}",
        f"- **chain_integrity**: {'valid' if chain_valid else 'BROKEN'}",
        f"- **open_tensions**: {open_tensions}",
    ]
    return "\n".join(lines)


@mcp.tool()
def org_read_process(process_id: str) -> str:
    """Read all artifacts for a specific process.

    Args:
        process_id: Process ID (e.g., 'PERM-001')
    """
    ok, msg = _validate_process_id(process_id)
    if not ok:
        return msg

    proc_dir = PROCESSES / process_id
    if not proc_dir.exists():
        return f"Process {process_id} not found."

    # Get state
    state_data = _read_yaml(REGISTRY / "state.yaml")
    proc_state = state_data.get("processes", {}).get(process_id, {})

    lines = [f"# {process_id}\n"]
    if proc_state:
        lines.append(f"**State:** {proc_state.get('state', '?')}")
        lines.append(f"**Assigned:** {proc_state.get('assigned_to', '?')}")
        lines.append(f"**Priority:** {proc_state.get('priority', '?')}")
        lines.append(f"**Template:** {proc_state.get('template', '?')}")
        if proc_state.get("notes"):
            lines.append(f"**Notes:** {proc_state['notes']}")
        lines.append("")

    # List artifacts
    artifacts = sorted(proc_dir.iterdir())
    for artifact in artifacts:
        if artifact.is_file():
            lines.append(f"## {artifact.name}\n")
            content = artifact.read_text()
            if len(content) > 3000:
                content = content[:3000] + "\n...(truncated)"
            lines.append(content)
            lines.append("")

    return "\n".join(lines)


@mcp.tool()
def org_read_artifacts(limit: int = 20) -> str:
    """Read recent entries from the immutable artifact log.

    Args:
        limit: Maximum number of entries to return (default 20, most recent first)
    """
    entries = _read_jsonl(REGISTRY / "artifacts.jsonl")
    if not entries:
        return "No artifacts logged."
    recent = entries[-limit:]
    recent.reverse()
    lines = []
    for e in recent:
        agent = e.get("agent", "?")
        action = e.get("action", "?")
        desc = e.get("description", "")
        ts = e.get("timestamp", "?")
        pid = e.get("process_id", "")
        prefix = f"[{pid}] " if pid else ""
        lines.append(f"- `{ts}` **{agent}** {prefix}{action} — {desc}")
    return "\n".join(lines)


# --- WRITE tools ---

@mcp.tool()
def org_update_state(
    process_id: str,
    state: str,
    assigned_to: str = "",
    notes: str = "",
) -> str:
    """Update the state of a process in registry/state.yaml.

    Args:
        process_id: Process ID (e.g., 'PERM-001')
        state: New state (P_COMPLETE, V_COMPLETE, P_READY, COMMITTED, ABANDONED)
        assigned_to: Agent ID to assign to (optional, keeps current if empty)
        notes: Optional notes about the state change
    """
    ok, msg = _validate_process_id(process_id)
    if not ok:
        return msg
    if assigned_to:
        ok, msg = _validate_agent_id(assigned_to)
        if not ok:
            return msg

    # Enforce P↔V protocol: COMMITTED requires at least one V-step
    if state == "COMMITTED":
        config = _load_config()
        if config.get("process_engine", {}).get("enforce_transitions", True):
            if not _has_v_step(process_id):
                return (
                    f"Cannot commit {process_id}: no V-step found in audit trail. "
                    f"The P↔V protocol requires at least one validation step before commit. "
                    f"(Disable with process_engine.enforce_transitions: false in config.yaml)"
                )

    storage_mode = _get_state_storage_mode()

    if storage_mode == "per_process":
        # Auto-migrate monolithic data if switching to per_process for the first time
        _auto_migrate_if_needed()
        # Per-process mode: read/write from processes/{ID}/state.yaml
        per_process_path = PROCESSES / process_id / "state.yaml"
        proc = _read_yaml(per_process_path)
        if not proc:
            return f"Process {process_id} not found. Use org_create_process first."

        old_state = proc.get("state", "?")
        proc["state"] = state
        proc["last_updated"] = _now_iso()
        if assigned_to:
            proc["assigned_to"] = assigned_to
        if notes:
            proc["notes"] = notes

        _write_yaml(per_process_path, proc)
        _regenerate_state_index()
    else:
        # Monolithic mode: read/write from registry/state.yaml
        path = REGISTRY / "state.yaml"
        data = _read_yaml(path)
        if "processes" not in data:
            data["processes"] = {}

        if process_id not in data["processes"]:
            return f"Process {process_id} not found in state.yaml. Use org_create_process first."

        proc = data["processes"][process_id]
        old_state = proc.get("state", "?")
        proc["state"] = state
        proc["last_updated"] = _now_iso()
        if assigned_to:
            proc["assigned_to"] = assigned_to
        if notes:
            proc["notes"] = notes

        _write_yaml(path, data)

    logger.info(f"State updated: {process_id} {old_state} -> {state}")
    return f"Updated {process_id}: {old_state} → {state}"


@mcp.tool()
def org_create_process(
    process_id: str,
    template: str,
    title: str,
    description: str,
    agent_id: str,
    priority: float = 0.5,
    source_repo: str = "",
    tension_id: str = "",
    depends_on: str = "",
) -> str:
    """Create a new process with P.0 artifact and register it in state.yaml.

    Args:
        process_id: Unique process ID (e.g., 'TG-002', 'ORG-001')
        template: Template to use ('feature' or 'bugfix')
        title: Short title for the process
        description: Detailed description of what needs to be done
        agent_id: Agent creating this process (e.g., 'coder', 'reviewer')
        priority: Priority score 0.0-1.0 (default 0.5)
        source_repo: Related repository name (optional)
        tension_id: Related tension ID (optional)
        depends_on: Comma-separated process IDs this process depends on (optional)
    """
    ok, msg = _validate_process_id(process_id)
    if not ok:
        return msg
    ok, msg = _validate_agent_id(agent_id)
    if not ok:
        return msg

    # Parse and validate dependencies
    dep_list = [d.strip() for d in depends_on.split(",") if d.strip()] if depends_on else []
    if dep_list:
        ok, msg = _validate_dependencies(process_id, dep_list)
        if not ok:
            return msg

    proc_dir = PROCESSES / process_id
    if proc_dir.exists():
        return f"Process {process_id} already exists."

    proc_dir.mkdir(parents=True)

    # Determine P.0 artifact name from template
    tmpl = _read_yaml(PROTOCOL / "process_templates" / f"{template}.yaml")
    first_step = tmpl.get("steps", [{}])[0]
    artifact_name = first_step.get("artifact", "P.0_proposal.md")

    # Create P.0 artifact
    p0_content = f"""# {process_id}: {title}

**Process:** {tmpl.get('name', template)}
**Agent:** {agent_id}
**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')}
{f'**Repo:** {source_repo}' if source_repo else ''}
{f'**Tension:** {tension_id}' if tension_id else ''}

## Description

{description}
"""
    (proc_dir / artifact_name).write_text(p0_content)

    # Build the process state dict
    process_state = {
        "state": "P_COMPLETE",
        "p_step": 0,
        "v_step": 0,
        "assigned_to": agent_id,
        "priority": priority,
        "created_at": _now_iso(),
        "last_updated": _now_iso(),
        "template": template,
    }
    if source_repo:
        process_state["source_repo"] = source_repo
    if tension_id:
        process_state["tension"] = tension_id
    if dep_list:
        process_state["depends_on"] = dep_list

    storage_mode = _get_state_storage_mode()

    if storage_mode == "per_process":
        # Auto-migrate monolithic data if switching to per_process for the first time
        _auto_migrate_if_needed()
        # Per-process mode: write to processes/{ID}/state.yaml
        per_process_state = {"process_id": process_id, **process_state}
        _write_yaml(PROCESSES / process_id / "state.yaml", per_process_state)
        _regenerate_state_index()
    else:
        # Monolithic mode: write to registry/state.yaml
        state_data = _read_yaml(REGISTRY / "state.yaml")
        if "processes" not in state_data:
            state_data["processes"] = {}
        state_data["processes"][process_id] = process_state
        _write_yaml(REGISTRY / "state.yaml", state_data)

    # Log artifact (safe: state YAML already written atomically)
    _safe_audit_append({
        "type": "p_step",
        "agent": agent_id,
        "process_id": process_id,
        "action": first_step.get("name", "P.0"),
        "description": title,
        "priority": priority,
        "timestamp": _now_iso(),
    })

    if dep_list:
        _regenerate_fdm_register()

    logger.info(f"Process created: {process_id} by {agent_id}")
    return f"Created {process_id} ({template}) — {artifact_name} written, state=P_COMPLETE"


@mcp.tool()
def org_add_dependency(process_id: str, depends_on_id: str) -> str:
    """Add a dependency between two existing processes.

    Args:
        process_id: Process that depends on another (e.g., 'FEAT-002')
        depends_on_id: Process being depended upon (e.g., 'FEAT-001')
    """
    ok, msg = _validate_process_id(process_id)
    if not ok:
        return msg
    ok, msg = _validate_process_id(depends_on_id)
    if not ok:
        return msg

    # Self-reference check
    if process_id == depends_on_id:
        return f"Process {process_id} cannot depend on itself"

    storage_mode = _get_state_storage_mode()

    if storage_mode == "per_process":
        _auto_migrate_if_needed()
        per_process_path = PROCESSES / process_id / "state.yaml"
        proc = _read_yaml(per_process_path)
        if not proc:
            return f"Process {process_id} not found."

        # Check depends_on_id exists
        dep_file = PROCESSES / depends_on_id / "state.yaml"
        if not dep_file.exists():
            mono_state = _read_yaml(REGISTRY / "state.yaml")
            if depends_on_id not in mono_state.get("processes", {}):
                return f"Dependency {depends_on_id} does not exist"

        # Check for duplicate
        current_deps = proc.get("depends_on", [])
        if depends_on_id in current_deps:
            return f"Dependency {depends_on_id} already exists for {process_id}"

        # Add dependency
        current_deps.append(depends_on_id)
        proc["depends_on"] = current_deps

        _write_yaml(per_process_path, proc)
        _regenerate_state_index()
    else:
        path = REGISTRY / "state.yaml"
        data = _read_yaml(path)
        proc = data.get("processes", {}).get(process_id)
        if not proc:
            return f"Process {process_id} not found."

        # Check depends_on_id exists
        if depends_on_id not in data.get("processes", {}):
            return f"Dependency {depends_on_id} does not exist"

        # Check for duplicate
        current_deps = proc.get("depends_on", [])
        if depends_on_id in current_deps:
            return f"Dependency {depends_on_id} already exists for {process_id}"

        # Add dependency
        current_deps.append(depends_on_id)
        proc["depends_on"] = current_deps

        _write_yaml(path, data)

    # Audit trail (safe: state YAML already written atomically)
    _safe_audit_append({
        "type": "dependency_add",
        "process_id": process_id,
        "depends_on": depends_on_id,
        "agent": "system",
        "timestamp": _now_iso(),
    })

    _regenerate_fdm_register()

    logger.info(f"Dependency added: {process_id} -> {depends_on_id}")
    return f"Added dependency: {process_id} depends on {depends_on_id}"


@mcp.tool()
def org_remove_dependency(process_id: str, depends_on_id: str) -> str:
    """Remove a dependency between two existing processes.

    Args:
        process_id: Process to remove dependency from (e.g., 'FEAT-002')
        depends_on_id: Process to remove from depends_on list (e.g., 'FEAT-001')
    """
    ok, msg = _validate_process_id(process_id)
    if not ok:
        return msg
    ok, msg = _validate_process_id(depends_on_id)
    if not ok:
        return msg

    storage_mode = _get_state_storage_mode()

    if storage_mode == "per_process":
        _auto_migrate_if_needed()
        per_process_path = PROCESSES / process_id / "state.yaml"
        proc = _read_yaml(per_process_path)
        if not proc:
            return f"Process {process_id} not found."

        current_deps = proc.get("depends_on", [])
        if depends_on_id not in current_deps:
            return f"Dependency {depends_on_id} not found for {process_id}"

        current_deps.remove(depends_on_id)
        if current_deps:
            proc["depends_on"] = current_deps
        else:
            proc.pop("depends_on", None)

        _write_yaml(per_process_path, proc)
        _regenerate_state_index()
    else:
        path = REGISTRY / "state.yaml"
        data = _read_yaml(path)
        proc = data.get("processes", {}).get(process_id)
        if not proc:
            return f"Process {process_id} not found."

        current_deps = proc.get("depends_on", [])
        if depends_on_id not in current_deps:
            return f"Dependency {depends_on_id} not found for {process_id}"

        current_deps.remove(depends_on_id)
        if current_deps:
            proc["depends_on"] = current_deps
        else:
            proc.pop("depends_on", None)

        _write_yaml(path, data)

    # Audit trail (safe: state YAML already written atomically)
    _safe_audit_append({
        "type": "dependency_remove",
        "process_id": process_id,
        "depends_on": depends_on_id,
        "agent": "system",
        "timestamp": _now_iso(),
    })

    _regenerate_fdm_register()

    logger.info(f"Dependency removed: {process_id} -/-> {depends_on_id}")
    return f"Removed dependency: {process_id} no longer depends on {depends_on_id}"


@mcp.tool()
def org_analyze_dependencies() -> str:
    """Analyze the dependency graph across all processes.

    Builds a dependency graph from all process states and returns:
    - Parallel groups (which processes can run simultaneously)
    - Cycle detection with resolution proposals
    - Critical path (topological ordering)
    - Bottleneck process with impact scores
    """
    data = _aggregate_per_process_states()
    processes = data.get("processes", {})

    if not processes:
        return "No processes found."

    graph = DependencyGraph()

    # Add all processes as nodes
    for pid in processes:
        graph.add_node(pid)

    # Add edges from depends_on
    has_deps = False
    for pid, pdata in processes.items():
        deps = pdata.get("depends_on", [])
        for dep in deps:
            graph.add_edge(pid, dep)
            has_deps = True

    result = graph.analyze()

    lines: list[str] = ["=== Dependency Analysis ===", ""]

    # --- Parallel Groups ---
    lines.append("--- Parallel Groups ---")
    groups = result["parallel_groups"]
    if groups:
        for i, group in enumerate(groups):
            label = "can start immediately" if i == 0 else f"after group {i}"
            lines.append(f"Group {i + 1} ({label}): {', '.join(group)}")
    else:
        lines.append("No groups (no processes).")
    lines.append("")

    # --- Cycles ---
    lines.append("--- Cycles ---")
    cycles = result["cycles"]
    if not cycles:
        lines.append("No circular dependencies detected.")
    else:
        for cyc in cycles:
            nodes = cyc["nodes"]
            cycle_str = " -> ".join(nodes) + " -> " + nodes[0]
            lines.append(f"Cycle: {cycle_str}")
            we = cyc.get("weakest_edge")
            if we:
                lines.append(f"  Suggested resolution: Remove dependency {we[0]} -> {we[1]} (most recently added)")
    lines.append("")

    # --- Critical Path ---
    lines.append("--- Critical Path ---")
    topo = result["critical_path"]
    if topo:
        lines.append(" -> ".join(topo))
    else:
        lines.append("No critical path (no dependencies).")
    lines.append("")

    # --- Bottleneck ---
    lines.append("--- Bottleneck ---")
    bottleneck = result["bottleneck"]
    scores = result["impact_scores"]
    if bottleneck:
        score = scores.get(bottleneck, 0)
        lines.append(f"{bottleneck} ({score} downstream dependents)")
        score_parts = [f"{k}={v}" for k, v in sorted(scores.items(), key=lambda x: -x[1])]
        lines.append(f"Impact scores: {', '.join(score_parts)}")
    else:
        lines.append("No bottleneck identified.")
    lines.append("")

    # --- Summary ---
    lines.append("--- Summary ---")
    dep_count = sum(len(pdata.get("depends_on", [])) for pdata in processes.values())
    cycle_count = len(cycles)
    lines.append(f"Processes: {len(processes)} | Dependencies: {dep_count} | Parallel groups: {len(groups)} | Cycles: {cycle_count}")

    return "\n".join(lines)


@mcp.tool()
def org_read_dependencies() -> str:
    """Read the current FDM dependency register (registry/fdm.json).

    Returns the cached dependency analysis including nodes, edges,
    parallel groups, cycles, critical path, bottleneck, and impact scores.
    """
    fdm_path = REGISTRY / "fdm.json"
    if not fdm_path.exists():
        return "No FDM register found. Dependencies have not been configured yet."

    try:
        register = json.loads(fdm_path.read_text())
    except (json.JSONDecodeError, ValueError):
        return "FDM register is corrupted. Run a dependency mutation to regenerate."

    lines: list[str] = ["=== FDM Dependency Register ===", ""]

    # Generated at
    lines.append(f"Generated: {register.get('generated_at', 'unknown')}")
    lines.append("")

    # Nodes
    nodes = register.get("nodes", {})
    lines.append(f"--- Nodes ({len(nodes)}) ---")
    for nid, ndata in sorted(nodes.items()):
        state = ndata.get("state", "unknown")
        lines.append(f"  {nid}: {state}")
    lines.append("")

    # Edges
    edges = register.get("edges", [])
    lines.append(f"--- Edges ({len(edges)}) ---")
    if edges:
        for edge in edges:
            lines.append(f"  {edge[0]} -> {edge[1]}")
    else:
        lines.append("  No dependencies configured.")
    lines.append("")

    # Parallel Groups
    groups = register.get("parallel_groups", [])
    lines.append("--- Parallel Groups ---")
    if groups:
        for i, group in enumerate(groups):
            label = "can start immediately" if i == 0 else f"after group {i}"
            lines.append(f"  Group {i + 1} ({label}): {', '.join(group)}")
    else:
        lines.append("  No groups.")
    lines.append("")

    # Cycles
    cycles = register.get("cycles", [])
    lines.append("--- Cycles ---")
    if not cycles:
        lines.append("  No circular dependencies detected.")
    else:
        for cyc in cycles:
            cyc_nodes = cyc["nodes"]
            cycle_str = " -> ".join(cyc_nodes) + " -> " + cyc_nodes[0]
            lines.append(f"  Cycle: {cycle_str}")
            we = cyc.get("weakest_edge")
            if we:
                lines.append(f"    Suggested resolution: Remove {we[0]} -> {we[1]}")
    lines.append("")

    # Critical Path
    lines.append("--- Critical Path ---")
    cpath = register.get("critical_path", [])
    if cpath:
        lines.append(f"  {' -> '.join(cpath)}")
    else:
        lines.append("  No critical path.")
    lines.append("")

    # Bottleneck
    lines.append("--- Bottleneck ---")
    bottleneck = register.get("bottleneck")
    scores = register.get("impact_scores", {})
    if bottleneck:
        score = scores.get(bottleneck, 0)
        lines.append(f"  {bottleneck} ({score} downstream dependents)")
    else:
        lines.append("  No bottleneck identified.")
    lines.append("")

    # Impact Scores
    lines.append("--- Impact Scores ---")
    if scores:
        for k, v in sorted(scores.items(), key=lambda x: -x[1]):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  No scores available.")

    return "\n".join(lines)


@mcp.tool()
def org_log_artifact(
    agent_id: str,
    action: str,
    description: str,
    process_id: str = "",
    extra: str = "",
) -> str:
    """Append an entry to the immutable artifact log (artifacts.jsonl).

    Args:
        agent_id: Agent performing the action (e.g., 'coder', 'reviewer', 'alice')
        action: Action name (e.g., 'V.0_review', 'P.1_implementation')
        description: What was done
        process_id: Related process ID (optional)
        extra: Additional JSON fields as a JSON string (optional)
    """
    ok, msg = _validate_agent_id(agent_id, allow_bootstrap=True)
    if not ok:
        return msg
    if process_id:
        ok, msg = _validate_process_id(process_id)
        if not ok:
            return msg

    entry = {
        "type": "v_step" if action.startswith("V") else "p_step" if action.startswith("P") else "action",
        "agent": agent_id,
        "action": action,
        "description": description,
        "timestamp": _now_iso(),
    }
    if process_id:
        entry["process_id"] = process_id
    if extra:
        try:
            extra_data = json.loads(extra)
            entry.update(extra_data)

            # Auto-calculate E(x) for V-steps with convergence scores
            if action.startswith("V") and "convergence" in extra_data:
                conv = extra_data["convergence"]
                config = _load_config()
                energy_cfg = config.get("energy", {})
                weights = energy_cfg.get("weights", {})

                wg = weights.get("gaps", 0.30)
                wi = weights.get("inconsistencies", 0.30)
                wu = weights.get("uncertainty", 0.25)
                we = weights.get("evidence", 0.15)

                g = conv.get("gaps", 0.0)
                i = conv.get("inconsistencies", 0.0)
                u = conv.get("uncertainty", 0.0)
                e = conv.get("evidence", 0.0)

                energy = max(0.0, wg * g**2 + wi * i**2 + wu * u**2 - we * e**2)
                entry["energy_score"] = round(energy, 4)
        except json.JSONDecodeError:
            pass

    _append_jsonl(REGISTRY / "artifacts.jsonl", entry)
    logger.info(f"Artifact logged: {agent_id} {action}")
    return f"Logged: {agent_id} — {action}"


@mcp.tool()
def org_create_tension(
    title: str,
    description: str,
    priority: float = 0.5,
    source_repo: str = "",
) -> str:
    """Register a new tension (unresolved problem or opportunity).

    Args:
        title: Short title for the tension
        description: Detailed description
        priority: Priority score 0.0-1.0 (default 0.5)
        source_repo: Related repository (optional)
    """
    data = _read_yaml(REGISTRY / "tensions.yaml")
    if "tensions" not in data:
        data["tensions"] = []

    # Generate ID
    year = datetime.now().year
    existing_ids = [t.get("id", "") for t in data["tensions"]]
    n = 1
    while f"T-{year}-{n:03d}" in existing_ids:
        n += 1
    tension_id = f"T-{year}-{n:03d}"

    tension = {
        "id": tension_id,
        "title": title,
        "description": description,
        "created_at": _now_iso(),
        "priority": priority,
        "status": "open",
    }
    if source_repo:
        tension["source_repo"] = source_repo

    data["tensions"].append(tension)
    _write_yaml(REGISTRY / "tensions.yaml", data)

    logger.info(f"Tension created: {tension_id}")
    return f"Created tension {tension_id}: {title}"


@mcp.tool()
def org_resolve_tension(tension_id: str, resolution: str = "") -> str:
    """Mark a tension as resolved.

    Args:
        tension_id: Tension ID (e.g., 'T-2026-001')
        resolution: How it was resolved (optional)
    """
    data = _read_yaml(REGISTRY / "tensions.yaml")
    tensions = data.get("tensions", [])

    for t in tensions:
        if t.get("id") == tension_id:
            t["status"] = "resolved"
            t["resolved_at"] = _now_iso()
            if resolution:
                t["resolution"] = resolution
            _write_yaml(REGISTRY / "tensions.yaml", data)
            logger.info(f"Tension resolved: {tension_id}")
            return f"Resolved {tension_id}"

    return f"Tension {tension_id} not found."


@mcp.tool()
def org_verify_chain(file: str = "artifacts.jsonl") -> str:
    """Verify the hash-chain integrity of a JSONL audit log.

    Walks every entry and re-computes entry_hash from prev_hash + content.
    Reports any broken links (tampered, inserted, or deleted entries).

    Args:
        file: JSONL filename in registry/ (default: artifacts.jsonl)
    """
    path = REGISTRY / file
    entries = _read_jsonl(path)
    if not entries:
        return "No entries to verify."

    # Count legacy entries (before hash-chaining was added)
    legacy = sum(1 for e in entries if "entry_hash" not in e)
    chained = [e for e in entries if "entry_hash" in e]

    if not chained:
        return f"All {len(entries)} entries are legacy (pre-hash-chain). No chain to verify."

    broken = []
    expected_prev = chained[0].get("prev_hash", "0" * 64)

    for i, entry in enumerate(chained):
        stored_hash = entry.get("entry_hash", "")
        stored_prev = entry.get("prev_hash", "")

        # Verify prev_hash links to previous entry_hash
        if i > 0 and stored_prev != chained[i - 1].get("entry_hash", ""):
            broken.append(f"Entry {legacy + i}: prev_hash mismatch (chain broken)")

        # Re-compute entry_hash
        content = {k: v for k, v in entry.items() if k != "entry_hash"}
        chain_input = stored_prev + _canonical_json(content)
        recomputed = _hash_content(chain_input)

        if recomputed != stored_hash:
            broken.append(
                f"Entry {legacy + i}: entry_hash mismatch "
                f"(stored={stored_hash[:12]}..., recomputed={recomputed[:12]}...)"
            )

    total = len(entries)
    tip = chained[-1].get("entry_hash", "?")[:16]
    result = f"Verified {len(chained)} chained entries ({legacy} legacy, {total} total).\n"
    result += f"Chain tip: {tip}...\n"

    if broken:
        result += f"\n**{len(broken)} INTEGRITY VIOLATIONS:**\n"
        for b in broken:
            result += f"  - {b}\n"
    else:
        result += "**Chain integrity: VALID** — no tampering detected."

    return result


@mcp.tool()
def org_calculate_priority(
    urgency: float = 0.5,
    commitment: float = 0.5,
    demand: float = 0.5,
    blocking: float = 0.5,
) -> str:
    """Calculate Hamiltonian priority H(s) using protocol weights.

    Args:
        urgency: How time-sensitive (0.0-1.0)
        commitment: How committed we are (0.0-1.0)
        demand: External demand/need (0.0-1.0)
        blocking: How much this blocks other work (0.0-1.0)
    """
    config = _load_config()
    weights = config.get("hamiltonian", {}).get("weights", {})
    thresholds = config.get("hamiltonian", {}).get("thresholds", {})

    w_u = weights.get("urgency", 0.25)
    w_c = weights.get("commitment", 0.25)
    w_d = weights.get("demand", 0.25)
    w_b = weights.get("blocking", 0.25)

    h = w_u * urgency + w_c * commitment + w_d * demand + w_b * blocking

    action_trigger = thresholds.get("action_trigger", 0.5)
    escalation = thresholds.get("escalation", 0.8)

    status = "ESCALATE to human" if h >= escalation else "ACTION required" if h >= action_trigger else "LOW priority"

    return (
        f"H(s) = {h:.3f}\n"
        f"  urgency={urgency}×{w_u} + commitment={commitment}×{w_c} + "
        f"demand={demand}×{w_d} + blocking={blocking}×{w_b}\n"
        f"  → {status} (action≥{action_trigger}, escalation≥{escalation})"
    )


@mcp.tool()
def org_calculate_energy(
    gaps: float = 0.0,
    inconsistencies: float = 0.0,
    uncertainty: float = 0.0,
    evidence: float = 0.0,
    w_gaps: float = 0.0,
    w_inconsistencies: float = 0.0,
    w_uncertainty: float = 0.0,
    w_evidence: float = 0.0,
) -> str:
    """Calculate semantic energy E(x) for a process state.

    E(x) = w_g*gaps^2 + w_i*inconsistencies^2 + w_u*uncertainty^2 - w_e*evidence^2

    Low E(x) = close to convergence. High E(x) = far from attractor.
    Unlike H(s) (linear priority), E(x) penalizes large deviations
    quadratically — one critical gap matters more than three minor ones.

    Args:
        gaps: Missing information score (0.0-1.0)
        inconsistencies: Contradictions score (0.0-1.0)
        uncertainty: Unknowns score (0.0-1.0)
        evidence: Supporting evidence score (0.0-1.0)
        w_gaps: Weight for gaps (0 = use config default)
        w_inconsistencies: Weight for inconsistencies (0 = use config default)
        w_uncertainty: Weight for uncertainty (0 = use config default)
        w_evidence: Weight for evidence (0 = use config default)
    """
    config = _load_config()
    energy_cfg = config.get("energy", {})
    weights = energy_cfg.get("weights", {})
    thresholds = energy_cfg.get("thresholds", {})

    wg = w_gaps or weights.get("gaps", 0.30)
    wi = w_inconsistencies or weights.get("inconsistencies", 0.30)
    wu = w_uncertainty or weights.get("uncertainty", 0.25)
    we = w_evidence or weights.get("evidence", 0.15)

    e = wg * gaps**2 + wi * inconsistencies**2 + wu * uncertainty**2 - we * evidence**2
    e = max(e, 0.0)  # floor at 0

    convergence_t = thresholds.get("convergence", 0.10)
    minor_t = thresholds.get("minor_revision", 0.30)

    if e < convergence_t:
        status = "READY to commit"
    elif e < minor_t:
        status = "MINOR revision needed"
    else:
        status = "MAJOR revision needed"

    # Find dominant tension component
    components = {
        "gaps": wg * gaps**2,
        "inconsistencies": wi * inconsistencies**2,
        "uncertainty": wu * uncertainty**2,
    }
    dominant = max(components, key=components.get)

    return (
        f"E(x) = {e:.4f}\n"
        f"  gaps={gaps}^2*{wg} + inconsistencies={inconsistencies}^2*{wi} + "
        f"uncertainty={uncertainty}^2*{wu} - evidence={evidence}^2*{we}\n"
        f"  Dominant tension: {dominant} ({components[dominant]:.4f})\n"
        f"  -> {status} (convergence<{convergence_t}, minor<{minor_t})"
    )


@mcp.tool()
def org_read_convergence(process_id: str) -> str:
    """Read convergence history for a process.

    Shows E(x) at each V-step, delta between iterations,
    and whether the process is converging, stagnating, or diverging.

    Args:
        process_id: Process ID (e.g., 'FEAT-001')
    """
    ok, msg = _validate_process_id(process_id)
    if not ok:
        return msg

    entries = _read_jsonl(REGISTRY / "artifacts.jsonl")
    energy_entries = [
        e for e in entries
        if e.get("process_id") == process_id and "energy_score" in e
    ]

    if not energy_entries:
        return f"No convergence data for {process_id}. V-steps have not included energy scores."

    lines = [f"# Convergence history: {process_id}\n"]
    prev_e = None
    deltas = []

    for e in energy_entries:
        score = e["energy_score"]
        action = e.get("action", "?")
        ts = e.get("timestamp", "?")

        if prev_e is not None:
            delta = score - prev_e
            deltas.append(delta)
            arrow = "v" if delta < 0 else "^" if delta > 0 else "="
            lines.append(f"  {ts}  {action}  E={score:.4f}  {arrow} delta={delta:+.4f}")
        else:
            lines.append(f"  {ts}  {action}  E={score:.4f}  (initial)")

        prev_e = score

    # Classify convergence pattern
    if len(deltas) == 0:
        verdict = "INSUFFICIENT DATA (only 1 measurement)"
    elif all(d < -0.005 for d in deltas):
        verdict = "CONVERGING"
    elif all(d > 0.005 for d in deltas):
        verdict = "DIVERGING"
    elif all(abs(d) <= 0.005 for d in deltas):
        verdict = "STAGNATING"
    elif deltas[-1] < -0.005:
        verdict = "CONVERGING (recent)"
    elif deltas[-1] > 0.005:
        verdict = "DIVERGING (recent)"
    else:
        verdict = "MIXED"

    lines.append(f"\n  Verdict: **{verdict}**")
    lines.append(f"  Measurements: {len(energy_entries)}")
    if energy_entries:
        lines.append(f"  Latest E(x): {energy_entries[-1]['energy_score']:.4f}")

    return "\n".join(lines)


@mcp.tool()
def org_git_sync(
    commit_message: str = "",
    agent_id: str = "",
) -> str:
    """Pull latest changes, optionally commit and push.

    Args:
        commit_message: If provided, stages all changes, commits with this message, and pushes. If empty, only pulls.
        agent_id: Agent ID for commit message prefix (e.g., 'coder'). Required if committing.
    """
    try:
        # Always pull first
        pull_output = _git_run("pull", "--rebase", check=False)
        result = f"Pull: {pull_output}\n"

        if commit_message:
            if not agent_id:
                return result + "Error: agent_id required for commit."

            # Git sync always requires a registered agent — even in permissive mode.
            # This is the most impactful operation (commit + push to remote).
            known = _get_known_agent_ids()
            if agent_id not in known:
                _log_security_event(agent_id, "git_sync_rejected",
                    f"agent_id '{agent_id}' not registered — git sync requires a known agent")
                return result + f"Error: agent_id '{agent_id}' not registered in agents.yaml. Git sync requires a registered agent."

            # Stage all changes
            _git_run("add", "-A")

            # Check if there are changes to commit
            status = _git_run("status", "--porcelain")
            if not status:
                return result + "Nothing to commit."

            # Commit (sanitize to prevent git trailer injection)
            full_msg = f"{agent_id}: {_sanitize_commit_message(commit_message)}"
            _git_run("commit", "-m", full_msg)
            result += f"Committed: {full_msg}\n"

            # Push
            push_output = _git_run("push", check=False)
            result += f"Push: {push_output or 'OK'}"

        return result

    except Exception as e:
        return f"Git error: {e}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Fields that Gemini Function Calling does NOT support in JSON Schema.
# See: https://ai.google.dev/gemini-api/docs/function-calling
_GEMINI_UNSUPPORTED_FIELDS = {
    "additionalProperties", "title", "$defs", "$ref",
    "allOf", "anyOf", "oneOf", "not",
    "patternProperties", "minItems", "maxItems",
    "minimum", "maximum", "pattern", "exclusiveMinimum", "exclusiveMaximum",
}


def _sanitize_schema_for_gemini(schema: dict, _is_properties: bool = False) -> dict:
    """Recursively strip Gemini-incompatible fields from JSON Schema dicts.

    The ``properties`` dict is special: its keys are parameter names (not schema
    keywords), so we must not strip them even if they collide with banned names
    like ``title``.
    """
    if not _is_properties:
        for field in _GEMINI_UNSUPPORTED_FIELDS:
            schema.pop(field, None)
    for key, value in schema.items():
        if isinstance(value, dict):
            _sanitize_schema_for_gemini(value, _is_properties=(key == "properties"))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _sanitize_schema_for_gemini(item)
    return schema


def _sanitize_tool_schemas():
    """Strip Gemini-incompatible fields from all tool parameter schemas."""
    tools = mcp._tool_manager._tools
    for name, tool in tools.items():
        _sanitize_schema_for_gemini(tool.parameters)
        logger.info(f"  Sanitized schema: {name}")


def main():
    if not ORG_REPO.exists():
        print(f"Error: ORG_REPO_PATH={ORG_REPO} does not exist", file=sys.stderr)
        sys.exit(1)

    if not REGISTRY.exists():
        print(f"Error: {REGISTRY} not found — is this an org-as-code repo?", file=sys.stderr)
        sys.exit(1)

    logger.info(f"Org-as-Code MCP server starting — repo: {ORG_REPO}")
    logger.info(f"Tools: {len(mcp._tool_manager._tools)} registered")
    _sanitize_tool_schemas()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
