#!/usr/bin/env python3
"""
org — CLI wrapper for org-as-code registry
============================================
Thin wrapper around org_mcp_server.py helpers.
No code duplication: imports _read_yaml, _write_yaml, etc. directly.

Usage:
    org status                          — All processes + state
    org tensions                        — Open tensions
    org attractors                      — Strategic goals
    org agents                          — Registered agents
    org health                          — Health metrics
    org log [--limit N]                 — Recent artifacts
    org show <PROCESS_ID>               — Process detail + artifacts
    org verify                          — Verify hash-chain integrity
    org dashboard                       — Combined overview (status + tensions + health + deps)

    org create <ID> <template> <title> <description> [--agent A] [--priority N] [--repo R] [--tension T]
    org update <ID> <STATE> [--assign AGENT] [--notes TEXT]
    org artifact <agent> <action> <description> [--process ID] [--extra JSON]

    org tension-add <title> <description> [--priority N] [--repo R]
    org tension-resolve <ID> [resolution]

    org priority [--urgency N] [--commitment N] [--demand N] [--blocking N]

    org deps-add <PROC-A> <PROC-B>     — Add dependency (A depends on B)
    org deps-remove <PROC-A> <PROC-B>  — Remove dependency
    org deps <PROCESS_ID>              — Show dependencies for a process
    org deps-analyze                    — Full dependency analysis (FDM)

    org sync                            — Pull only
    org sync <message> [--agent ID]     — Commit + push
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Import helpers from the MCP server (same repo)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from org_mcp_server import (
    PROCESSES,
    PROTOCOL,
    REGISTRY,
    _append_jsonl,
    _canonical_json,
    _git_run,
    _hash_content,
    _load_config,
    _now_iso,
    _read_jsonl,
    _read_yaml,
    _write_yaml,
)

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------

class C:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    RESET = "\033[0m"


STATE_COLORS = {
    "COMMITTED": C.GREEN,
    "P_COMPLETE": C.YELLOW,
    "V_COMPLETE": C.CYAN,
    "P_READY": C.BLUE,
    "ABANDONED": C.DIM,
}

TENSION_COLORS = {
    "open": C.RED,
    "resolved": C.GREEN,
}


# ---------------------------------------------------------------------------
# Read commands
# ---------------------------------------------------------------------------

def cmd_status(args):
    data = _read_yaml(REGISTRY / "state.yaml")
    processes = data.get("processes", {})
    if not processes:
        print("No active processes.")
        return
    for pid, info in processes.items():
        state = info.get("state", "?")
        color = STATE_COLORS.get(state, "")
        assigned = info.get("assigned_to", "?")
        priority = info.get("priority", 0)
        notes = info.get("notes", "")
        line = f"  {C.BOLD}{pid}{C.RESET}  {color}[{state}]{C.RESET}  assigned={assigned}  priority={priority}"
        if notes:
            line += f"\n    {C.DIM}{notes}{C.RESET}"
        print(line)


def cmd_tensions(args):
    data = _read_yaml(REGISTRY / "tensions.yaml")
    tensions = data.get("tensions", [])
    if not tensions:
        print("No tensions.")
        return
    for t in tensions:
        status = t.get("status", "?")
        color = TENSION_COLORS.get(status, "")
        print(f"  {C.BOLD}{t['id']}{C.RESET}  {color}[{status}]{C.RESET}  priority={t.get('priority', 0)}")
        print(f"    {t.get('title', '?')}")
        desc = t.get("description", "").strip()
        if desc:
            print(f"    {C.DIM}{desc}{C.RESET}")


def cmd_attractors(args):
    data = _read_yaml(REGISTRY / "attractors.yaml")
    attractors = data.get("attractors", [])
    if not attractors:
        print("No attractors defined.")
        return
    for a in attractors:
        status = a.get("status", "?")
        weight = a.get("weight", 0)
        bar = "█" * int(weight * 10) + "░" * (10 - int(weight * 10))
        print(f"  {C.BOLD}{a['id']}{C.RESET}  [{bar}] {weight}  [{status}]")
        print(f"    {a.get('title', '?')}")
        desc = a.get("description", "").strip()
        if desc:
            print(f"    {C.DIM}{desc}{C.RESET}")


def cmd_agents(args):
    data = _read_yaml(REGISTRY / "agents.yaml")
    agents = data.get("agents", [])
    if not agents:
        print("No agents registered.")
        return
    for a in agents:
        atype = a.get("type", "?")
        status = a.get("status", "?")
        color = C.GREEN if status == "active" else C.DIM
        skills = ", ".join(a.get("skills", []))
        print(f"  {C.BOLD}{a['id']}{C.RESET}  ({atype})  {color}[{status}]{C.RESET}  capacity={a.get('capacity', '?')}")
        print(f"    {a.get('name', '?')}")
        if skills:
            print(f"    {C.DIM}skills: {skills}{C.RESET}")


def cmd_health(args):
    from org_mcp_server import org_read_health
    result = org_read_health()
    for line in result.split("\n"):
        # Strip markdown bold for terminal display
        line = line.lstrip("- ").replace("**", "")
        if ": " in line:
            k, v = line.split(": ", 1)
            print(f"  {C.BOLD}{k}{C.RESET}: {v}")
        else:
            print(f"  {line}")


def cmd_log(args):
    entries = _read_jsonl(REGISTRY / "artifacts.jsonl")
    if not entries:
        print("No artifacts logged.")
        return
    limit = args.limit
    recent = entries[-limit:]
    recent.reverse()
    for e in recent:
        agent = e.get("agent", "?")
        action = e.get("action", "?")
        desc = e.get("description", "")
        ts = e.get("timestamp", "?")
        pid = e.get("process_id", "")
        prefix = f"{C.CYAN}[{pid}]{C.RESET} " if pid else ""
        print(f"  {C.DIM}{ts}{C.RESET}  {C.BOLD}{agent}{C.RESET}  {prefix}{action} — {desc}")


def cmd_show(args):
    process_id = args.process_id
    proc_dir = PROCESSES / process_id
    if not proc_dir.exists():
        print(f"Process {process_id} not found.")
        return

    state_data = _read_yaml(REGISTRY / "state.yaml")
    proc_state = state_data.get("processes", {}).get(process_id, {})

    state = proc_state.get("state", "?")
    color = STATE_COLORS.get(state, "")
    print(f"\n  {C.BOLD}{process_id}{C.RESET}  {color}[{state}]{C.RESET}")
    if proc_state:
        print(f"  assigned={proc_state.get('assigned_to', '?')}  priority={proc_state.get('priority', '?')}  template={proc_state.get('template', '?')}")
        if proc_state.get("notes"):
            print(f"  {C.DIM}{proc_state['notes']}{C.RESET}")
    print()

    artifacts = sorted(proc_dir.iterdir())
    for artifact in artifacts:
        if artifact.is_file():
            size = artifact.stat().st_size
            print(f"  {C.CYAN}── {artifact.name}{C.RESET}  ({size:,} bytes)")
            content = artifact.read_text()
            if len(content) > 3000:
                content = content[:3000] + "\n  ...(truncated)"
            for line in content.split("\n"):
                print(f"    {line}")
            print()


def cmd_verify(args):
    """Verify the SHA-256 hash-chain integrity of artifacts.jsonl."""
    path = REGISTRY / "artifacts.jsonl"
    if not path.exists():
        print("  No artifacts.jsonl found.")
        return

    entries = _read_jsonl(path)
    if not entries:
        print("  No entries to verify.")
        return

    legacy = sum(1 for e in entries if "entry_hash" not in e)
    chained = [e for e in entries if "entry_hash" in e]

    if not chained:
        print(f"  All {len(entries)} entries are legacy (pre-hash-chain). No chain to verify.")
        return

    broken = []
    for i, entry in enumerate(chained):
        stored_hash = entry.get("entry_hash", "")
        stored_prev = entry.get("prev_hash", "")

        if i > 0 and stored_prev != chained[i - 1].get("entry_hash", ""):
            broken.append(f"Entry {legacy + i}: prev_hash mismatch (chain broken)")

        content = {k: v for k, v in entry.items() if k != "entry_hash"}
        chain_input = stored_prev + _canonical_json(content)
        recomputed = _hash_content(chain_input)

        if recomputed != stored_hash:
            broken.append(
                f"Entry {legacy + i}: hash mismatch "
                f"(stored={stored_hash[:12]}..., expected={recomputed[:12]}...)"
            )

    tip = chained[-1].get("entry_hash", "?")[:16]
    print(f"  Verified {len(chained)} chained entries ({legacy} legacy, {len(entries)} total).")
    print(f"  Chain tip: {tip}...")

    if broken:
        print(f"\n  {C.RED}{C.BOLD}{len(broken)} INTEGRITY VIOLATIONS:{C.RESET}")
        for b in broken:
            print(f"    {C.RED}- {b}{C.RESET}")
    else:
        print(f"  {C.GREEN}{C.BOLD}Chain integrity: VALID{C.RESET} — no tampering detected.")


def cmd_dashboard(args):
    # Header
    print(f"\n  {C.BOLD}{'═' * 50}{C.RESET}")
    print(f"  {C.BOLD}  ORG-AS-CODE DASHBOARD{C.RESET}")
    print(f"  {C.BOLD}{'═' * 50}{C.RESET}\n")

    # Processes
    print(f"  {C.BOLD}{C.BLUE}PROCESSES{C.RESET}")
    data = _read_yaml(REGISTRY / "state.yaml")
    processes = data.get("processes", {})
    if not processes:
        print(f"    {C.DIM}(none){C.RESET}")
    else:
        for pid, info in processes.items():
            state = info.get("state", "?")
            color = STATE_COLORS.get(state, "")
            assigned = info.get("assigned_to", "?")
            priority = info.get("priority", 0)
            print(f"    {C.BOLD}{pid:<12}{C.RESET} {color}{state:<12}{C.RESET} {assigned:<14} p={priority}")
    print()

    # Tensions
    print(f"  {C.BOLD}{C.RED}TENSIONS{C.RESET}")
    tdata = _read_yaml(REGISTRY / "tensions.yaml")
    tensions = tdata.get("tensions", [])
    open_tensions = [t for t in tensions if t.get("status") == "open"]
    if not open_tensions:
        print(f"    {C.GREEN}(no open tensions){C.RESET}")
    else:
        for t in open_tensions:
            print(f"    {C.BOLD}{t['id']}{C.RESET}  p={t.get('priority', 0)}  {t.get('title', '?')}")
    print()

    # Health (computed live)
    print(f"  {C.BOLD}{C.MAGENTA}HEALTH{C.RESET}")
    from org_mcp_server import org_read_health
    health_result = org_read_health()
    for line in health_result.split("\n"):
        line = line.lstrip("- ").replace("**", "")
        if ": " in line:
            k, v = line.split(": ", 1)
            print(f"    {k}: {v}")
    print()

    # Chain integrity (quick check)
    print(f"  {C.BOLD}{C.CYAN}AUDIT CHAIN{C.RESET}")
    entries = _read_jsonl(REGISTRY / "artifacts.jsonl")
    chained = [e for e in entries if "entry_hash" in e]
    if chained:
        tip = chained[-1].get("entry_hash", "?")[:16]
        print(f"    {len(chained)} entries, tip: {tip}...")
    else:
        print(f"    {C.DIM}(no chain){C.RESET}")
    print()

    # Dependencies
    print(f"  {C.BOLD}{C.GREEN}DEPENDENCIES{C.RESET}")
    try:
        from org_mcp_server import org_analyze_dependencies
        dep_result = org_analyze_dependencies()
        if "No processes" in dep_result:
            print(f"    {C.DIM}(no dependency data){C.RESET}")
        else:
            # Parse the Summary line
            for line in dep_result.split("\n"):
                if line.startswith("Processes:"):
                    parts = {p.split(":")[0].strip(): p.split(":")[1].strip() for p in line.split("|")}
                    pg = parts.get("Parallel groups", "0")
                    cy = parts.get("Cycles", "0")
                    print(f"    Parallel groups: {pg} | Cycles: {cy}")
                    if int(cy) > 0:
                        print(f"    {C.RED}WARNING: Circular dependencies detected! Run: org deps-analyze{C.RESET}")
                    break
            else:
                print(f"    {C.DIM}(no dependency data){C.RESET}")
    except Exception:
        print(f"    {C.DIM}(no dependency data){C.RESET}")
    print()

    # Recent activity (last 5)
    print(f"  {C.BOLD}{C.CYAN}RECENT ACTIVITY{C.RESET}")
    recent = entries[-5:]
    recent.reverse()
    if not recent:
        print(f"    {C.DIM}(no artifacts){C.RESET}")
    else:
        for e in recent:
            agent = e.get("agent", "?")
            action = e.get("action", "?")
            pid = e.get("process_id", "")
            prefix = f"[{pid}] " if pid else ""
            print(f"    {C.DIM}{e.get('timestamp', '?')}{C.RESET}  {agent}  {prefix}{action}")

    print(f"\n  {C.BOLD}{'═' * 50}{C.RESET}\n")


# ---------------------------------------------------------------------------
# Write commands
# ---------------------------------------------------------------------------

def cmd_create(args):
    from org_mcp_server import org_create_process
    result = org_create_process(
        process_id=args.process_id,
        template=args.template,
        title=args.title,
        description=args.description,
        agent_id=args.agent,
        priority=args.priority,
        source_repo=args.repo or "",
        tension_id=args.tension or "",
    )
    print(result)


def cmd_update(args):
    from org_mcp_server import org_update_state
    result = org_update_state(
        process_id=args.process_id,
        state=args.state,
        assigned_to=args.assign or "",
        notes=args.notes or "",
    )
    print(result)


def cmd_artifact(args):
    from org_mcp_server import org_log_artifact
    result = org_log_artifact(
        agent_id=args.agent,
        action=args.action,
        description=args.description,
        process_id=args.process or "",
        extra=args.extra or "",
    )
    print(result)


def cmd_tension_add(args):
    from org_mcp_server import org_create_tension
    result = org_create_tension(
        title=args.title,
        description=args.description,
        priority=args.priority,
        source_repo=args.repo or "",
    )
    print(result)


def cmd_tension_resolve(args):
    from org_mcp_server import org_resolve_tension
    result = org_resolve_tension(
        tension_id=args.tension_id,
        resolution=" ".join(args.resolution) if args.resolution else "",
    )
    print(result)


def cmd_priority(args):
    from org_mcp_server import org_calculate_priority
    result = org_calculate_priority(
        urgency=args.urgency,
        commitment=args.commitment,
        demand=args.demand,
        blocking=args.blocking,
    )
    print(result)


def cmd_energy(args):
    from org_mcp_server import org_calculate_energy
    result = org_calculate_energy(
        gaps=args.gaps,
        inconsistencies=args.inconsistencies,
        uncertainty=args.uncertainty,
        evidence=args.evidence,
    )
    print(result)


def cmd_convergence(args):
    from org_mcp_server import org_read_convergence
    result = org_read_convergence(process_id=args.process_id)
    print(result)


def cmd_sync(args):
    from org_mcp_server import org_git_sync
    message = " ".join(args.message) if args.message else ""
    result = org_git_sync(
        commit_message=message,
        agent_id=args.agent or "",
    )
    print(result)


# ---------------------------------------------------------------------------
# Dependency commands
# ---------------------------------------------------------------------------

def cmd_deps_add(args):
    from org_mcp_server import org_add_dependency
    result = org_add_dependency(args.process_id, args.depends_on_id)
    print(result)


def cmd_deps_remove(args):
    from org_mcp_server import org_remove_dependency
    result = org_remove_dependency(args.process_id, args.depends_on_id)
    print(result)


def cmd_deps(args):
    from org_mcp_server import _aggregate_per_process_states
    process_id = args.process_id
    data = _aggregate_per_process_states()
    processes = data.get("processes", {})

    if process_id not in processes:
        print(f"Process {process_id} not found.")
        return

    # Upstream: processes this one depends on
    upstream = processes[process_id].get("depends_on", [])

    # Downstream: processes whose depends_on contains this process
    downstream = []
    for pid, pdata in processes.items():
        if pid == process_id:
            continue
        if process_id in pdata.get("depends_on", []):
            downstream.append(pid)

    print(f"\n  {C.BOLD}{C.CYAN}{process_id} Dependencies{C.RESET}\n")
    print(f"  {C.BOLD}Upstream (depends on):{C.RESET}")
    if upstream:
        for dep in upstream:
            print(f"    {C.DIM}-{C.RESET} {dep}")
    else:
        print(f"    {C.DIM}(none){C.RESET}")

    print(f"\n  {C.BOLD}Downstream (depended on by):{C.RESET}")
    if downstream:
        for dep in downstream:
            print(f"    {C.DIM}-{C.RESET} {dep}")
    else:
        print(f"    {C.DIM}(none){C.RESET}")
    print()


def cmd_deps_analyze(args):
    from org_mcp_server import org_analyze_dependencies
    result = org_analyze_dependencies()
    print(result)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="org",
        description="CLI for org-as-code registry",
    )
    sub = parser.add_subparsers(dest="command")

    # Read commands
    sub.add_parser("status", help="All processes + state")
    sub.add_parser("tensions", help="Open tensions")
    sub.add_parser("attractors", help="Strategic goals")
    sub.add_parser("agents", help="Registered agents")
    sub.add_parser("health", help="Health metrics")
    sub.add_parser("verify", help="Verify hash-chain integrity of artifacts.jsonl")

    p_log = sub.add_parser("log", help="Recent artifact log")
    p_log.add_argument("--limit", type=int, default=20, help="Number of entries")

    p_show = sub.add_parser("show", help="Process detail")
    p_show.add_argument("process_id", help="Process ID (e.g. FEAT-001)")

    sub.add_parser("dashboard", help="Combined overview")

    # Write commands
    p_create = sub.add_parser("create", help="Create new process")
    p_create.add_argument("process_id", help="Unique ID (e.g. FEAT-002)")
    p_create.add_argument("template", choices=["feature", "bugfix"], help="Process template")
    p_create.add_argument("title", help="Short title")
    p_create.add_argument("description", help="Detailed description")
    p_create.add_argument("--agent", default="cli-user", help="Agent ID creating this process")
    p_create.add_argument("--priority", type=float, default=0.5, help="Priority 0.0-1.0")
    p_create.add_argument("--repo", help="Related repository")
    p_create.add_argument("--tension", help="Related tension ID")

    p_update = sub.add_parser("update", help="Update process state")
    p_update.add_argument("process_id", help="Process ID")
    p_update.add_argument("state", choices=["P_COMPLETE", "V_COMPLETE", "P_READY", "COMMITTED", "ABANDONED"])
    p_update.add_argument("--assign", help="Assign to agent")
    p_update.add_argument("--notes", help="Notes about change")

    p_art = sub.add_parser("artifact", help="Log an artifact")
    p_art.add_argument("agent", help="Agent ID")
    p_art.add_argument("action", help="Action name (e.g. V.0_review)")
    p_art.add_argument("description", help="What was done")
    p_art.add_argument("--process", help="Related process ID")
    p_art.add_argument("--extra", help="Extra JSON fields")

    p_ta = sub.add_parser("tension-add", help="Register a tension")
    p_ta.add_argument("title", help="Short title")
    p_ta.add_argument("description", help="Description")
    p_ta.add_argument("--priority", type=float, default=0.5, help="Priority 0.0-1.0")
    p_ta.add_argument("--repo", help="Related repository")

    p_tr = sub.add_parser("tension-resolve", help="Resolve a tension")
    p_tr.add_argument("tension_id", help="Tension ID (e.g. T-2026-001)")
    p_tr.add_argument("resolution", nargs="*", help="Resolution text")

    p_pri = sub.add_parser("priority", help="Calculate H(s) priority")
    p_pri.add_argument("--urgency", type=float, default=0.5)
    p_pri.add_argument("--commitment", type=float, default=0.5)
    p_pri.add_argument("--demand", type=float, default=0.5)
    p_pri.add_argument("--blocking", type=float, default=0.5)

    p_energy = sub.add_parser("energy", help="Calculate convergence score E(x)")
    p_energy.add_argument("--gaps", type=float, default=0.0, help="Missing information (0.0-1.0)")
    p_energy.add_argument("--inconsistencies", type=float, default=0.0, help="Contradictions (0.0-1.0)")
    p_energy.add_argument("--uncertainty", type=float, default=0.0, help="Unknowns (0.0-1.0)")
    p_energy.add_argument("--evidence", type=float, default=0.0, help="Supporting evidence (0.0-1.0)")

    p_conv = sub.add_parser("convergence", help="Show convergence history for a process")
    p_conv.add_argument("process_id", help="Process ID (e.g. FEAT-001)")

    p_sync = sub.add_parser("sync", help="Git pull, optionally commit+push")
    p_sync.add_argument("message", nargs="*", help="Commit message (omit for pull-only)")
    p_sync.add_argument("--agent", default="cli-user", help="Agent ID for commit prefix")

    # Dependency commands
    p_da = sub.add_parser("deps-add", help="Add dependency (PROC-A depends on PROC-B)")
    p_da.add_argument("process_id", help="Process that depends on another")
    p_da.add_argument("depends_on_id", help="Process depended upon")

    p_dr = sub.add_parser("deps-remove", help="Remove dependency")
    p_dr.add_argument("process_id", help="Process to remove dependency from")
    p_dr.add_argument("depends_on_id", help="Process no longer depended upon")

    p_deps = sub.add_parser("deps", help="Show dependencies for a process")
    p_deps.add_argument("process_id", help="Process ID")

    sub.add_parser("deps-analyze", help="Full dependency analysis")

    sub.add_parser("help", help="Show this help message")

    return parser


DISPATCH = {
    "status": cmd_status,
    "tensions": cmd_tensions,
    "attractors": cmd_attractors,
    "agents": cmd_agents,
    "health": cmd_health,
    "log": cmd_log,
    "show": cmd_show,
    "verify": cmd_verify,
    "dashboard": cmd_dashboard,
    "create": cmd_create,
    "update": cmd_update,
    "artifact": cmd_artifact,
    "tension-add": cmd_tension_add,
    "tension-resolve": cmd_tension_resolve,
    "priority": cmd_priority,
    "energy": cmd_energy,
    "convergence": cmd_convergence,
    "sync": cmd_sync,
    "deps-add": cmd_deps_add,
    "deps-remove": cmd_deps_remove,
    "deps": cmd_deps,
    "deps-analyze": cmd_deps_analyze,
    "help": lambda args: build_parser().print_help(),
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        # Default: show dashboard
        cmd_dashboard(args)
        return

    handler = DISPATCH.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
