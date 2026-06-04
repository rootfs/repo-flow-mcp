# repo-flow-mcp

`repo-flow-mcp` is a production-oriented Model Context Protocol (MCP) server that builds a unified graph across:

- code-level entities and dependencies (imports, calls)
- script-level orchestration (shell, Makefile, workflow steps)
- operational dependency flow (artifacts, env contracts, stage handoffs)

## Why this exists

Most code graph tools cover symbols and imports well, but operational repos also depend on script chains, CI jobs, build targets, and artifact lineage. This server combines those layers into one queryable graph for AI agents and humans.

## Features

- Unified graph model with typed nodes and edges.
- Parsers for:
  - Python code (`ast` imports/calls)
  - JS/TS imports plus symbol/call extraction (regex-based)
  - Go, Rust, Java, and C/C++ dependency imports/includes (regex-based)
  - Markdown dependency parsing for skills/docs links and tool references
  - shell scripts (`.sh`)
  - Makefiles
  - GitHub Actions workflows
  - GitLab CI (`.gitlab-ci.yml`)
  - Jenkinsfile pipeline steps
  - CMake and Bazel build files
  - Dockerfile and docker-compose
- MCP tools:
  - `build_flow_graph`
  - `get_upstream_downstream`
  - `trace_artifact_lineage`
  - `find_broken_stage_contracts`
  - `explain_runbook_path`
  - `repo_localizer_overview`
  - `repo_localizer_entrypoints`
  - `code_localizer_function_to_script`
  - `code_localizer_node_context`
- Defensive parsing and structured error output.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Run (stdio MCP)

```bash
repo-flow-mcp serve
```

## Run tests

```bash
pytest
```

## MCP tools summary

### `build_flow_graph`
Builds and returns the full graph for a repository root.

### `get_upstream_downstream`
Returns incoming and outgoing neighborhoods for a node up to a configurable depth.

### `trace_artifact_lineage`
Traces all producers and consumers for a named artifact node.

### `find_broken_stage_contracts`
Finds flow contract issues, such as consumed artifacts with no producer and produced artifacts with no consumer.

### `explain_runbook_path`
Finds a shortest dependency/execution path between two nodes.

### TUI subagent interfaces

These tool interfaces are designed for direct use by TUI repo/code localizer subagents:

- `repo_localizer_overview(path, include_hidden, top_k)`
- `repo_localizer_entrypoints(path, include_hidden, limit)`
- `code_localizer_function_to_script(path, function_query, limit)`
- `code_localizer_node_context(path, node_id, depth)`

Ready-to-copy subagent contracts are provided in `docs/tui-subagent-contracts.prompt.md`.

## Production notes

- Ignore patterns and scan safety limits are configurable via environment variables.
- Parsing is best-effort and non-fatal per file; errors are captured and returned in graph metadata.
- Graph node IDs are deterministic for stable automation and comparisons.
