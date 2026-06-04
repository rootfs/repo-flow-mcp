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
  - `code_localizer_function_to_script_batch`
  - `code_localizer_node_context`
  - `code_localizer_node_context_batch`
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
- `code_localizer_function_to_script_batch(path, function_queries, limit_per_query)`
- `code_localizer_node_context(path, node_id, depth)`
- `code_localizer_node_context_batch(path, node_ids, depth)`

Ready-to-copy subagent contracts are provided in `docs/tui-subagent-contracts.prompt.md`.

## Effective use for agent integrations

These notes come from real review/localization agents driving this server. They
matter because the host harness — not the server — usually decides whether
tool calls actually fire.

### Prefer the `_batch` variants for multi-symbol questions

`code_localizer_function_to_script_batch` and `code_localizer_node_context_batch`
take a list of queries and return one structured result. For a PR with N changed
symbols, that is **one** MCP round-trip instead of N. The graph build is reused
inside the batch, so latency stays close to a single call.

### Use a stable absolute `path` to hit the graph cache

The server keeps an LRU of recently built repo graphs (invalidated on file
mtime). First call against a repo is the expensive one (build); subsequent
calls return in milliseconds. Always pass the **same absolute repo root** in
`path` across calls within an agent turn — different relative paths or symlink
variants will miss the cache and rebuild.

### Zero matches is a signal, not a failure

`*_batch` returning `0 matches` for a symbol almost always means the symbol is
module-internal or newly introduced. Agents should report this as evidence
("symbol X is not referenced outside the changed files"), not retry with
broader queries.

### For PR review, point `path` at a post-patch worktree

If you are localizing for a PR review, the symbols you care about most are the
ones the PR *adds*. The base checkout does not contain them yet, so the graph
(and `grep`) will correctly return zero for every newly introduced name. Set
up a detached `git worktree` at the PR's head SHA and pass that path:

```bash
git fetch origin pull/<N>/head
git worktree add --detach /tmp/review-wt-<N> <head_sha>
# call repo_localizer / code_localizer_function_to_script_batch with path=/tmp/review-wt-<N>
git worktree remove --force /tmp/review-wt-<N>
```

This is harness-side work — the MCP server has no notion of PRs. Without it,
"new symbol" localization looks like a tool failure when in fact the working
tree is missing the symbol's definition.

### Frame agent prompts as **extraction** questions, not topical ones

A diff alone can answer "what does this change do". It cannot answer:

- "How many out-of-PR files reference symbol X?"
- "Which test files import this symbol?"
- "Which config files mention this name?"

Phrasing the subagent's job as those concrete extraction questions, with the
batch tools listed as the primary way to answer them, makes tool use the
shortest path to a complete response. Topical prompts ("review the impact")
let the model satisfice without ever calling a tool.

### Require a structured `tool_log` in the agent's output schema

Have your localizer agent emit JSON with an explicit `tool_log: [{tool,
args_summary, result_summary}]` field and a hard rule that *empty `tool_log`
implies empty answer arrays*. This makes tool-skipping observable to the
downstream consumer, which can then degrade confidence rather than silently
trusting fabricated counts.

### Filter self-references out of "out-of-PR impact"

When using results to anchor cross-file impact claims, drop any callsite whose
file is in the change set. The MCP returns true repo-wide references; deciding
what counts as *out-of-scope* is the agent's responsibility.

### Permission gating in the host harness

This server's tools are read-only, but most agent harnesses route every tool
call through a permission policy. If the harness is non-interactive and the
policy is `interactive`, calls will be **rejected with a message about no
approval channel** and the model will record a zero-result tool call (or skip
the tool entirely). For non-interactive runs, configure the harness to
auto-approve this MCP's tools (or all read-only tools). If you see agents
producing empty `symbol_summary` arrays despite the tools being advertised,
check the harness permission policy first — the prompt is rarely the cause.

## Production notes

- Ignore patterns and scan safety limits are configurable via environment variables.
- Parsing is best-effort and non-fatal per file; errors are captured and returned in graph metadata.
- Graph node IDs are deterministic for stable automation and comparisons.
