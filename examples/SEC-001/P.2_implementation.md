# SEC-001: P.2 — Implementation

**Agent:** builder
**Date:** 2026-03-18

All changes in `org_mcp_server.py`. No external dependencies added.

## D1 — Commit Message Sanitization

```python
def _sanitize_commit_message(msg: str) -> str:
    """Strip newlines to prevent git trailer injection."""
    return msg.replace('\n', ' ').replace('\r', ' ').strip()
```

## D2 — Agent ID Validation

```python
def _validate_agent_id(agent_id: str, allow_bootstrap: bool = False) -> tuple[bool, str]:
    config = _load_config()
    security_mode = config.get("security", {}).get("mode", "permissive")
    agents_data = _read_yaml(REGISTRY / "agents.yaml")
    known_ids = {a["id"] for a in agents_data.get("agents", [])}

    if agent_id in known_ids:
        return True, ""

    warning = f"agent_id '{agent_id}' not registered in agents.yaml"
    if security_mode == "strict" and not allow_bootstrap:
        _log_security_event(agent_id, "unknown_agent_rejected", warning)
        return False, f"SecurityError: {warning}"
    else:
        _log_security_event(agent_id, "unknown_agent_warning", warning)
        return True, warning
```

## D3 — Process ID Validation

```python
_PROCESS_ID_RE = re.compile(r'^[A-Z]+-[0-9]+$')

def _validate_process_id(process_id: str) -> tuple[bool, str]:
    if not _PROCESS_ID_RE.match(process_id):
        return False, f"Invalid process_id '{process_id}': must match [A-Z]+-[0-9]+"
    return True, ""
```

## D4 — Security Event Logging

```python
def _log_security_event(agent_id: str, event_type: str, detail: str):
    _append_jsonl(REGISTRY / "artifacts.jsonl", {
        "type": "security_event",
        "agent": agent_id,
        "action": event_type,
        "description": detail,
        "timestamp": _now_iso(),
    })
```

## Acceptance Criteria

- [x] Newline stripping prevents git trailer injection
- [x] Agent ID validation with strict/permissive mode
- [x] Process ID regex `^[A-Z]+-[0-9]+$`
- [x] Security events logged to artifacts.jsonl
- [x] `security_mode` in config.yaml, default: permissive
- [x] Bootstrapping exemption for `org_log_artifact` in strict mode
- [x] All existing tests pass
