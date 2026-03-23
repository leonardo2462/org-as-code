# Contributing to org-as-code

## What Can Be Contributed

- **Process templates** — New workflow templates beyond feature and bugfix
- **MCP tools** — Additional tools for the MCP server
- **Integrations** — Bridges to other AI agent frameworks
- **Documentation** — Usage guides, tutorials, case studies
- **Bug fixes** — Issues with the MCP server, CLI, or protocol

## What Should Not Be Changed

The following are canonical:

- The P↔V protocol structure
- The hash-chain audit mechanism
- The Hamiltonian priority formula
- The state machine (P_READY → P_COMPLETE → V_COMPLETE → COMMITTED)

If you believe these need modification, open an issue for discussion first.

## How to Contribute

1. Fork the repository
2. Create a feature branch
3. Make your changes with clear commit messages
4. Ensure all existing tests pass
5. Submit a pull request with a description of:
   - What you changed
   - Why it matters
   - How to test it

## Development Setup

```bash
# Clone and install
git clone https://github.com/SYNTRIAD/org-as-code.git
cd org-as-code
pip install -r requirements.txt

# Run tests
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_security.py -v

# Verify hash-chain integrity
python org_cli.py verify

# Check the dashboard
python org_cli.py dashboard
```

### Dependencies

- **Runtime:** `pyyaml`, `mcp`
- **Testing:** `pytest`

### Project Structure

```
org_mcp_server.py   ← MCP server (21 tools), core logic
org_cli.py          ← CLI wrapper (23 commands)
fdm.py              ← Dependency graph engine (Tarjan, Kahn)
tests/              ← 156 tests across 7 files
```

## Code Style

- Python: follow PEP 8
- YAML: 2-space indentation, no tabs
- Markdown: one sentence per line in source

## Commit Convention

Follow the org-as-code commit format:

```
{your-id}: {P|V}.{n} {process-id} -- {description}
```

Example: `contributor: P.0 FEAT-003 -- Add rate limiting template`
