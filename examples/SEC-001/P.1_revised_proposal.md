# SEC-001: P.1 — Revised Security Proposal

**Agent:** architect
**Date:** 2026-03-17
**Responding to:** V.0 rejection by critic

## Response to Blockers

### B1 — Commit Message Sanitization (REVISED)

The critic is correct: `subprocess.run()` with a list does not invoke a shell. Shell metacharacters are not a vector. The actual risk is **git trailer injection** via embedded newlines.

**Revised D1:** Strip `\n` and `\r` from `commit_message` only.

```python
def _sanitize_commit_message(msg: str) -> str:
    """Strip newlines to prevent git trailer injection."""
    return msg.replace('\n', ' ').replace('\r', ' ').strip()
```

### B2 — Path Traversal Threat Model (REVISED)

The regex whitelist is the right defense because:
1. **Format enforcement** — `FEAT-001` is the documented format. Anything else is a protocol violation.
2. **Defense in depth** — prevents `FEAT-001/../registry` from creating unexpected directories.

**Revised threat model:** The primary risk is process ID format violations, not path traversal to system files.

### B3 — Agent Bootstrapping (RESOLVED)

- **Strict mode:** rejects unknown agents in all write tools *except* `org_log_artifact` (bootstrap exemption)
- **Permissive mode** (default): logs warning, proceeds

## Revised Decomposition

| Part | Description | Change from P.0 |
|------|-------------|-----------------|
| D1 | Strip `\n`/`\r` from commit_message only | Revised: not all metacharacters |
| D2 | Agent ID validation (strict/permissive) | Revised: bootstrap exemption |
| D3 | Process ID regex `^[A-Z]+-[0-9]+$` | Unchanged (revised threat model) |
| D4 | Security event logging | Unchanged |
| D5 | `security_mode: permissive` in config.yaml | Unchanged |
