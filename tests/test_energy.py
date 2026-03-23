"""Tests for semantic energy E(x) calculation, convergence tracking, and classification."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

# Set ORG_REPO_PATH before importing org_mcp_server
_tmpdir = tempfile.mkdtemp(prefix="org_energy_test_")
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

# Config with energy settings
(Path(_tmpdir) / "protocol" / "config.yaml").write_text(
    yaml.dump({
        "hamiltonian": {
            "weights": {"urgency": 0.3, "commitment": 0.2, "demand": 0.3, "blocking": 0.2},
            "thresholds": {"action_trigger": 0.5, "escalation": 0.8},
        },
        "energy": {
            "weights": {"gaps": 0.30, "inconsistencies": 0.30, "uncertainty": 0.25, "evidence": 0.15},
            "thresholds": {"convergence": 0.10, "minor_revision": 0.30},
        },
        "security": {"mode": "permissive"},
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

# Empty state and tensions
(Path(_tmpdir) / "registry" / "state.yaml").write_text(yaml.dump({"processes": {}}))
(Path(_tmpdir) / "registry" / "tensions.yaml").write_text(yaml.dump({"tensions": []}))

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("mcp", reason="mcp package required for org_mcp_server tests")
import org_mcp_server
from org_mcp_server import (
    org_calculate_energy,
    org_log_artifact,
    org_read_convergence,
    _read_jsonl,
)

# Patch module-level paths to this test's tmpdir
org_mcp_server.ORG_REPO = Path(_tmpdir)
org_mcp_server.REGISTRY = Path(_tmpdir) / "registry"
org_mcp_server.PROCESSES = Path(_tmpdir) / "processes"
org_mcp_server.PROTOCOL = Path(_tmpdir) / "protocol"
REGISTRY = org_mcp_server.REGISTRY


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


# ─── E(x) calculation ──────────────────────────────────────────────────────


class TestEnergyCalculation:
    def test_zero_energy(self):
        """All zeros should give E(x) = 0."""
        result = org_calculate_energy()
        assert "E(x) = 0.0000" in result
        assert "READY to commit" in result

    def test_high_gaps(self):
        """High gaps should produce high energy and flag as dominant."""
        result = org_calculate_energy(gaps=0.9)
        # 0.3 * 0.81 = 0.243, which is between 0.10 and 0.30 → MINOR
        assert "MINOR revision" in result
        assert "Dominant tension: gaps" in result

    def test_high_evidence_reduces(self):
        """Evidence should reduce energy (negative term)."""
        result_without = org_calculate_energy(gaps=0.5)
        result_with = org_calculate_energy(gaps=0.5, evidence=0.8)
        # Parse E(x) values
        e_without = float(result_without.split("E(x) = ")[1].split("\n")[0])
        e_with = float(result_with.split("E(x) = ")[1].split("\n")[0])
        assert e_with < e_without

    def test_quadratic_penalty(self):
        """One large gap should produce higher energy than multiple small gaps."""
        # One gap at 0.9: 0.3 * 0.81 = 0.243
        result_one = org_calculate_energy(gaps=0.9)
        e_one = float(result_one.split("E(x) = ")[1].split("\n")[0])

        # Three small: gaps=0.3, inconsistencies=0.3, uncertainty=0.3
        # 0.3*0.09 + 0.3*0.09 + 0.25*0.09 = 0.027 + 0.027 + 0.0225 = 0.0765
        result_three = org_calculate_energy(gaps=0.3, inconsistencies=0.3, uncertainty=0.3)
        e_three = float(result_three.split("E(x) = ")[1].split("\n")[0])

        assert e_one > e_three, "Quadratic: one large gap > three small gaps"

    def test_energy_floor_at_zero(self):
        """E(x) should never go negative (evidence can't overcome zero tensions)."""
        result = org_calculate_energy(evidence=1.0)
        e = float(result.split("E(x) = ")[1].split("\n")[0])
        assert e >= 0.0

    def test_convergence_threshold(self):
        """E(x) just below 0.10 should be READY to commit."""
        # gaps=0.3: 0.3 * 0.09 = 0.027, evidence=0.0 → E = 0.027
        result = org_calculate_energy(gaps=0.3)
        assert "READY to commit" in result

    def test_minor_revision_threshold(self):
        """E(x) between 0.10 and 0.30 should be MINOR revision."""
        # gaps=0.6: 0.3 * 0.36 = 0.108
        result = org_calculate_energy(gaps=0.6)
        assert "MINOR revision" in result

    def test_major_revision_threshold(self):
        """E(x) >= 0.30 should be MAJOR revision."""
        # gaps=0.9, inconsistencies=0.5: 0.3*0.81 + 0.3*0.25 = 0.243 + 0.075 = 0.318
        result = org_calculate_energy(gaps=0.9, inconsistencies=0.5)
        assert "MAJOR revision" in result

    def test_custom_weights(self):
        """Custom weights should override config defaults."""
        result = org_calculate_energy(gaps=1.0, w_gaps=1.0)
        e = float(result.split("E(x) = ")[1].split("\n")[0])
        assert abs(e - 1.0) < 0.001


# ─── Auto E(x) in org_log_artifact ─────────────────────────────────────────


class TestArtifactEnergyIntegration:
    def test_v_step_with_convergence_gets_energy(self):
        """V-step artifacts with convergence data should get energy_score."""
        extra = json.dumps({
            "convergence": {
                "gaps": 0.5,
                "inconsistencies": 0.3,
                "uncertainty": 0.4,
                "evidence": 0.6,
            }
        })
        org_log_artifact(
            agent_id="reviewer",
            action="V.0_review",
            description="Review with convergence",
            process_id="FEAT-001",
            extra=extra,
        )
        entries = _read_jsonl(REGISTRY / "artifacts.jsonl")
        assert len(entries) == 1
        assert "energy_score" in entries[0]
        assert entries[0]["energy_score"] > 0

    def test_p_step_no_energy(self):
        """P-step artifacts should not get energy_score even with convergence data."""
        extra = json.dumps({
            "convergence": {"gaps": 0.5, "inconsistencies": 0.3, "uncertainty": 0.4, "evidence": 0.6}
        })
        org_log_artifact(
            agent_id="coder",
            action="P.0_proposal",
            description="Proposal",
            process_id="FEAT-001",
            extra=extra,
        )
        entries = _read_jsonl(REGISTRY / "artifacts.jsonl")
        assert "energy_score" not in entries[0]

    def test_v_step_without_convergence_no_energy(self):
        """V-step without convergence data should not get energy_score."""
        org_log_artifact(
            agent_id="reviewer",
            action="V.0_review",
            description="Simple review",
            process_id="FEAT-001",
        )
        entries = _read_jsonl(REGISTRY / "artifacts.jsonl")
        assert "energy_score" not in entries[0]


# ─── Convergence tracking ──────────────────────────────────────────────────


class TestConvergenceTracking:
    def _log_v_step(self, process_id, action, gaps, inconsistencies, uncertainty, evidence):
        """Helper to log a V-step with convergence scores."""
        extra = json.dumps({
            "convergence": {
                "gaps": gaps,
                "inconsistencies": inconsistencies,
                "uncertainty": uncertainty,
                "evidence": evidence,
            }
        })
        org_log_artifact(
            agent_id="reviewer",
            action=action,
            description=f"Review {action}",
            process_id=process_id,
            extra=extra,
        )

    def test_no_data(self):
        result = org_read_convergence("FEAT-999")
        assert "No convergence data" in result

    def test_invalid_process_id(self):
        result = org_read_convergence("invalid")
        assert "Invalid process_id" in result

    def test_single_measurement(self):
        self._log_v_step("FEAT-001", "V.0_review", 0.8, 0.5, 0.6, 0.3)
        result = org_read_convergence("FEAT-001")
        assert "INSUFFICIENT DATA" in result
        assert "(initial)" in result

    def test_converging(self):
        """Decreasing E(x) across V-steps → CONVERGING."""
        self._log_v_step("FEAT-002", "V.0_review", 0.8, 0.6, 0.7, 0.2)
        self._log_v_step("FEAT-002", "V.1_review", 0.4, 0.2, 0.3, 0.5)
        self._log_v_step("FEAT-002", "V.2_review", 0.1, 0.1, 0.1, 0.8)
        result = org_read_convergence("FEAT-002")
        assert "CONVERGING" in result

    def test_diverging(self):
        """Increasing E(x) across V-steps → DIVERGING."""
        self._log_v_step("FEAT-003", "V.0_review", 0.2, 0.1, 0.1, 0.8)
        self._log_v_step("FEAT-003", "V.1_review", 0.5, 0.4, 0.5, 0.3)
        self._log_v_step("FEAT-003", "V.2_review", 0.8, 0.7, 0.8, 0.1)
        result = org_read_convergence("FEAT-003")
        assert "DIVERGING" in result

    def test_stagnating(self):
        """Flat E(x) across V-steps → STAGNATING."""
        self._log_v_step("FEAT-004", "V.0_review", 0.5, 0.3, 0.4, 0.4)
        self._log_v_step("FEAT-004", "V.1_review", 0.5, 0.3, 0.4, 0.4)
        result = org_read_convergence("FEAT-004")
        assert "STAGNATING" in result

    def test_filters_by_process_id(self):
        """Only entries for the requested process should be included."""
        self._log_v_step("FEAT-005", "V.0_review", 0.8, 0.5, 0.6, 0.2)
        self._log_v_step("FEAT-006", "V.0_review", 0.3, 0.2, 0.1, 0.7)
        result = org_read_convergence("FEAT-005")
        assert "Measurements: 1" in result


# ─── Cleanup ────────────────────────────────────────────────────────────────


def teardown_module():
    shutil.rmtree(_tmpdir, ignore_errors=True)
