"""Tests for per-process state read/write paths in org_mcp_server.py."""

import argparse
import contextlib
import io
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

# Set ORG_REPO_PATH before importing org_mcp_server
_tmpdir = tempfile.mkdtemp(prefix="org_pps_test_")
os.environ["ORG_REPO_PATH"] = _tmpdir

# Create minimal repo structure
(Path(_tmpdir) / "registry").mkdir()
(Path(_tmpdir) / "processes").mkdir()
(Path(_tmpdir) / "protocol" / "process_templates").mkdir(parents=True)

# Minimal agents.yaml
(Path(_tmpdir) / "registry" / "agents.yaml").write_text(
    yaml.dump({"agents": [
        {"id": "coder", "type": "ai", "skills": ["code"], "status": "active", "capacity": 10},
        {"id": "reviewer", "type": "ai", "skills": ["review"], "status": "active", "capacity": 5},
    ]})
)

# Config with per_process mode
(Path(_tmpdir) / "protocol" / "config.yaml").write_text(
    yaml.dump({
        "hamiltonian": {
            "weights": {"urgency": 0.3, "commitment": 0.2, "demand": 0.3, "blocking": 0.2},
            "thresholds": {"action_trigger": 0.5, "escalation": 0.8},
        },
        "security": {"mode": "permissive"},
        "process_engine": {"enforce_transitions": True},
        "state_storage": {"mode": "per_process"},
    })
)

# Minimal feature template
(Path(_tmpdir) / "protocol" / "process_templates" / "feature.yaml").write_text(
    yaml.dump({
        "name": "feature",
        "steps": [
            {"name": "P.0", "artifact": "P.0_proposal.md", "agent_types": ["ai", "human"]},
            {"name": "V.0", "artifact": "V.0_review.yaml", "agent_types": ["ai", "human"]},
        ],
    })
)

# Empty state.yaml and tensions.yaml
(Path(_tmpdir) / "registry" / "state.yaml").write_text(yaml.dump({"processes": {}}))
(Path(_tmpdir) / "registry" / "tensions.yaml").write_text(yaml.dump({"tensions": []}))

# Now import
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("mcp", reason="mcp package required for org_mcp_server tests")
import org_mcp_server
from org_mcp_server import (
    _auto_migrate_if_needed,
    _get_state_storage_mode,
    org_add_dependency,
    org_analyze_dependencies,
    org_create_process,
    org_read_state,
    org_remove_dependency,
    org_update_state,
)

# Import CLI commands
import org_cli
from org_cli import cmd_status, cmd_show, cmd_dashboard

# Patch module-level paths to this test's tmpdir
org_mcp_server.ORG_REPO = Path(_tmpdir)
org_mcp_server.REGISTRY = Path(_tmpdir) / "registry"
org_mcp_server.PROCESSES = Path(_tmpdir) / "processes"
org_mcp_server.PROTOCOL = Path(_tmpdir) / "protocol"
# Also patch org_cli's imported references so CLI commands read from test tmpdir
org_cli.REGISTRY = Path(_tmpdir) / "registry"
org_cli.PROCESSES = Path(_tmpdir) / "processes"


def _config_path():
    return Path(_tmpdir) / "protocol" / "config.yaml"


def _set_storage_mode(mode: str):
    """Helper to switch state_storage.mode in the test config."""
    config = yaml.safe_load(_config_path().read_text())
    config["state_storage"]["mode"] = mode
    _config_path().write_text(yaml.dump(config))


@pytest.fixture(autouse=True)
def _reset_paths():
    """Ensure module paths point to this test's tmpdir before each test."""
    org_mcp_server.ORG_REPO = Path(_tmpdir)
    org_mcp_server.REGISTRY = Path(_tmpdir) / "registry"
    org_mcp_server.PROCESSES = Path(_tmpdir) / "processes"
    org_mcp_server.PROTOCOL = Path(_tmpdir) / "protocol"
    org_cli.REGISTRY = Path(_tmpdir) / "registry"
    org_cli.PROCESSES = Path(_tmpdir) / "processes"
    _set_storage_mode("per_process")
    yield


# ---- Test _get_state_storage_mode ----


def test_get_state_storage_mode_per_process():
    """Config has mode: per_process, helper returns 'per_process'."""
    _set_storage_mode("per_process")
    assert _get_state_storage_mode() == "per_process"


def test_get_state_storage_mode_monolithic():
    """Config has mode: monolithic, helper returns 'monolithic'."""
    _set_storage_mode("monolithic")
    assert _get_state_storage_mode() == "monolithic"


def test_get_state_storage_mode_default():
    """Config has no state_storage key, helper returns 'monolithic'."""
    config = yaml.safe_load(_config_path().read_text())
    del config["state_storage"]
    _config_path().write_text(yaml.dump(config))
    try:
        assert _get_state_storage_mode() == "monolithic"
    finally:
        # Restore
        config["state_storage"] = {"mode": "per_process"}
        _config_path().write_text(yaml.dump(config))


# ---- Test org_create_process with per_process mode ----


_pid_counter = 0


def _unique_pid(prefix="PPS"):
    """Generate a unique process ID for each test."""
    global _pid_counter
    _pid_counter += 1
    return f"{prefix}-{_pid_counter:03d}"


def test_create_process_writes_per_process_state():
    """In per_process mode, org_create_process writes processes/{ID}/state.yaml."""
    _set_storage_mode("per_process")
    pid = _unique_pid()
    result = org_create_process(
        process_id=pid,
        template="feature",
        title="Test Per-Process",
        description="Testing per-process state write",
        agent_id="coder",
        priority=0.7,
    )
    assert f"Created {pid}" in result

    state_file = Path(_tmpdir) / "processes" / pid / "state.yaml"
    assert state_file.exists(), f"Per-process state.yaml not created for {pid}"

    state = yaml.safe_load(state_file.read_text())
    assert state["process_id"] == pid
    assert state["state"] == "P_COMPLETE"
    assert state["assigned_to"] == "coder"
    assert state["priority"] == 0.7
    assert state["template"] == "feature"
    assert "created_at" in state
    assert "last_updated" in state


def test_create_process_monolithic_unchanged():
    """In monolithic mode, state goes to registry/state.yaml, NOT processes/{ID}/state.yaml."""
    _set_storage_mode("monolithic")
    pid = _unique_pid("MON")
    result = org_create_process(
        process_id=pid,
        template="feature",
        title="Test Monolithic",
        description="Testing monolithic mode",
        agent_id="coder",
    )
    assert f"Created {pid}" in result

    # Should be in registry/state.yaml
    state_data = yaml.safe_load((Path(_tmpdir) / "registry" / "state.yaml").read_text())
    assert pid in state_data.get("processes", {}), f"{pid} not in registry/state.yaml"

    # Should NOT have per-process state.yaml
    per_process_state = Path(_tmpdir) / "processes" / pid / "state.yaml"
    assert not per_process_state.exists(), f"Per-process state.yaml should not exist in monolithic mode"

    # Restore for other tests
    _set_storage_mode("per_process")


# ---- Test org_update_state with per_process mode ----


def test_update_state_per_process():
    """In per_process mode, org_update_state reads/writes processes/{ID}/state.yaml."""
    _set_storage_mode("per_process")
    pid = _unique_pid()

    # Create process first
    org_create_process(
        process_id=pid,
        template="feature",
        title="Update Test",
        description="Testing state update",
        agent_id="coder",
    )

    # Log a V-step so we can transition to COMMITTED if needed
    from org_mcp_server import org_log_artifact
    org_log_artifact(
        agent_id="reviewer",
        action="V.0_review",
        description="Approved",
        process_id=pid,
    )

    # Update state
    result = org_update_state(
        process_id=pid,
        state="V_COMPLETE",
        assigned_to="reviewer",
        notes="Review done",
    )
    assert f"Updated {pid}" in result

    # Verify the per-process state.yaml was updated
    state_file = Path(_tmpdir) / "processes" / pid / "state.yaml"
    state = yaml.safe_load(state_file.read_text())
    assert state["state"] == "V_COMPLETE"
    assert state["assigned_to"] == "reviewer"
    assert state["notes"] == "Review done"


def test_update_state_missing_process():
    """org_update_state returns error for non-existent process in per_process mode."""
    _set_storage_mode("per_process")
    result = org_update_state(
        process_id="GHOST-999",
        state="P_COMPLETE",
    )
    assert "not found" in result.lower()


def test_two_processes_independent_files():
    """Two processes in per_process mode have independent state files."""
    _set_storage_mode("per_process")
    pid_a = _unique_pid("IND")
    pid_b = _unique_pid("IND")

    # Create both
    org_create_process(process_id=pid_a, template="feature", title="A", description="A", agent_id="coder")
    org_create_process(process_id=pid_b, template="feature", title="B", description="B", agent_id="coder")

    # Update only A
    org_update_state(process_id=pid_a, state="V_COMPLETE")

    # Read both state files
    state_a = yaml.safe_load((Path(_tmpdir) / "processes" / pid_a / "state.yaml").read_text())
    state_b = yaml.safe_load((Path(_tmpdir) / "processes" / pid_b / "state.yaml").read_text())

    assert state_a["state"] == "V_COMPLETE", "Process A should be updated"
    assert state_b["state"] == "P_COMPLETE", "Process B should be unchanged"


# ---- Test state aggregation and index regeneration ----


def test_read_state_aggregates_per_process_files():
    """In per_process mode, org_read_state aggregates from individual state files."""
    _set_storage_mode("per_process")
    pid_a = _unique_pid("AGG")
    pid_b = _unique_pid("AGG")

    org_create_process(process_id=pid_a, template="feature", title="Agg A", description="A", agent_id="coder")
    org_create_process(process_id=pid_b, template="feature", title="Agg B", description="B", agent_id="coder")

    result = org_read_state()
    assert pid_a in result, f"Expected {pid_a} in aggregated read"
    assert pid_b in result, f"Expected {pid_b} in aggregated read"


def test_read_state_empty_per_process():
    """In per_process mode with no processes, org_read_state returns empty message."""
    _set_storage_mode("per_process")
    # Clear all process dirs to simulate empty state
    import shutil as _shutil
    procs_dir = Path(_tmpdir) / "processes"
    for child in list(procs_dir.iterdir()):
        _shutil.rmtree(child)

    result = org_read_state()
    assert result == "No active processes."


def test_create_process_regenerates_index():
    """In per_process mode, org_create_process regenerates registry/state.yaml."""
    _set_storage_mode("per_process")
    pid = _unique_pid("IDX")
    org_create_process(process_id=pid, template="feature", title="Index Test", description="X", agent_id="coder")

    index = yaml.safe_load((Path(_tmpdir) / "registry" / "state.yaml").read_text())
    assert pid in index.get("processes", {}), f"{pid} not in regenerated state index"


def test_update_state_regenerates_index():
    """In per_process mode, org_update_state regenerates registry/state.yaml."""
    _set_storage_mode("per_process")
    pid = _unique_pid("UPD")
    org_create_process(process_id=pid, template="feature", title="Update Idx", description="X", agent_id="coder")

    org_update_state(process_id=pid, state="V_COMPLETE")

    index = yaml.safe_load((Path(_tmpdir) / "registry" / "state.yaml").read_text())
    assert index["processes"][pid]["state"] == "V_COMPLETE"


def test_index_matches_per_process_data():
    """In per_process mode, registry/state.yaml mirrors all per-process state files."""
    _set_storage_mode("per_process")
    # Clear processes first for a clean test
    import shutil as _shutil
    procs_dir = Path(_tmpdir) / "processes"
    for child in list(procs_dir.iterdir()):
        _shutil.rmtree(child)
    # Reset monolithic state to prevent auto-migration of stale data
    (Path(_tmpdir) / "registry" / "state.yaml").write_text(yaml.dump({"processes": {}}))

    pid_a = _unique_pid("MIR")
    pid_b = _unique_pid("MIR")
    org_create_process(process_id=pid_a, template="feature", title="Mirror A", description="A", agent_id="coder")
    org_create_process(process_id=pid_b, template="feature", title="Mirror B", description="B", agent_id="coder")

    # Update one
    org_update_state(process_id=pid_a, state="V_COMPLETE")

    index = yaml.safe_load((Path(_tmpdir) / "registry" / "state.yaml").read_text())
    procs = index.get("processes", {})
    assert len(procs) == 2, f"Expected 2 processes in index, got {len(procs)}"
    assert procs[pid_a]["state"] == "V_COMPLETE"
    assert procs[pid_b]["state"] == "P_COMPLETE"


def test_read_state_monolithic_unchanged():
    """In monolithic mode, org_read_state reads from registry/state.yaml directly."""
    _set_storage_mode("monolithic")
    pid = _unique_pid("MONO")

    # Manually write a process entry to registry/state.yaml
    state_path = Path(_tmpdir) / "registry" / "state.yaml"
    state_data = yaml.safe_load(state_path.read_text()) or {}
    if "processes" not in state_data:
        state_data["processes"] = {}
    state_data["processes"][pid] = {
        "state": "P_COMPLETE",
        "assigned_to": "coder",
        "priority": 0.5,
    }
    state_path.write_text(yaml.dump(state_data))

    result = org_read_state()
    assert pid in result, f"Expected {pid} in monolithic read"

    # Restore mode
    _set_storage_mode("per_process")


# ---- Auto-migration tests ----


def test_auto_migrate_monolithic_to_per_process():
    """Auto-migration creates per-process files from monolithic state.yaml."""
    _set_storage_mode("per_process")
    # Clear all process dirs
    procs_dir = Path(_tmpdir) / "processes"
    for child in list(procs_dir.iterdir()):
        shutil.rmtree(child)

    # Write monolithic state with 2 processes
    state_path = Path(_tmpdir) / "registry" / "state.yaml"
    state_path.write_text(yaml.dump({"processes": {
        "MIG-001": {"state": "P_COMPLETE", "assigned_to": "coder", "priority": 0.7, "template": "feature"},
        "MIG-002": {"state": "V_COMPLETE", "assigned_to": "reviewer", "priority": 0.5, "template": "feature"},
    }}))

    _auto_migrate_if_needed()

    # Verify MIG-001
    s1 = yaml.safe_load((procs_dir / "MIG-001" / "state.yaml").read_text())
    assert s1["process_id"] == "MIG-001"
    assert s1["state"] == "P_COMPLETE"
    assert s1["assigned_to"] == "coder"
    assert s1["priority"] == 0.7
    assert s1["template"] == "feature"

    # Verify MIG-002
    s2 = yaml.safe_load((procs_dir / "MIG-002" / "state.yaml").read_text())
    assert s2["process_id"] == "MIG-002"
    assert s2["state"] == "V_COMPLETE"
    assert s2["assigned_to"] == "reviewer"
    assert s2["priority"] == 0.5
    assert s2["template"] == "feature"


def test_auto_migrate_skips_when_per_process_files_exist():
    """Auto-migration skips when per-process state files already exist."""
    _set_storage_mode("per_process")
    # Clear processes
    procs_dir = Path(_tmpdir) / "processes"
    for child in list(procs_dir.iterdir()):
        shutil.rmtree(child)

    # Create a process via normal path (creates per-process file)
    pid = _unique_pid("SKIP")
    org_create_process(process_id=pid, template="feature", title="Existing", description="X", agent_id="coder")

    # Manually add another process to monolithic state that does NOT have a per-process file
    state_path = Path(_tmpdir) / "registry" / "state.yaml"
    state_data = yaml.safe_load(state_path.read_text())
    state_data["processes"]["SKIP-EXTRA"] = {"state": "P_COMPLETE", "assigned_to": "coder"}
    state_path.write_text(yaml.dump(state_data))

    _auto_migrate_if_needed()

    # SKIP-EXTRA should NOT have been migrated (existing per-process files detected)
    assert not (procs_dir / "SKIP-EXTRA" / "state.yaml").exists(), \
        "SKIP-EXTRA should not be migrated when per-process files already exist"


def test_auto_migrate_noop_in_monolithic_mode():
    """Auto-migration does nothing in monolithic mode."""
    _set_storage_mode("monolithic")
    # Clear processes
    procs_dir = Path(_tmpdir) / "processes"
    for child in list(procs_dir.iterdir()):
        shutil.rmtree(child)

    # Write process data to monolithic state
    state_path = Path(_tmpdir) / "registry" / "state.yaml"
    state_path.write_text(yaml.dump({"processes": {
        "MONO-NOP": {"state": "P_COMPLETE", "assigned_to": "coder"},
    }}))

    _auto_migrate_if_needed()

    # No per-process file should exist
    assert not (procs_dir / "MONO-NOP" / "state.yaml").exists(), \
        "No per-process files should be created in monolithic mode"

    # Restore
    _set_storage_mode("per_process")


def test_auto_migrate_triggered_by_create_process():
    """org_create_process triggers auto-migration before creating new process."""
    _set_storage_mode("per_process")
    # Clear processes
    procs_dir = Path(_tmpdir) / "processes"
    for child in list(procs_dir.iterdir()):
        shutil.rmtree(child)

    # Write monolithic state with 1 process (valid process ID format)
    state_path = Path(_tmpdir) / "registry" / "state.yaml"
    state_path.write_text(yaml.dump({"processes": {
        "MIGC-001": {"state": "P_COMPLETE", "assigned_to": "coder", "priority": 0.6, "template": "feature"},
    }}))

    # Create a NEW process (should trigger migration first)
    pid_new = _unique_pid("MIGC")
    org_create_process(process_id=pid_new, template="feature", title="New After Migrate",
                       description="X", agent_id="coder")

    # Both should have per-process files
    assert (procs_dir / "MIGC-001" / "state.yaml").exists(), \
        "MIGC-001 should be migrated by auto-migration"
    assert (procs_dir / pid_new / "state.yaml").exists(), \
        f"{pid_new} should be created by org_create_process"

    # Verify migrated data integrity
    s = yaml.safe_load((procs_dir / "MIGC-001" / "state.yaml").read_text())
    assert s["state"] == "P_COMPLETE"
    assert s["process_id"] == "MIGC-001"


def test_auto_migrate_triggered_by_update_state():
    """org_update_state triggers auto-migration before updating."""
    _set_storage_mode("per_process")
    # Clear processes
    procs_dir = Path(_tmpdir) / "processes"
    for child in list(procs_dir.iterdir()):
        shutil.rmtree(child)

    # Write monolithic state with 1 process (valid process ID format)
    state_path = Path(_tmpdir) / "registry" / "state.yaml"
    state_path.write_text(yaml.dump({"processes": {
        "MIGU-001": {"state": "P_COMPLETE", "assigned_to": "coder", "priority": 0.5, "template": "feature"},
    }}))

    # Update the process (triggers migration first, then update)
    result = org_update_state(process_id="MIGU-001", state="V_COMPLETE", assigned_to="reviewer")
    assert "Updated MIGU-001" in result

    # Verify the per-process file has the UPDATED state
    s = yaml.safe_load((procs_dir / "MIGU-001" / "state.yaml").read_text())
    assert s["state"] == "V_COMPLETE"
    assert s["assigned_to"] == "reviewer"
    assert s["process_id"] == "MIGU-001"


# ---- CLI transparency tests (STATE-07) ----


def _clear_processes():
    """Remove all process directories for a clean test."""
    procs_dir = Path(_tmpdir) / "processes"
    for child in list(procs_dir.iterdir()):
        shutil.rmtree(child)
    # Reset index to match
    (Path(_tmpdir) / "registry" / "state.yaml").write_text(yaml.dump({"processes": {}}))


def test_cli_status_works_with_per_process():
    """cmd_status shows processes created in per_process mode (STATE-07)."""
    _set_storage_mode("per_process")
    _clear_processes()

    pid_a = _unique_pid("CLI")
    pid_b = _unique_pid("CLI")
    org_create_process(process_id=pid_a, template="feature", title="CLI A", description="A", agent_id="coder", priority=0.8)
    org_create_process(process_id=pid_b, template="feature", title="CLI B", description="B", agent_id="coder", priority=0.6)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_status(argparse.Namespace())

    output = buf.getvalue()
    assert pid_a in output, f"Expected {pid_a} in status output"
    assert pid_b in output, f"Expected {pid_b} in status output"
    assert "P_COMPLETE" in output, "Expected P_COMPLETE state in status output"


def test_cli_show_works_with_per_process():
    """cmd_show displays process detail in per_process mode (STATE-07)."""
    _set_storage_mode("per_process")
    _clear_processes()

    pid = _unique_pid("CLI")
    org_create_process(process_id=pid, template="feature", title="CLI Show", description="Show test", agent_id="coder", priority=0.7)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_show(argparse.Namespace(process_id=pid))

    output = buf.getvalue()
    assert pid in output, f"Expected {pid} in show output"
    assert "P_COMPLETE" in output, "Expected P_COMPLETE state in show output"


def test_cli_dashboard_works_with_per_process():
    """cmd_dashboard shows PROCESSES section with per_process data (STATE-07)."""
    _set_storage_mode("per_process")
    _clear_processes()

    pid = _unique_pid("CLI")
    org_create_process(process_id=pid, template="feature", title="CLI Dash", description="Dash test", agent_id="coder")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_dashboard(argparse.Namespace())

    output = buf.getvalue()
    assert "PROCESSES" in output, "Expected PROCESSES section header in dashboard"
    assert pid in output, f"Expected {pid} in dashboard output"


def test_cli_status_reflects_state_update():
    """cmd_status reflects state changes made via org_update_state in per_process mode (STATE-07)."""
    _set_storage_mode("per_process")
    _clear_processes()

    pid = _unique_pid("CLI")
    org_create_process(process_id=pid, template="feature", title="CLI Update", description="Update test", agent_id="coder")
    org_update_state(process_id=pid, state="V_COMPLETE")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_status(argparse.Namespace())

    output = buf.getvalue()
    assert "V_COMPLETE" in output, "Expected V_COMPLETE state after update in status output"


# ---- Concurrent write safety tests (TEST-06) ----


def test_concurrent_create_no_data_loss():
    """Sequential creates of 5 processes lose no data in per_process mode (TEST-06)."""
    _set_storage_mode("per_process")
    _clear_processes()

    pids = [_unique_pid("CON") for _ in range(5)]
    for i, pid in enumerate(pids):
        org_create_process(
            process_id=pid, template="feature",
            title=f"Concurrent {i}", description=f"Proc {i}",
            agent_id="coder", priority=round(0.5 + i * 0.1, 1),
        )

    # Verify all 5 per-process state files exist with correct data
    for i, pid in enumerate(pids):
        state_file = Path(_tmpdir) / "processes" / pid / "state.yaml"
        assert state_file.exists(), f"Per-process file missing for {pid}"
        state = yaml.safe_load(state_file.read_text())
        assert state["process_id"] == pid
        assert state["state"] == "P_COMPLETE"
        assert state["priority"] == round(0.5 + i * 0.1, 1)

    # Verify the index contains all 5
    index = yaml.safe_load((Path(_tmpdir) / "registry" / "state.yaml").read_text())
    procs = index.get("processes", {})
    for pid in pids:
        assert pid in procs, f"{pid} missing from state index"
        assert procs[pid]["state"] == "P_COMPLETE"


def test_concurrent_update_no_interference():
    """Updates to different processes do not interfere in per_process mode (TEST-06)."""
    _set_storage_mode("per_process")
    _clear_processes()

    pid_a = _unique_pid("CON")
    pid_b = _unique_pid("CON")
    pid_c = _unique_pid("CON")

    org_create_process(process_id=pid_a, template="feature", title="A", description="A", agent_id="coder")
    org_create_process(process_id=pid_b, template="feature", title="B", description="B", agent_id="coder")
    org_create_process(process_id=pid_c, template="feature", title="C", description="C", agent_id="coder")

    # Update A and B, leave C unchanged
    org_update_state(process_id=pid_a, state="V_COMPLETE")
    org_update_state(process_id=pid_b, state="V_COMPLETE")

    # Verify per-process files
    sa = yaml.safe_load((Path(_tmpdir) / "processes" / pid_a / "state.yaml").read_text())
    sb = yaml.safe_load((Path(_tmpdir) / "processes" / pid_b / "state.yaml").read_text())
    sc = yaml.safe_load((Path(_tmpdir) / "processes" / pid_c / "state.yaml").read_text())
    assert sa["state"] == "V_COMPLETE", "A should be V_COMPLETE"
    assert sb["state"] == "V_COMPLETE", "B should be V_COMPLETE"
    assert sc["state"] == "P_COMPLETE", "C should remain P_COMPLETE"

    # Verify index matches
    index = yaml.safe_load((Path(_tmpdir) / "registry" / "state.yaml").read_text())
    procs = index.get("processes", {})
    assert procs[pid_a]["state"] == "V_COMPLETE"
    assert procs[pid_b]["state"] == "V_COMPLETE"
    assert procs[pid_c]["state"] == "P_COMPLETE"


def test_interleaved_create_and_update_no_data_loss():
    """Interleaved create/update operations preserve all data in per_process mode (TEST-06)."""
    _set_storage_mode("per_process")
    _clear_processes()

    pid_a = _unique_pid("CON")
    pid_b = _unique_pid("CON")
    pid_c = _unique_pid("CON")

    # Interleaved: create A, create B, update A, create C, update B
    org_create_process(process_id=pid_a, template="feature", title="A", description="A", agent_id="coder")
    org_create_process(process_id=pid_b, template="feature", title="B", description="B", agent_id="coder")
    org_update_state(process_id=pid_a, state="V_COMPLETE")
    org_create_process(process_id=pid_c, template="feature", title="C", description="C", agent_id="coder")
    org_update_state(process_id=pid_b, state="V_COMPLETE")

    # Verify per-process files
    sa = yaml.safe_load((Path(_tmpdir) / "processes" / pid_a / "state.yaml").read_text())
    sb = yaml.safe_load((Path(_tmpdir) / "processes" / pid_b / "state.yaml").read_text())
    sc = yaml.safe_load((Path(_tmpdir) / "processes" / pid_c / "state.yaml").read_text())
    assert sa["state"] == "V_COMPLETE", "A should be V_COMPLETE after interleaved ops"
    assert sb["state"] == "V_COMPLETE", "B should be V_COMPLETE after interleaved ops"
    assert sc["state"] == "P_COMPLETE", "C should remain P_COMPLETE"

    # Verify index matches
    index = yaml.safe_load((Path(_tmpdir) / "registry" / "state.yaml").read_text())
    procs = index.get("processes", {})
    assert len(procs) == 3, f"Expected 3 processes in index, got {len(procs)}"
    assert procs[pid_a]["state"] == "V_COMPLETE"
    assert procs[pid_b]["state"] == "V_COMPLETE"
    assert procs[pid_c]["state"] == "P_COMPLETE"


# ---- FDM dependency tests (FDM-01, FDM-02, FDM-05) ----


def test_create_process_with_depends_on():
    """Create process with depends_on stores dependency list in state.yaml."""
    _set_storage_mode("per_process")
    _clear_processes()

    # Create dependency target first
    org_create_process(process_id="FEAT-001", template="feature", title="Dep Target",
                       description="Target", agent_id="coder")
    # Create process that depends on it
    result = org_create_process(process_id="FEAT-002", template="feature", title="Dep Source",
                                description="Source", agent_id="coder", depends_on="FEAT-001")
    assert "Created FEAT-002" in result

    # Verify state.yaml has depends_on
    state = yaml.safe_load((Path(_tmpdir) / "processes" / "FEAT-002" / "state.yaml").read_text())
    assert state["depends_on"] == ["FEAT-001"]

    # Verify aggregated state includes depends_on
    agg = org_read_state()
    assert "FEAT-002" in agg
    assert "FEAT-001" in agg  # depends_on should appear in output


def test_create_process_with_multiple_depends_on():
    """Create process with multiple dependencies stores all in state.yaml."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-010", template="feature", title="A", description="A", agent_id="coder")
    org_create_process(process_id="FEAT-011", template="feature", title="B", description="B", agent_id="coder")
    org_create_process(process_id="FEAT-012", template="feature", title="C", description="C",
                       agent_id="coder", depends_on="FEAT-010,FEAT-011")

    state = yaml.safe_load((Path(_tmpdir) / "processes" / "FEAT-012" / "state.yaml").read_text())
    assert state["depends_on"] == ["FEAT-010", "FEAT-011"]


def test_depends_on_self_reference_rejected():
    """Self-referencing dependency is rejected."""
    _set_storage_mode("per_process")
    _clear_processes()

    result = org_create_process(process_id="FEAT-020", template="feature", title="Self Ref",
                                description="Self", agent_id="coder", depends_on="FEAT-020")
    assert "cannot depend on itself" in result


def test_depends_on_nonexistent_rejected():
    """Dependency on non-existent process is rejected."""
    _set_storage_mode("per_process")
    _clear_processes()

    result = org_create_process(process_id="FEAT-030", template="feature", title="Ghost Dep",
                                description="Ghost", agent_id="coder", depends_on="GHOST-999")
    assert "does not exist" in result


def test_depends_on_invalid_format_rejected():
    """Dependency with invalid format is rejected."""
    _set_storage_mode("per_process")
    _clear_processes()

    result = org_create_process(process_id="FEAT-040", template="feature", title="Bad Format",
                                description="Bad", agent_id="coder", depends_on="not-valid-id")
    assert "must match" in result or "Invalid process_id" in result


def test_create_process_without_depends_on_unchanged():
    """Process created without depends_on has no depends_on key in state (backward compat)."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-050", template="feature", title="No Deps",
                       description="None", agent_id="coder")

    state = yaml.safe_load((Path(_tmpdir) / "processes" / "FEAT-050" / "state.yaml").read_text())
    assert "depends_on" not in state, "depends_on should not be present when not provided"


def test_validate_dependencies_direct():
    """Direct unit tests for _validate_dependencies helper."""
    from org_mcp_server import _validate_dependencies
    _set_storage_mode("per_process")
    _clear_processes()

    # Self-reference check
    ok, msg = _validate_dependencies("X-1", ["X-1"])
    assert not ok
    assert "cannot depend on itself" in msg

    # Invalid format check
    ok, msg = _validate_dependencies("X-1", ["bad"])
    assert not ok
    assert "must match" in msg or "Invalid process_id" in msg

    # Existence check with valid dependency
    org_create_process(process_id="Y-1", template="feature", title="Dep", description="Dep", agent_id="coder")
    ok, msg = _validate_dependencies("X-1", ["Y-1"])
    assert ok
    assert msg == ""


# ---- Dependency mutation tests (FDM-03, FDM-04) ----


def test_add_dependency():
    """org_add_dependency adds a dependency edge between two existing processes."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="Target",
                       description="Target", agent_id="coder")
    org_create_process(process_id="FEAT-002", template="feature", title="Source",
                       description="Source", agent_id="coder")

    result = org_add_dependency("FEAT-002", "FEAT-001")
    assert "Added dependency" in result
    assert "FEAT-002 depends on FEAT-001" in result

    state = yaml.safe_load((Path(_tmpdir) / "processes" / "FEAT-002" / "state.yaml").read_text())
    assert state["depends_on"] == ["FEAT-001"]


def test_add_dependency_duplicate():
    """Duplicate add returns informational message, not error."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="Target",
                       description="Target", agent_id="coder")
    org_create_process(process_id="FEAT-002", template="feature", title="Source",
                       description="Source", agent_id="coder", depends_on="FEAT-001")

    result = org_add_dependency("FEAT-002", "FEAT-001")
    assert "already exists" in result

    # Verify list unchanged (no duplicate entry)
    state = yaml.safe_load((Path(_tmpdir) / "processes" / "FEAT-002" / "state.yaml").read_text())
    assert state["depends_on"] == ["FEAT-001"]


def test_add_dependency_self_reference_rejected():
    """Self-referencing dependency is rejected."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="Self",
                       description="Self", agent_id="coder")

    result = org_add_dependency("FEAT-001", "FEAT-001")
    assert "cannot depend on itself" in result


def test_add_dependency_nonexistent_target_rejected():
    """Adding dependency on non-existent target is rejected."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="Source",
                       description="Source", agent_id="coder")

    result = org_add_dependency("FEAT-001", "FEAT-999")
    assert "does not exist" in result


def test_add_dependency_nonexistent_source_rejected():
    """Adding dependency for non-existent source returns not found."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="Target",
                       description="Target", agent_id="coder")

    result = org_add_dependency("FEAT-999", "FEAT-001")
    assert "not found" in result.lower()


def test_remove_dependency():
    """org_remove_dependency removes a dependency edge and cleans up empty list."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="Target",
                       description="Target", agent_id="coder")
    org_create_process(process_id="FEAT-002", template="feature", title="Source",
                       description="Source", agent_id="coder", depends_on="FEAT-001")

    result = org_remove_dependency("FEAT-002", "FEAT-001")
    assert "Removed dependency" in result
    assert "no longer depends on" in result

    state = yaml.safe_load((Path(_tmpdir) / "processes" / "FEAT-002" / "state.yaml").read_text())
    assert "depends_on" not in state, "depends_on key should be removed when list is empty"


def test_remove_dependency_not_found():
    """Removing non-existent dependency returns informational message."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="A",
                       description="A", agent_id="coder")
    org_create_process(process_id="FEAT-002", template="feature", title="B",
                       description="B", agent_id="coder")

    result = org_remove_dependency("FEAT-002", "FEAT-001")
    assert "not found" in result.lower()


def test_add_then_remove_roundtrip():
    """Full lifecycle: add dependency, verify, remove, verify gone."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="Target",
                       description="Target", agent_id="coder")
    org_create_process(process_id="FEAT-002", template="feature", title="Source",
                       description="Source", agent_id="coder")

    # Add
    result = org_add_dependency("FEAT-002", "FEAT-001")
    assert "Added dependency" in result
    state = yaml.safe_load((Path(_tmpdir) / "processes" / "FEAT-002" / "state.yaml").read_text())
    assert state["depends_on"] == ["FEAT-001"]

    # Remove
    result = org_remove_dependency("FEAT-002", "FEAT-001")
    assert "Removed dependency" in result
    state = yaml.safe_load((Path(_tmpdir) / "processes" / "FEAT-002" / "state.yaml").read_text())
    assert "depends_on" not in state


def test_add_dependency_audit_trail():
    """org_add_dependency logs to audit trail with type dependency_add."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="Target",
                       description="Target", agent_id="coder")
    org_create_process(process_id="FEAT-002", template="feature", title="Source",
                       description="Source", agent_id="coder")

    org_add_dependency("FEAT-002", "FEAT-001")

    # Read audit log
    import json
    jsonl_path = Path(_tmpdir) / "registry" / "artifacts.jsonl"
    entries = [json.loads(line) for line in jsonl_path.read_text().strip().split("\n") if line.strip()]
    last = entries[-1]
    assert last["type"] == "dependency_add"
    assert last["process_id"] == "FEAT-002"
    assert last["depends_on"] == "FEAT-001"


# ---- org_analyze_dependencies tests ----


def test_analyze_dependencies_no_processes():
    """Empty repo returns 'No processes found' without errors."""
    _set_storage_mode("per_process")
    _clear_processes()

    result = org_analyze_dependencies()
    assert "No processes found" in result


def test_analyze_dependencies_no_deps():
    """Processes without dependencies all appear in group 1, no cycles, no bottleneck."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="A",
                       description="A", agent_id="coder")
    org_create_process(process_id="FEAT-002", template="feature", title="B",
                       description="B", agent_id="coder")

    result = org_analyze_dependencies()
    assert "Group 1 (can start immediately): FEAT-001, FEAT-002" in result
    assert "No circular dependencies detected" in result
    assert "No bottleneck identified" in result


def test_analyze_dependencies_linear_chain():
    """Linear chain FEAT-003 <- FEAT-002 <- FEAT-001 produces 3 parallel groups."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="A",
                       description="A", agent_id="coder")
    org_create_process(process_id="FEAT-002", template="feature", title="B",
                       description="B", agent_id="coder")
    org_create_process(process_id="FEAT-003", template="feature", title="C",
                       description="C", agent_id="coder")

    # FEAT-001 depends on FEAT-002, FEAT-002 depends on FEAT-003
    org_add_dependency("FEAT-001", "FEAT-002")
    org_add_dependency("FEAT-002", "FEAT-003")

    result = org_analyze_dependencies()

    # Group 1 should have FEAT-003 (no deps)
    assert "Group 1" in result
    assert "FEAT-003" in result
    # Group 2 should have FEAT-002
    assert "Group 2" in result
    assert "FEAT-002" in result
    # Group 3 should have FEAT-001
    assert "Group 3" in result
    assert "FEAT-001" in result
    # No cycles
    assert "No circular dependencies detected" in result
    # Bottleneck is FEAT-003 (most downstream dependents)
    assert "FEAT-003" in result
    assert "downstream dependents" in result
    # Summary
    assert "Cycles: 0" in result


def test_analyze_dependencies_with_cycle():
    """Circular dependency produces cycle output with resolution proposal."""
    _set_storage_mode("per_process")
    _clear_processes()

    org_create_process(process_id="FEAT-001", template="feature", title="A",
                       description="A", agent_id="coder")
    org_create_process(process_id="FEAT-002", template="feature", title="B",
                       description="B", agent_id="coder")
    org_create_process(process_id="FEAT-003", template="feature", title="C",
                       description="C", agent_id="coder")

    # Create cycle: FEAT-001 -> FEAT-002 -> FEAT-003 -> FEAT-001
    org_add_dependency("FEAT-001", "FEAT-002")
    org_add_dependency("FEAT-002", "FEAT-003")
    org_add_dependency("FEAT-003", "FEAT-001")

    result = org_analyze_dependencies()

    assert "Cycle:" in result
    assert "->" in result
    assert "Suggested resolution" in result
    assert "most recently added" in result


# ---- Cleanup ----


def teardown_module():
    shutil.rmtree(_tmpdir, ignore_errors=True)
