# Changelog

All notable changes to org-as-code are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [2.3.1] — 2026-03-23

### Changed

- **Atomic YAML writes** — `_write_yaml()` now writes to a temporary file and renames (atomic on POSIX), preventing partial/corrupted state files on crash or power loss.
- **Safe dual-write pattern** — State-modifying operations (`org_create_process`, `org_add_dependency`, `org_remove_dependency`) use `_safe_audit_append()` for the audit trail: if the JSONL append fails after state was already written, a CRITICAL log is emitted instead of raising, preventing state-without-audit inconsistency from crashing the operation.
- **Buffered `_get_chain_tip()`** — Replaced character-by-character reverse seek with 8 KB block reads. Faster and safer with large JSONL files and multi-byte UTF-8 encodings.
- **Configurable git timeout** — `process_engine.git_timeout_seconds` in `config.yaml` (default: 30). Previously hardcoded.
- **Graceful test degradation** — All test modules that import `org_mcp_server` now use `pytest.importorskip("mcp")`, so the FDM-only tests (10) still run when the `mcp` package is not installed instead of all 146 failing at collection.

---

## [2.3.0] — 2026-03-22

### Added

- **Per-process state (default)** — Each process stores its state in `processes/{ID}/state.yaml`, eliminating concurrent write conflicts. `registry/state.yaml` is auto-generated as a read-only index. Auto-migration from monolithic state on first write. Configurable via `state_storage.mode` in `config.yaml`. 26 new tests.
- **FDM process dependencies** — Processes can declare `depends_on` relationships. New MCP tools: `org_add_dependency`, `org_remove_dependency`, `org_analyze_dependencies`, `org_read_dependencies`. Dependency validation rejects self-references, non-existent IDs, and invalid formats.
- **FDM graph engine** — `fdm.py` module (267 LOC, stdlib only) implementing Tarjan's SCC for cycle detection, Kahn's topological sort, parallel group computation, impact scoring, and bottleneck identification. 10 topology tests.
- **FDM register** — `registry/fdm.json` auto-generated on every dependency change. Contains nodes, edges, parallel groups, cycles, critical path, bottleneck, impact scores. Dependency changes logged in hash-chain audit trail.
- **CLI dependency commands** — `deps-add`, `deps-remove`, `deps` (upstream/downstream view), `deps-analyze` (full FDM analysis). Dashboard includes DEPENDENCIES section with cycle warnings.
- **71 new tests** across 4 test files (85→156 total): per-process state (26), graph topology (10), FDM integration (6), CLI dependencies (9), plus 20 more across phases.

---

## [2.2.0] — 2026-03-21

### Added

- **State transition enforcement** — COMMITTED state now requires at least one V-step in the audit trail for the process. The P↔V protocol is enforced, not just conventional. Configurable via `process_engine.enforce_transitions` in `config.yaml` (default: `true`). 3 new tests.
- **`pyproject.toml`** — Package metadata for PyPI distribution. Enables `pip install org-as-code` and provides `org` and `org-mcp-server` console entry points.
- **37 new tests** in `test_cli_and_chain.py`: CLI argument parsing (8), CLI output (9), hash-chain roundtrip with tamper detection (7), template loading edge cases (6), and `org_git_sync` integration with real git repo (7).

### Changed

- **Live health metrics** — `org_read_health` (MCP) and `dashboard`/`health` (CLI) now compute all metrics live from `state.yaml`, `artifacts.jsonl`, and `tensions.yaml` instead of reading static `health_metrics.yaml`. Metrics: active_processes, committed_total, avg_cycle_time_hours, chain_length, chain_integrity, open_tensions.
- **Vocabulary shift** — README uses practical terms (priority score, convergence score) instead of academic jargon (Hamiltonian, Semantic Energy). Formal terms preserved in THEORY.md with cross-references. No changes to config keys, function names, or API contracts.
- **`org_git_sync` hardened** — Now requires a registered agent for commit+push, even in permissive mode. This is the most impactful git operation and should not be available to unknown agents.
- **YAML error handling** — `_read_yaml` gracefully handles corrupt YAML files (logs warning, returns empty dict) instead of crashing.
- **File locking** — `_write_yaml` and `_append_jsonl` use advisory file locking (`fcntl.flock`) to prevent concurrent write corruption.
- **Python 3 compatibility** — All README commands use `python3`/`pip3` instead of `python`/`pip`.
- **State concurrency documentation** — Known Limitations clarifies design for 1–5 agents, documents migration path to per-process state files with concrete commands.
- **Removed health_metrics staleness limitation** — No longer applies; metrics are computed live.

---

## [2.1.0] — 2026-03-19

### Security

- **Integrated security validation into MCP server** — Process ID format validation (`[A-Z]+-[0-9]+`) now enforced on `org_create_process`, `org_read_process`, `org_update_state`, and `org_log_artifact`. Prevents path traversal via malformed IDs.
- **Agent ID validation** — All write tools validate agent IDs against `registry/agents.yaml`. Behaviour follows `security.mode` in `protocol/config.yaml`: `permissive` (default) logs warnings, `strict` rejects unknown agents.
- **Commit message sanitization** — `org_git_sync` strips `\n` and `\r` from commit messages to prevent git trailer injection (e.g., fake Co-authored-by entries).
- **Security event logging** — Unknown agent attempts are logged as `security_event` entries in the hash-chained audit trail.
- **Bootstrap exemption** — `org_log_artifact` allows unknown agents in strict mode (via `allow_bootstrap=True`) to support initial bootstrapping.

### Added

- `tests/test_security.py` — 26 pytest tests covering process ID validation, commit sanitization, agent ID validation (permissive + strict modes), and integration tests for all write tools.

---

## [2.0.0] — 2026-03-19

Initial public release.

### Features

- **P↔V Governance Protocol** — Strict state machine (P_READY → P_COMPLETE → V_COMPLETE → COMMITTED / ABANDONED) governing all organizational decisions via expand-contract rhythm.
- **MCP Server** — 14 tools over stdio transport (JSON-RPC 2.0). Compatible with Claude Code, Windsurf, and Google Gemini (schema auto-sanitized). Read (7), write (5), and governance (3) operations.
- **CLI** — 16 commands with ANSI color output: status, tensions, attractors, agents, health, log, show, verify, dashboard, create, update, artifact, tension-add, tension-resolve, priority, sync.
- **SHA-256 Hash-Chain Audit Trail** — Append-only JSONL with cryptographic linking. Each entry's hash covers prev_hash + canonical JSON content. Tamper-evident, Git-versioned. Verifiable via `org verify` (CLI) or `org_verify_chain` (MCP).
- **Hamiltonian Priority** — H(s) = w₁·urgency + w₂·commitment + w₃·demand + w₄·blocking. Configurable weights and thresholds. Escalation at H(s) ≥ 0.8, action trigger at H(s) ≥ 0.5.
- **Process Templates** — `feature.yaml` (4-step P/V flow), `bugfix.yaml` (4-step diagnosis flow), `feature_v2.yaml` (conditional human gate based on Hamiltonian score).
- **Tension & Attractor Registry** — Formal tracking of unresolved problems and strategic goals as YAML.
- **Maintenance Tools** — `tools/fix_hash_chain.py` (rebuild corrupted chains), `tools/per_process_state.py` (migrate to per-process state files).

### Configuration

- `protocol/config.yaml` includes: Hamiltonian weights, process engine limits, concurrency settings, commit conventions, security mode (permissive/strict), state storage mode (monolithic/per_process).

### Known Limitations

- **State concurrency** — Monolithic `registry/state.yaml`. Migration tool available in `tools/per_process_state.py`. Per-process state will become the default in a future release.
- **Security mode** — Resolved in v2.1.0: validation now integrated into all MCP server write tools. Default mode is `permissive` (warns on unknown agents). Set `security.mode: strict` in `protocol/config.yaml` for production use.
- **Health metrics** — Manually maintained. Not auto-updated by the protocol.
- **Local tamper-evidence** — Hash chain provides evidence, not resistance. Force-push can rewrite history.

### Independently Evaluated

Stress-tested by Manus AI across three evaluation rounds:

- **v1** — Identified hash-chain integrity bug (fake 62-char hashes), CLI verify gap, state scalability bottleneck, and security attack vectors. Full experiment simulated a 4-person AI startup with PERF-001 (document pipeline optimization) and BUG-002 (memory leak fix).
- **v2** — SEC-001 process demonstrated the P↔V protocol catching an architectural error: Critic rejected a flawed commit sanitization proposal (shell metacharacter stripping) because subprocess.run with list args is not shell-invoked. Forced correct fix (newline-only stripping). SCALE-001 validated the per-process state migration approach.
- **v3** — Delta analysis confirmed all fixes applied correctly.
