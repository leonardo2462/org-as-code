"""Integration tests for FDM MCP tool pipeline.

Tests the full flow: process creation -> dependency mutation -> fdm.json generation
-> org_read_dependencies reading the register -> audit trail integrity.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

# Set up a temp dir before importing org_mcp_server
_tmpdir = tempfile.mkdtemp(prefix="org_fdm_int_")
os.environ["ORG_REPO_PATH"] = _tmpdir

# Create minimal repo structure
(Path(_tmpdir) / "registry").mkdir()
(Path(_tmpdir) / "processes").mkdir()
(Path(_tmpdir) / "protocol" / "process_templates").mkdir(parents=True)

# Minimal agents.yaml
(Path(_tmpdir) / "registry" / "agents.yaml").write_text(
    yaml.dump({"agents": [
        {"id": "coder", "type": "ai", "skills": ["code"], "status": "active", "capacity": 10},
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
        ],
    })
)

# Empty state and tensions
(Path(_tmpdir) / "registry" / "state.yaml").write_text(yaml.dump({"processes": {}}))
(Path(_tmpdir) / "registry" / "tensions.yaml").write_text(yaml.dump({"tensions": []}))

# Now import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("mcp", reason="mcp package required for org_mcp_server tests")
import org_mcp_server
from org_mcp_server import (
    org_add_dependency,
    org_create_process,
    org_read_dependencies,
    org_remove_dependency,
)


@pytest.fixture(autouse=True)
def fdm_env(tmp_path):
    """Reset environment for each test: fresh tmp_path, patched module paths."""
    # Create structure
    (tmp_path / "registry").mkdir()
    (tmp_path / "processes").mkdir()
    (tmp_path / "protocol" / "process_templates").mkdir(parents=True)

    (tmp_path / "registry" / "agents.yaml").write_text(
        yaml.dump({"agents": [
            {"id": "coder", "type": "ai", "skills": ["code"], "status": "active", "capacity": 10},
        ]})
    )
    (tmp_path / "protocol" / "config.yaml").write_text(
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
    (tmp_path / "protocol" / "process_templates" / "feature.yaml").write_text(
        yaml.dump({
            "name": "feature",
            "steps": [
                {"name": "P.0", "artifact": "P.0_proposal.md", "agent_types": ["ai", "human"]},
            ],
        })
    )
    (tmp_path / "registry" / "state.yaml").write_text(yaml.dump({"processes": {}}))
    (tmp_path / "registry" / "tensions.yaml").write_text(yaml.dump({"tensions": []}))

    # Patch module paths
    org_mcp_server.ORG_REPO = tmp_path
    org_mcp_server.REGISTRY = tmp_path / "registry"
    org_mcp_server.PROCESSES = tmp_path / "processes"
    org_mcp_server.PROTOCOL = tmp_path / "protocol"

    yield tmp_path

    # Restore (not strictly necessary with autouse + tmp_path, but clean)
    org_mcp_server.ORG_REPO = Path(_tmpdir)
    org_mcp_server.REGISTRY = Path(_tmpdir) / "registry"
    org_mcp_server.PROCESSES = Path(_tmpdir) / "processes"
    org_mcp_server.PROTOCOL = Path(_tmpdir) / "protocol"


def _create_process(pid: str, depends_on: str = "") -> str:
    """Helper to create a process via MCP tool."""
    return org_create_process(
        template="feature",
        process_id=pid,
        title=f"Test {pid}",
        description=f"Test process {pid}",
        agent_id="coder",
        depends_on=depends_on,
    )


def test_fdm_json_created_on_add_dependency(fdm_env):
    """fdm.json is auto-created when a dependency is added between processes."""
    tmp_path = fdm_env

    _create_process("FEAT-001")
    _create_process("FEAT-002")

    # fdm.json should not exist yet (no deps)
    fdm_path = tmp_path / "registry" / "fdm.json"
    assert not fdm_path.exists(), "fdm.json should not exist before any dependency"

    result = org_add_dependency("FEAT-002", "FEAT-001")
    assert "Added dependency" in result

    assert fdm_path.exists(), "fdm.json should be auto-created after add_dependency"

    register = json.loads(fdm_path.read_text())

    # Nodes
    assert "FEAT-001" in register["nodes"]
    assert "FEAT-002" in register["nodes"]

    # Edges
    assert ["FEAT-002", "FEAT-001"] in register["edges"]

    # Parallel groups: 2 groups (FEAT-001 first, FEAT-002 second)
    assert len(register["parallel_groups"]) == 2

    # No cycles
    assert register["cycles"] == []

    # Bottleneck is FEAT-001 (has 1 downstream dependent)
    assert register["bottleneck"] == "FEAT-001"


def test_fdm_json_updated_on_remove_dependency(fdm_env):
    """fdm.json is updated when a dependency is removed."""
    tmp_path = fdm_env

    _create_process("FEAT-001")
    _create_process("FEAT-002")
    org_add_dependency("FEAT-002", "FEAT-001")

    result = org_remove_dependency("FEAT-002", "FEAT-001")
    assert "Removed dependency" in result

    fdm_path = tmp_path / "registry" / "fdm.json"
    assert fdm_path.exists()

    register = json.loads(fdm_path.read_text())

    # Edges should be empty after removal
    assert register["edges"] == []

    # All processes in one parallel group (no deps)
    assert len(register["parallel_groups"]) == 1
    group = register["parallel_groups"][0]
    assert "FEAT-001" in group
    assert "FEAT-002" in group


def test_fdm_json_created_on_create_with_deps(fdm_env):
    """fdm.json is auto-created when a process is created with depends_on."""
    tmp_path = fdm_env

    _create_process("FEAT-001")
    _create_process("FEAT-002", depends_on="FEAT-001")

    fdm_path = tmp_path / "registry" / "fdm.json"
    assert fdm_path.exists(), "fdm.json should be created when process has depends_on"

    register = json.loads(fdm_path.read_text())
    assert ["FEAT-002", "FEAT-001"] in register["edges"]


def test_read_dependencies_returns_register(fdm_env):
    """org_read_dependencies returns formatted register content."""
    _create_process("FEAT-001")
    _create_process("FEAT-002")
    org_add_dependency("FEAT-002", "FEAT-001")

    result = org_read_dependencies()

    assert "Nodes" in result
    assert "Edges" in result
    assert "Parallel Groups" in result
    assert "FEAT-001" in result
    assert "FEAT-002" in result


def test_read_dependencies_no_register(fdm_env):
    """org_read_dependencies returns helpful message when no register exists."""
    result = org_read_dependencies()
    assert "No FDM register found" in result


def test_audit_trail_contains_dependency_events(fdm_env):
    """Audit trail contains dependency_add and dependency_remove with hash-chain."""
    tmp_path = fdm_env

    _create_process("FEAT-001")
    _create_process("FEAT-002")
    org_add_dependency("FEAT-002", "FEAT-001")
    org_remove_dependency("FEAT-002", "FEAT-001")

    artifacts_path = tmp_path / "registry" / "artifacts.jsonl"
    assert artifacts_path.exists()

    entries = [json.loads(line) for line in artifacts_path.read_text().strip().split("\n")]

    # Find dependency events
    dep_add = [e for e in entries if e.get("type") == "dependency_add"]
    dep_remove = [e for e in entries if e.get("type") == "dependency_remove"]

    assert len(dep_add) >= 1, "Should have at least one dependency_add event"
    assert len(dep_remove) >= 1, "Should have at least one dependency_remove event"

    # Verify hash-chain fields
    for entry in dep_add + dep_remove:
        assert "prev_hash" in entry, f"Entry missing prev_hash: {entry}"
        assert "entry_hash" in entry, f"Entry missing entry_hash: {entry}"
