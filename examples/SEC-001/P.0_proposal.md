# SEC-001: Harden MCP Server Against Commit Injection

**Process:** feature
**Agent:** architect
**Date:** 2026-03-15

## State

The `org_git_sync` tool constructs a git commit message by concatenating `agent_id` and `commit_message` without sanitization. An AI agent (or a compromised prompt) could pass messages containing shell metacharacters or git trailers. More critically, `agent_id` is not validated against `agents.yaml`, enabling agent impersonation.

**H(s) = 0.670 → ACTION required**

## Intention

Harden the MCP server against three attack vectors:
1. **Commit message injection** via unsanitized `commit_message` in `org_git_sync`
2. **Agent impersonation** via unvalidated `agent_id` in all write tools
3. **Path traversal** via unsanitized `process_id` in `org_create_process`

## Constraints

- No breaking changes to MCP tool signatures
- Validation must be configurable (strict/permissive)
- All validation failures logged to `artifacts.jsonl` as `security_event` entries
- No new external dependencies

## Decomposition

| Part | Description |
|------|-------------|
| D1 | Commit message sanitization: strip/escape shell metacharacters |
| D2 | Agent ID validation against agents.yaml (strict/permissive) |
| D3 | Process ID regex whitelist `[A-Z]+-[0-9]+` |
| D4 | Security event logging to artifacts.jsonl |
| D5 | `security_mode` in config.yaml (strict/permissive, default: permissive) |

## Validation

- [ ] `org_git_sync(agent_id="'; rm -rf /; echo '", commit_message="test")` returns error
- [ ] `org_create_process(process_id="../../../etc/passwd")` returns validation error
- [ ] Unknown agent in strict mode → error; in permissive → warning + proceed
- [ ] Security events appear in artifacts.jsonl
