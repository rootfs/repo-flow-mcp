# Repo Flow MCP Implementation Plan

## Goal

Build a dedicated repository and MCP server that exposes a production-ready, unified graph of:

- code entities and dependencies
- script orchestration dependencies
- operational artifact and environment contracts

## Scope

### Graph layers

1. Code layer
- Symbol discovery and language-aware dependency extraction.
- Import and call relationships where feasible.

2. Script layer
- Shell scripts, Makefile targets, and CI workflow step chaining.
- Command-level invocation edges.

3. Operational layer
- Artifact production/consumption lineage.
- Environment variable contracts and stage gating dependencies.

### API/MCP tools

1. `build_flow_graph`
- Build complete graph for a repository path.

2. `get_upstream_downstream`
- Return bounded incoming/outgoing neighborhoods for impact analysis.

3. `trace_artifact_lineage`
- Return full producer/consumer chain for artifacts.

4. `find_broken_stage_contracts`
- Detect missing producers, orphan products, and likely stage handoff breaks.

5. `explain_runbook_path`
- Explain shortest execution/dependency path between two nodes.

## Architecture

### Modules

- `models.py`: graph schema, node/edge types, validation helpers.
- `settings.py`: runtime config and scan policy.
- `graph_builder.py`: orchestrator that scans files and merges parser outputs.
- `parsers/`: pluggable parsers by source type.
- `server.py`: MCP tool registration and output contracts.
- `cli.py`: entrypoint for MCP serving and local graph export.

### Quality gates

- Deterministic IDs and stable output shape.
- Best-effort parser behavior with per-file error isolation.
- Unit tests for parser extraction and graph invariants.
- Type checks and linting config included.

## Delivery sequence

1. Repository scaffold and packaging.
2. Graph schema and parser interfaces.
3. Parser implementations (code, scripts, workflow, container configs).
4. Graph builder and graph query utilities.
5. MCP server exposure of analysis tools.
6. Test fixtures and unit tests.
7. Documentation and operational instructions.

## Non-goals (v1)

- Full semantic execution emulation.
- Language-complete call graph fidelity for every ecosystem.
- Runtime tracing agents.

## Production readiness criteria

- Installable package with CLI entrypoint.
- MCP server starts and responds to all tool calls.
- Unit tests pass with representative fixtures.
- Clear failure modes and structured error reporting.
- Docs describe setup, operation, and known limitations.
