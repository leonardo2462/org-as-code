"""Tests for CLI dependency commands (deps-add, deps-remove, deps, deps-analyze, dashboard deps)."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Set up isolated tmpdir BEFORE importing org_mcp_server
# ---------------------------------------------------------------------------

_tmpdir = tempfile.mkdtemp(prefix="org_cli_deps_test_")
os.environ["ORG_REPO_PATH"] = _tmpdir

# Minimal repo structure
(Path(_tmpdir) / "registry").mkdir()
(Path(_tmpdir) / "processes").mkdir()
(Path(_tmpdir) / "protocol" / "process_templates").mkdir(parents=True)

# agents.yaml
(Path(_tmpdir) / "registry" / "agents.yaml").write_text(
    yaml.dump({"agents": [
        {"id": "alice", "type": "human", "name": "Alice", "skills": ["review"], "status": "active", "capacity": 5},
        {"id": "coder", "type": "ai", "name": "Coder Bot", "skills": ["code"], "status": "active", "capacity": 10},
    ]})
)

# config.yaml with per_process state storage
(Path(_tmpdir) / "protocol" / "config.yaml").write_text(
    yaml.dump({
        "hamiltonian": {
            "weights": {"urgency": 0.3, "commitment": 0.2, "demand": 0.3, "blocking": 0.2},
            "thresholds": {"action_trigger": 0.5, "escalation": 0.8},
        },
        "security": {"mode": "permissive"},
        "process_engine": {"enforce_transitions": True},
        "energy": {
            "weights": {"gap": 0.4, "inconsistency": 0.3, "uncertainty": 0.2, "evidence": 0.1},
            "thresholds": {"high_energy": 0.7, "low_energy": 0.3},
        },
        "state_storage": {"mode": "per_process"},
    })
)

# feature template
(Path(_tmpdir) / "protocol" / "process_templates" / "feature.yaml").write_text(
    yaml.dump({
        "name": "feature",
        "steps": [
            {"name": "P.0", "artifact": "P.0_proposal.md", "agent_types": ["ai", "human"]},
            {"name": "V.0", "artifact": "V.0_review.yaml", "agent_types": ["ai", "human"]},
        ],
    })
)

# bugfix template
(Path(_tmpdir) / "protocol" / "process_templates" / "bugfix.yaml").write_text(
    yaml.dump({
        "name": "bugfix",
        "steps": [
            {"name": "P.0_diagnosis", "artifact": "P.0_diagnosis.md", "agent_types": ["ai", "human"]},
            {"name": "V.0_review", "artifact": "V.0_review.yaml", "agent_types": ["ai", "human"]},
        ],
    })
)

# state.yaml (monolithic index)
(Path(_tmpdir) / "registry" / "state.yaml").write_text(
    yaml.dump({"processes": {
        "FEAT-001": {"state": "P_COMPLETE", "assigned_to": "coder", "priority": 0.7, "template": "feature"},
        "FEAT-002": {"state": "P_COMPLETE", "assigned_to": "alice", "priority": 0.5, "template": "feature"},
    }})
)

# tensions.yaml
(Path(_tmpdir) / "registry" / "tensions.yaml").write_text(
    yaml.dump({"tensions": []})
)

# attractors.yaml
(Path(_tmpdir) / "registry" / "attractors.yaml").write_text(
    yaml.dump({"attractors": []})
)

# Create FEAT-001 process directory with artifact and per-process state
(Path(_tmpdir) / "processes" / "FEAT-001").mkdir()
(Path(_tmpdir) / "processes" / "FEAT-001" / "P.0_proposal.md").write_text("# FEAT-001: Test\n\nDescription here.")
(Path(_tmpdir) / "processes" / "FEAT-001" / "state.yaml").write_text(
    yaml.dump({"process_id": "FEAT-001", "state": "P_COMPLETE", "assigned_to": "coder", "priority": 0.7, "template": "feature"})
)

# Create FEAT-002 process directory with per-process state
(Path(_tmpdir) / "processes" / "FEAT-002").mkdir()
(Path(_tmpdir) / "processes" / "FEAT-002" / "P.0_proposal.md").write_text("# FEAT-002: Second\n\nAnother feature.")
(Path(_tmpdir) / "processes" / "FEAT-002" / "state.yaml").write_text(
    yaml.dump({"process_id": "FEAT-002", "state": "P_COMPLETE", "assigned_to": "alice", "priority": 0.5, "template": "feature"})
)

# Now import
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("mcp", reason="mcp package required for org_mcp_server tests")
import org_mcp_server
from org_cli import (
    build_parser,
    cmd_dashboard,
    cmd_deps,
    cmd_deps_add,
    cmd_deps_analyze,
    cmd_deps_remove,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_paths():
    """Ensure module paths point to test tmpdir and clean state between tests."""
    org_mcp_server.ORG_REPO = Path(_tmpdir)
    org_mcp_server.REGISTRY = Path(_tmpdir) / "registry"
    org_mcp_server.PROCESSES = Path(_tmpdir) / "processes"
    org_mcp_server.PROTOCOL = Path(_tmpdir) / "protocol"

    import org_cli
    org_cli.REGISTRY = Path(_tmpdir) / "registry"
    org_cli.PROCESSES = Path(_tmpdir) / "processes"
    org_cli.PROTOCOL = Path(_tmpdir) / "protocol"

    # Clean artifacts.jsonl between tests
    artpath = Path(_tmpdir) / "registry" / "artifacts.jsonl"
    if artpath.exists():
        artpath.unlink()

    # Clean fdm.json between tests
    fdm_path = Path(_tmpdir) / "registry" / "fdm.json"
    if fdm_path.exists():
        fdm_path.unlink()

    # Reset per-process state files (remove depends_on from previous tests)
    (Path(_tmpdir) / "processes" / "FEAT-001" / "state.yaml").write_text(
        yaml.dump({"process_id": "FEAT-001", "state": "P_COMPLETE", "assigned_to": "coder", "priority": 0.7, "template": "feature"})
    )
    (Path(_tmpdir) / "processes" / "FEAT-002" / "state.yaml").write_text(
        yaml.dump({"process_id": "FEAT-002", "state": "P_COMPLETE", "assigned_to": "alice", "priority": 0.5, "template": "feature"})
    )

    yield


# ---------------------------------------------------------------------------
# 1. Argument Parsing Tests
# ---------------------------------------------------------------------------

class TestCLIDepsArgumentParsing:
    """Test that dependency argument parsers build correctly."""

    def test_parser_deps_add(self):
        parser = build_parser()
        args = parser.parse_args(["deps-add", "FEAT-002", "FEAT-001"])
        assert args.command == "deps-add"
        assert args.process_id == "FEAT-002"
        assert args.depends_on_id == "FEAT-001"

    def test_parser_deps_remove(self):
        parser = build_parser()
        args = parser.parse_args(["deps-remove", "FEAT-002", "FEAT-001"])
        assert args.command == "deps-remove"
        assert args.process_id == "FEAT-002"
        assert args.depends_on_id == "FEAT-001"

    def test_parser_deps(self):
        parser = build_parser()
        args = parser.parse_args(["deps", "FEAT-001"])
        assert args.command == "deps"
        assert args.process_id == "FEAT-001"

    def test_parser_deps_analyze(self):
        parser = build_parser()
        args = parser.parse_args(["deps-analyze"])
        assert args.command == "deps-analyze"


# ---------------------------------------------------------------------------
# 2. Command Output Tests
# ---------------------------------------------------------------------------

class TestCLIDepsCommandOutput:
    """Test that dependency CLI commands produce expected stdout output."""

    def test_cmd_deps_add_success(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["deps-add", "FEAT-002", "FEAT-001"])
        cmd_deps_add(args)
        out = capsys.readouterr().out
        assert "FEAT-002" in out
        assert "FEAT-001" in out

    def test_cmd_deps_remove_success(self, capsys):
        # First add a dependency
        parser = build_parser()
        args = parser.parse_args(["deps-add", "FEAT-002", "FEAT-001"])
        cmd_deps_add(args)
        capsys.readouterr()  # clear

        # Now remove it
        args = parser.parse_args(["deps-remove", "FEAT-002", "FEAT-001"])
        cmd_deps_remove(args)
        out = capsys.readouterr().out
        assert "FEAT-002" in out
        assert "FEAT-001" in out

    def test_cmd_deps_shows_upstream_and_downstream(self, capsys):
        parser = build_parser()

        # Add FEAT-002 depends on FEAT-001
        args = parser.parse_args(["deps-add", "FEAT-002", "FEAT-001"])
        cmd_deps_add(args)
        capsys.readouterr()

        # Check FEAT-001: should show FEAT-002 as downstream
        args = parser.parse_args(["deps", "FEAT-001"])
        cmd_deps(args)
        out = capsys.readouterr().out
        assert "FEAT-001 Dependencies" in out
        assert "Downstream" in out
        assert "FEAT-002" in out

        # Check FEAT-002: should show FEAT-001 as upstream
        args = parser.parse_args(["deps", "FEAT-002"])
        cmd_deps(args)
        out = capsys.readouterr().out
        assert "FEAT-002 Dependencies" in out
        assert "Upstream" in out
        assert "FEAT-001" in out

    def test_cmd_deps_analyze_output(self, capsys):
        parser = build_parser()

        # Add a dependency first
        args = parser.parse_args(["deps-add", "FEAT-002", "FEAT-001"])
        cmd_deps_add(args)
        capsys.readouterr()

        # Analyze
        args = parser.parse_args(["deps-analyze"])
        cmd_deps_analyze(args)
        out = capsys.readouterr().out
        assert "Parallel Groups" in out
        assert "Summary" in out

    def test_cmd_dashboard_includes_dependencies(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["dashboard"])
        cmd_dashboard(args)
        out = capsys.readouterr().out
        assert "DEPENDENCIES" in out


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def teardown_module():
    shutil.rmtree(_tmpdir, ignore_errors=True)
