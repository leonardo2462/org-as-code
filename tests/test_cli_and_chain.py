"""Tests for CLI commands, hash-chain roundtrip, template loading, and git sync."""

import json
import os
import shutil
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Set up isolated tmpdir BEFORE importing org_mcp_server
# ---------------------------------------------------------------------------

_tmpdir = tempfile.mkdtemp(prefix="org_cli_test_")
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
        {"id": "reviewer", "type": "ai", "name": "Reviewer Bot", "skills": ["review"], "status": "active", "capacity": 5},
    ]})
)

# config.yaml
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

# state.yaml with one process
(Path(_tmpdir) / "registry" / "state.yaml").write_text(
    yaml.dump({"processes": {
        "FEAT-001": {"state": "P_COMPLETE", "assigned_to": "coder", "priority": 0.7, "template": "feature", "notes": "Initial feature"},
    }})
)

# tensions.yaml
(Path(_tmpdir) / "registry" / "tensions.yaml").write_text(
    yaml.dump({"tensions": [
        {"id": "T-2026-001", "title": "Test tension", "description": "Something needs fixing", "status": "open", "priority": 0.6},
    ]})
)

# attractors.yaml
(Path(_tmpdir) / "registry" / "attractors.yaml").write_text(
    yaml.dump({"attractors": [
        {"id": "A-001", "title": "Quality", "description": "Improve quality", "status": "active", "weight": 0.8},
    ]})
)

# Create FEAT-001 process directory with an artifact
(Path(_tmpdir) / "processes" / "FEAT-001").mkdir()
(Path(_tmpdir) / "processes" / "FEAT-001" / "P.0_proposal.md").write_text("# FEAT-001: Test\n\nDescription here.")

# Now import
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("mcp", reason="mcp package required for org_mcp_server tests")
import org_mcp_server
from org_mcp_server import (
    _append_jsonl,
    _canonical_json,
    _hash_content,
    _read_jsonl,
    _read_yaml,
    _write_yaml,
    org_create_process,
    org_verify_chain,
    org_git_sync,
    _validate_agent_id,
)

# Import CLI commands
from org_cli import (
    build_parser,
    cmd_status,
    cmd_dashboard,
    cmd_verify,
    cmd_show,
    cmd_create,
    cmd_update,
    cmd_agents,
    cmd_tensions,
    cmd_attractors,
    cmd_log,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_paths():
    """Ensure module paths point to test tmpdir and clean artifacts between tests."""
    org_mcp_server.ORG_REPO = Path(_tmpdir)
    org_mcp_server.REGISTRY = Path(_tmpdir) / "registry"
    org_mcp_server.PROCESSES = Path(_tmpdir) / "processes"
    org_mcp_server.PROTOCOL = Path(_tmpdir) / "protocol"

    # Also patch org_cli's imported references
    import org_cli
    org_cli.REGISTRY = Path(_tmpdir) / "registry"
    org_cli.PROCESSES = Path(_tmpdir) / "processes"
    org_cli.PROTOCOL = Path(_tmpdir) / "protocol"

    # Clean artifacts.jsonl between tests
    artpath = Path(_tmpdir) / "registry" / "artifacts.jsonl"
    if artpath.exists():
        artpath.unlink()

    yield


# ---------------------------------------------------------------------------
# 1. CLI Tests
# ---------------------------------------------------------------------------

class TestCLIArgumentParsing:
    """Test that argument parser builds correctly and dispatches."""

    def test_parser_status(self):
        parser = build_parser()
        args = parser.parse_args(["status"])
        assert args.command == "status"

    def test_parser_dashboard(self):
        parser = build_parser()
        args = parser.parse_args(["dashboard"])
        assert args.command == "dashboard"

    def test_parser_verify(self):
        parser = build_parser()
        args = parser.parse_args(["verify"])
        assert args.command == "verify"

    def test_parser_show(self):
        parser = build_parser()
        args = parser.parse_args(["show", "FEAT-001"])
        assert args.command == "show"
        assert args.process_id == "FEAT-001"

    def test_parser_create(self):
        parser = build_parser()
        args = parser.parse_args(["create", "FEAT-099", "feature", "My Title", "My desc", "--agent", "coder", "--priority", "0.9"])
        assert args.command == "create"
        assert args.process_id == "FEAT-099"
        assert args.template == "feature"
        assert args.title == "My Title"
        assert args.priority == 0.9
        assert args.agent == "coder"

    def test_parser_update(self):
        parser = build_parser()
        args = parser.parse_args(["update", "FEAT-001", "COMMITTED", "--assign", "alice", "--notes", "done"])
        assert args.command == "update"
        assert args.state == "COMMITTED"
        assert args.assign == "alice"
        assert args.notes == "done"

    def test_parser_log_default_limit(self):
        parser = build_parser()
        args = parser.parse_args(["log"])
        assert args.limit == 20

    def test_parser_sync_pull_only(self):
        parser = build_parser()
        args = parser.parse_args(["sync"])
        assert args.command == "sync"
        assert args.message == []


class TestCLICommandOutput:
    """Test that CLI commands produce expected stdout output."""

    def test_cmd_status_shows_process(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["status"])
        cmd_status(args)
        out = capsys.readouterr().out
        assert "FEAT-001" in out
        assert "P_COMPLETE" in out

    def test_cmd_status_no_processes(self, capsys):
        # Temporarily empty the state
        state_path = Path(_tmpdir) / "registry" / "state.yaml"
        original = state_path.read_text()
        state_path.write_text(yaml.dump({"processes": {}}))
        try:
            parser = build_parser()
            args = parser.parse_args(["status"])
            cmd_status(args)
            out = capsys.readouterr().out
            assert "No active processes" in out
        finally:
            state_path.write_text(original)

    def test_cmd_dashboard_contains_sections(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["dashboard"])
        cmd_dashboard(args)
        out = capsys.readouterr().out
        assert "PROCESSES" in out
        assert "TENSIONS" in out
        assert "HEALTH" in out
        assert "AUDIT CHAIN" in out

    def test_cmd_verify_no_artifacts(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["verify"])
        cmd_verify(args)
        out = capsys.readouterr().out
        assert "No artifacts" in out

    def test_cmd_show_existing_process(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["show", "FEAT-001"])
        cmd_show(args)
        out = capsys.readouterr().out
        assert "FEAT-001" in out
        assert "P.0_proposal.md" in out

    def test_cmd_show_nonexistent_process(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["show", "NOPE-999"])
        cmd_show(args)
        out = capsys.readouterr().out
        assert "not found" in out

    def test_cmd_agents_output(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["agents"])
        cmd_agents(args)
        out = capsys.readouterr().out
        assert "alice" in out
        assert "coder" in out

    def test_cmd_tensions_output(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["tensions"])
        cmd_tensions(args)
        out = capsys.readouterr().out
        assert "T-2026-001" in out
        assert "open" in out

    def test_cmd_log_empty(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["log"])
        cmd_log(args)
        out = capsys.readouterr().out
        assert "No artifacts logged" in out


# ---------------------------------------------------------------------------
# 2. Hash-Chain Roundtrip Tests
# ---------------------------------------------------------------------------

class TestHashChainRoundtrip:
    """Write entries, verify passes, tamper, verify catches it."""

    def test_append_and_read_roundtrip(self):
        path = Path(_tmpdir) / "registry" / "artifacts.jsonl"
        _append_jsonl(path, {"agent": "coder", "action": "test", "description": "entry 1", "timestamp": "2026-01-01T00:00:00Z"})
        _append_jsonl(path, {"agent": "coder", "action": "test", "description": "entry 2", "timestamp": "2026-01-01T00:01:00Z"})
        entries = _read_jsonl(path)
        assert len(entries) == 2
        assert entries[0]["description"] == "entry 1"
        assert entries[1]["description"] == "entry 2"

    def test_chain_hashes_are_linked(self):
        path = Path(_tmpdir) / "registry" / "artifacts.jsonl"
        _append_jsonl(path, {"agent": "a", "action": "x", "description": "first", "timestamp": "t1"})
        _append_jsonl(path, {"agent": "a", "action": "x", "description": "second", "timestamp": "t2"})
        entries = _read_jsonl(path)
        assert entries[0]["prev_hash"] == "0" * 64  # genesis
        assert entries[1]["prev_hash"] == entries[0]["entry_hash"]

    def test_verify_valid_chain(self):
        path = Path(_tmpdir) / "registry" / "artifacts.jsonl"
        for i in range(3):
            _append_jsonl(path, {"agent": "coder", "action": "step", "description": f"entry {i}", "timestamp": f"t{i}"})
        result = org_verify_chain()
        assert "VALID" in result
        assert "3 chained entries" in result

    def test_verify_detects_tampered_content(self):
        path = Path(_tmpdir) / "registry" / "artifacts.jsonl"
        for i in range(3):
            _append_jsonl(path, {"agent": "coder", "action": "step", "description": f"entry {i}", "timestamp": f"t{i}"})

        # Tamper: modify the description of the second entry
        lines = path.read_text().strip().split("\n")
        entry = json.loads(lines[1])
        entry["description"] = "TAMPERED"
        lines[1] = json.dumps(entry)
        path.write_text("\n".join(lines) + "\n")

        result = org_verify_chain()
        assert "INTEGRITY VIOLATIONS" in result
        assert "mismatch" in result

    def test_verify_detects_broken_chain_link(self):
        path = Path(_tmpdir) / "registry" / "artifacts.jsonl"
        for i in range(3):
            _append_jsonl(path, {"agent": "coder", "action": "step", "description": f"entry {i}", "timestamp": f"t{i}"})

        # Tamper: change prev_hash of the third entry
        lines = path.read_text().strip().split("\n")
        entry = json.loads(lines[2])
        entry["prev_hash"] = "deadbeef" * 8
        lines[2] = json.dumps(entry)
        path.write_text("\n".join(lines) + "\n")

        result = org_verify_chain()
        assert "INTEGRITY VIOLATIONS" in result

    def test_hash_content_deterministic(self):
        assert _hash_content("hello") == _hash_content("hello")
        assert _hash_content("hello") != _hash_content("world")

    def test_canonical_json_sorted_keys(self):
        a = _canonical_json({"z": 1, "a": 2})
        b = _canonical_json({"a": 2, "z": 1})
        assert a == b
        assert '"a":2' in a


# ---------------------------------------------------------------------------
# 3. Template-Loading Tests
# ---------------------------------------------------------------------------

class TestTemplateLoading:
    """Test behavior when template files are missing or have invalid YAML."""

    def test_read_yaml_missing_file_returns_empty(self):
        result = _read_yaml(Path(_tmpdir) / "nonexistent.yaml")
        assert result == {}

    def test_read_yaml_valid_file(self):
        result = _read_yaml(Path(_tmpdir) / "protocol" / "process_templates" / "feature.yaml")
        assert result["name"] == "feature"
        assert len(result["steps"]) == 2

    def test_create_process_missing_template(self):
        """Creating a process with a nonexistent template should still work
        (uses default artifact name from empty steps list)."""
        result = org_create_process(
            process_id="TMPL-001",
            template="nonexistent_template",
            title="Test",
            description="Testing missing template",
            agent_id="coder",
        )
        # Should create the process (falls back to default artifact name)
        proc_dir = Path(_tmpdir) / "processes" / "TMPL-001"
        assert proc_dir.exists()
        assert "Created TMPL-001" in result

    def test_create_process_with_bugfix_template(self):
        result = org_create_process(
            process_id="BUG-001",
            template="bugfix",
            title="Fix a bug",
            description="Something broke",
            agent_id="coder",
        )
        assert "Created BUG-001" in result
        assert (Path(_tmpdir) / "processes" / "BUG-001" / "P.0_diagnosis.md").exists()

    def test_read_yaml_empty_file_returns_empty(self):
        empty_path = Path(_tmpdir) / "protocol" / "empty.yaml"
        empty_path.write_text("")
        result = _read_yaml(empty_path)
        assert result == {}

    def test_read_yaml_invalid_yaml_returns_empty(self):
        """Invalid YAML is caught gracefully and returns empty dict."""
        bad_path = Path(_tmpdir) / "protocol" / "bad.yaml"
        bad_path.write_text(":\n  - [invalid\n  yaml: {broken")
        result = _read_yaml(bad_path)
        assert result == {}


# ---------------------------------------------------------------------------
# 4. org_git_sync Integration Tests
# ---------------------------------------------------------------------------

class TestGitSync:
    """Integration tests using a real git repo in a tmpdir."""

    @pytest.fixture
    def git_repo(self):
        """Create a temporary git repo and patch ORG_REPO to point to it."""
        repo_dir = tempfile.mkdtemp(prefix="org_git_test_")
        repo = Path(repo_dir)

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, capture_output=True)

        # Create minimal structure for org_mcp_server
        (repo / "registry").mkdir()
        (repo / "processes").mkdir()
        (repo / "protocol" / "process_templates").mkdir(parents=True)

        # Copy config files
        shutil.copy(Path(_tmpdir) / "protocol" / "config.yaml", repo / "protocol" / "config.yaml")
        shutil.copy(Path(_tmpdir) / "registry" / "agents.yaml", repo / "registry" / "agents.yaml")

        # Initial commit
        (repo / "README.md").write_text("# Test repo\n")
        subprocess.run(["git", "add", "-A"], cwd=repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_dir, capture_output=True)

        # Patch module paths
        old_repo = org_mcp_server.ORG_REPO
        old_registry = org_mcp_server.REGISTRY
        old_processes = org_mcp_server.PROCESSES
        old_protocol = org_mcp_server.PROTOCOL

        org_mcp_server.ORG_REPO = repo
        org_mcp_server.REGISTRY = repo / "registry"
        org_mcp_server.PROCESSES = repo / "processes"
        org_mcp_server.PROTOCOL = repo / "protocol"

        yield repo

        # Restore
        org_mcp_server.ORG_REPO = old_repo
        org_mcp_server.REGISTRY = old_registry
        org_mcp_server.PROCESSES = old_processes
        org_mcp_server.PROTOCOL = old_protocol
        shutil.rmtree(repo_dir, ignore_errors=True)

    def test_pull_only_no_remote(self, git_repo):
        """Pull-only (no commit_message) should not fail fatally even without a remote."""
        result = org_git_sync(commit_message="", agent_id="")
        assert "Pull:" in result

    def test_commit_creates_git_commit(self, git_repo):
        """With a commit_message, changes should be committed."""
        # Create a file to commit
        (git_repo / "test_file.txt").write_text("hello world")

        result = org_git_sync(commit_message="add test file", agent_id="coder")
        assert "Committed:" in result
        assert "coder:" in result

        # Verify the commit exists in git log
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(git_repo), capture_output=True, text=True,
        )
        assert "coder: add test file" in log.stdout

    def test_commit_no_agent_id_fails(self, git_repo):
        """Commit without agent_id should return error."""
        (git_repo / "another.txt").write_text("data")
        result = org_git_sync(commit_message="test", agent_id="")
        assert "agent_id required" in result

    def test_commit_nothing_to_commit(self, git_repo):
        """If there are no changes, should report nothing to commit."""
        result = org_git_sync(commit_message="no changes", agent_id="coder")
        assert "Nothing to commit" in result

    def test_push_fails_gracefully_no_remote(self, git_repo):
        """Push should fail gracefully when no remote is configured."""
        (git_repo / "push_test.txt").write_text("test")
        result = org_git_sync(commit_message="push test", agent_id="coder")
        # Should still have committed successfully
        assert "Committed:" in result
        # Push output should be present (may contain error info or empty)
        assert "Push:" in result

    def test_unknown_agent_blocks_sync(self, git_repo):
        """Unknown agent should be rejected by git sync (even in permissive mode)."""
        (git_repo / "strict_test.txt").write_text("data")
        result = org_git_sync(commit_message="strict test", agent_id="unknown-agent")
        assert "not registered" in result
        assert "Error" in result

    def test_commit_sanitizes_message(self, git_repo):
        """Commit messages with newlines should be sanitized."""
        (git_repo / "sanitize_test.txt").write_text("data")
        result = org_git_sync(commit_message="line1\nline2\rline3", agent_id="coder")
        assert "Committed:" in result
        # The committed message should have no newlines
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(git_repo), capture_output=True, text=True,
        )
        assert "\n" not in log.stdout.strip()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def teardown_module():
    shutil.rmtree(_tmpdir, ignore_errors=True)
