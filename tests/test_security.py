"""Tests for security validation functions in org_mcp_server.py."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# Set ORG_REPO_PATH before importing org_mcp_server
_tmpdir = tempfile.mkdtemp(prefix="org_test_")
os.environ["ORG_REPO_PATH"] = _tmpdir

# Create minimal repo structure
(Path(_tmpdir) / "registry").mkdir()
(Path(_tmpdir) / "processes").mkdir()
(Path(_tmpdir) / "protocol" / "process_templates").mkdir(parents=True)

# Minimal agents.yaml
(Path(_tmpdir) / "registry" / "agents.yaml").write_text(
    yaml.dump({"agents": [
        {"id": "alice", "type": "human", "skills": ["review"], "status": "active", "capacity": 5},
        {"id": "coder", "type": "ai", "skills": ["code"], "status": "active", "capacity": 10},
        {"id": "reviewer", "type": "ai", "skills": ["review"], "status": "active", "capacity": 5},
    ]})
)

# Minimal config.yaml (permissive by default)
(Path(_tmpdir) / "protocol" / "config.yaml").write_text(
    yaml.dump({
        "hamiltonian": {
            "weights": {"urgency": 0.3, "commitment": 0.2, "demand": 0.3, "blocking": 0.2},
            "thresholds": {"action_trigger": 0.5, "escalation": 0.8},
        },
        "security": {"mode": "permissive"},
        "process_engine": {"enforce_transitions": True},
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

# Empty state.yaml
(Path(_tmpdir) / "registry" / "state.yaml").write_text(yaml.dump({"processes": {}}))

# Empty tensions.yaml
(Path(_tmpdir) / "registry" / "tensions.yaml").write_text(yaml.dump({"tensions": []}))

# Now import
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("mcp", reason="mcp package required for org_mcp_server tests")
import org_mcp_server
from org_mcp_server import (
    _sanitize_commit_message,
    _validate_agent_id,
    _validate_process_id,
    org_create_process,
    org_log_artifact,
    org_read_process,
    org_update_state,
)

# Patch module-level paths to this test's tmpdir
org_mcp_server.ORG_REPO = Path(_tmpdir)
org_mcp_server.REGISTRY = Path(_tmpdir) / "registry"
org_mcp_server.PROCESSES = Path(_tmpdir) / "processes"
org_mcp_server.PROTOCOL = Path(_tmpdir) / "protocol"


@pytest.fixture(autouse=True)
def _setup_paths_and_clean():
    """Ensure module paths point to this test's tmpdir and clean artifacts."""
    org_mcp_server.ORG_REPO = Path(_tmpdir)
    org_mcp_server.REGISTRY = Path(_tmpdir) / "registry"
    org_mcp_server.PROCESSES = Path(_tmpdir) / "processes"
    org_mcp_server.PROTOCOL = Path(_tmpdir) / "protocol"
    path = Path(_tmpdir) / "registry" / "artifacts.jsonl"
    if path.exists():
        path.unlink()
    yield


# ─── Process ID validation ──────────────────────────────────────────────────


class TestProcessIdValidation:
    @pytest.mark.parametrize("pid", ["FEAT-001", "BUG-042", "SCALE-100", "A-1"])
    def test_valid_ids(self, pid):
        ok, msg = _validate_process_id(pid)
        assert ok, f"Expected valid for {pid}: {msg}"

    @pytest.mark.parametrize("pid", [
        "feat-001",              # lowercase
        "FEAT001",               # missing hyphen
        "FEAT-",                 # missing number
        "-001",                  # missing prefix
        "FEAT-001/../registry",  # path traversal
        "",                      # empty
        "FEAT-001 EVIL",         # space injection
    ])
    def test_invalid_ids(self, pid):
        ok, msg = _validate_process_id(pid)
        assert not ok, f"Expected invalid for {pid!r}"
        assert "Invalid process_id" in msg


# ─── Commit message sanitization ────────────────────────────────────────────


class TestCommitSanitization:
    def test_normal_message(self):
        assert _sanitize_commit_message("fix: update auth") == "fix: update auth"

    def test_newline_injection(self):
        result = _sanitize_commit_message("fix\nCo-authored-by: attacker")
        assert "\n" not in result
        assert "Co-authored-by" in result  # text preserved, just on same line

    def test_carriage_return(self):
        result = _sanitize_commit_message("fix\r\ntrailer")
        assert "\r" not in result
        assert "\n" not in result

    def test_shell_metacharacters_preserved(self):
        """Shell metacharacters are NOT a vector (subprocess with list args)."""
        msg = "fix: handle $(x > 0)"
        assert _sanitize_commit_message(msg) == msg

    def test_backticks_preserved(self):
        msg = "fix: use `os.replace`"
        assert _sanitize_commit_message(msg) == msg


# ─── Agent ID validation ────────────────────────────────────────────────────


class TestAgentIdValidation:
    def test_known_agent_valid(self):
        ok, msg = _validate_agent_id("alice")
        assert ok
        assert msg == ""

    def test_unknown_agent_permissive(self):
        """In permissive mode, unknown agents are allowed with a warning."""
        ok, msg = _validate_agent_id("unknown-agent")
        assert ok
        assert "Warning" in msg or "not registered" in msg

    def test_unknown_agent_strict(self):
        """In strict mode, unknown agents are rejected."""
        config_path = Path(_tmpdir) / "protocol" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["security"]["mode"] = "strict"
        config_path.write_text(yaml.dump(config))
        try:
            ok, msg = _validate_agent_id("unknown-agent")
            assert not ok
            assert "SecurityError" in msg
        finally:
            config["security"]["mode"] = "permissive"
            config_path.write_text(yaml.dump(config))

    def test_unknown_agent_strict_bootstrap(self):
        """In strict mode, bootstrap exemption allows unknown agents."""
        config_path = Path(_tmpdir) / "protocol" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["security"]["mode"] = "strict"
        config_path.write_text(yaml.dump(config))
        try:
            ok, msg = _validate_agent_id("new-agent", allow_bootstrap=True)
            assert ok
        finally:
            config["security"]["mode"] = "permissive"
            config_path.write_text(yaml.dump(config))


# ─── Integration: write tools enforce validation ─────────────────────────────


class TestWriteToolValidation:
    def test_create_process_rejects_bad_id(self):
        result = org_create_process(
            process_id="invalid-id",
            template="feature",
            title="Test",
            description="Test",
            agent_id="coder",
        )
        assert "Invalid process_id" in result

    def test_create_process_rejects_unknown_agent_strict(self):
        config_path = Path(_tmpdir) / "protocol" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["security"]["mode"] = "strict"
        config_path.write_text(yaml.dump(config))
        try:
            result = org_create_process(
                process_id="TEST-001",
                template="feature",
                title="Test",
                description="Test",
                agent_id="unknown-agent",
            )
            assert "SecurityError" in result
            # Directory should NOT have been created
            assert not (Path(_tmpdir) / "processes" / "TEST-001").exists()
        finally:
            config["security"]["mode"] = "permissive"
            config_path.write_text(yaml.dump(config))

    def test_create_process_succeeds_valid(self):
        result = org_create_process(
            process_id="VALID-001",
            template="feature",
            title="Valid Process",
            description="A valid test process",
            agent_id="coder",
        )
        assert "Created VALID-001" in result
        assert (Path(_tmpdir) / "processes" / "VALID-001" / "P.0_proposal.md").exists()

    def test_read_process_rejects_bad_id(self):
        result = org_read_process("../etc/passwd")
        assert "Invalid process_id" in result

    def test_log_artifact_validates_process_id(self):
        result = org_log_artifact(
            agent_id="coder",
            action="test",
            description="test",
            process_id="invalid!",
        )
        assert "Invalid process_id" in result

    def test_update_state_rejects_bad_id(self):
        result = org_update_state(
            process_id="invalid",
            state="COMMITTED",
        )
        assert "Invalid process_id" in result


# ─── State transition enforcement ─────────────────────────────────────────────


class TestStateTransitionEnforcement:
    def _create_process(self, pid):
        """Helper: create a process so it exists in state.yaml."""
        org_create_process(
            process_id=pid,
            template="feature",
            title="Test",
            description="Test",
            agent_id="coder",
        )

    def test_commit_without_v_step_blocked(self):
        """COMMITTED without any V-step should be blocked."""
        self._create_process("BLOCK-001")
        result = org_update_state(process_id="BLOCK-001", state="COMMITTED")
        assert "Cannot commit" in result
        assert "no V-step" in result

    def test_commit_with_v_step_allowed(self):
        """COMMITTED with a V-step in audit trail should succeed."""
        self._create_process("ALLOW-001")
        org_log_artifact(
            agent_id="reviewer",
            action="V.0_review",
            description="Approved",
            process_id="ALLOW-001",
        )
        result = org_update_state(process_id="ALLOW-001", state="COMMITTED")
        assert "COMMITTED" in result
        assert "Cannot commit" not in result

    def test_commit_without_v_step_enforcement_disabled(self):
        """With enforce_transitions: false, COMMITTED without V-step should work."""
        config_path = Path(_tmpdir) / "protocol" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["process_engine"]["enforce_transitions"] = False
        config_path.write_text(yaml.dump(config))
        try:
            self._create_process("SKIP-001")
            result = org_update_state(process_id="SKIP-001", state="COMMITTED")
            assert "COMMITTED" in result
            assert "Cannot commit" not in result
        finally:
            config["process_engine"]["enforce_transitions"] = True
            config_path.write_text(yaml.dump(config))


# ─── Cleanup ─────────────────────────────────────────────────────────────────


def teardown_module():
    shutil.rmtree(_tmpdir, ignore_errors=True)
