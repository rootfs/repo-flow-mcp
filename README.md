# repo-flow-mcp

`repo-flow-mcp` is a production-oriented Model Context Protocol (MCP) server that builds a unified graph across:

- code-level entities and dependencies (imports, calls)
- script-level orchestration (shell, Makefile, workflow steps)
- operational dependency flow (artifacts, env contracts, stage handoffs)
- documentation prose (markdown / mdx / rst / txt), section-aware and BM25-ranked

## Why this exists

Most code graph tools cover symbols and imports well, but operational repos also depend on script chains, CI jobs, build targets, and artifact lineage. This server combines those layers into one queryable graph for AI agents and humans, plus a separate full-text doc index for prose-only review questions.

## Features

- Unified graph model with typed nodes and edges.
- Parsers for:
  - Python code (`ast` imports/calls)
  - JS/TS imports plus symbol/call extraction (regex-based)
  - Go, Rust, Java, and C/C++ dependency imports/includes (regex-based)
  - Markdown dependency parsing for skills/docs links and tool references
  - shell scripts (`.sh`)
  - Makefiles
  - GitHub Actions workflows (workflows / jobs / steps as first-class nodes)
  - GitLab CI (`.gitlab-ci.yml`)
  - Jenkinsfile pipeline steps
  - CMake and Bazel build files
  - Dockerfile and docker-compose
- Per-repo in-memory SQLite FTS5 symbol index (multi-kind: code symbols, modules, scripts, Make targets, workflows / jobs / steps).
- Per-repo in-memory SQLite FTS5 doc index (porter-stemmed prose, one row per heading-delimited section).
- Filesystem watcher (watchdog) that flips the cached graph + indexes to dirty on any non-ignored change, so the next call rebuilds without per-call mtime walks.
- Defensive parsing: per-file errors are captured and returned in graph metadata rather than aborting the build.

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
mypy src
```

## MCP tool surface

Three consolidated tools, each with a discriminator flag. The flag picks the operation; only the arguments named for that op are required (the server raises a clear `ValueError` listing the missing argument when one is omitted).

### `repo_localizer(path, view, ...)`

Repo-wide views.

- `view="overview"` (default): subsystem layout, node-kind counts, top files ranked by graph fan-in/out. Call FIRST when scoping a PR or exploring an unfamiliar repo. Knobs: `top_k` (1–200, default 25), `include_hidden`.
- `view="entrypoints"`: CLI mains, server boots, Make targets, shell scripts, and CI/workflow jobs in one call. Knob: `limit` (1–500, default 50).

Replaces `bash ls` + reading README + wide grep + `find . -name Makefile` + ad-hoc `view` of `.github/workflows/*` for orientation.

### `code_localizer(path, op, ...)`

Graph queries.

- `op="trace"` (default): trace function/method names OR runner labels (Make targets, scripts, GHA workflow / job / step) to their connected runners. Pass `queries=[...]` (one element behaves like a singular call). Each match has a `match_kind`:
  - `code_symbol_chain`: `{ node, function, script_source, invoked_script, bridge, command }`
  - `ci_runner` / `build_target` / `script` / `module`: `{ node, incoming[], outgoing[] }` with edge rows `{ id, label, kind, path, edge_kind, command? }`
  Knobs: `limit_per_query` (1–50, default 5), `max_total_matches` (1–500, default 60). Truncation reported via `truncated: bool` and `total_matches: int`.
  Scope: defined symbols and runner labels are indexed; struct fields, top-level const/var, and type aliases are not — fall back to `grep` for those.
- `op="context"`: 1+ node ids → upstream/downstream neighborhood. Pass `node_ids=[...]` and optional `depth` (1–5, default 2). Use AFTER another op surfaced ids worth expanding — never as a first call.
- `op="path"`: shortest dependency/execution path between two known node ids. Pass `start_node_id` and `end_node_id`.
- `op="artifact"`: trace producers/consumers for a build artifact label (image tag, binary name, generated path). Pass `artifact`.
- `op="contracts"`: detect producer/consumer mismatches across the whole graph (consumed but not produced, or produced but not consumed). No extra args.

### `doc_localizer(path, op, ...)`

Doc-prose queries against markdown / mdx / rst / txt (one row per heading-delimited section, BM25 ranked, porter-stemmed so 'calibrate' matches 'calibration').

- `op="search"` (default): full-text search. Pass `query` (free text or single term — same op handles both "which sibling pages discuss this concept" and "where else is term X used"). Optional `path_glob` (SQLite GLOB, e.g. `'docs/*.md'`) scopes results. Returns `{ path, section, start_line, snippet, score }` per hit (lower score = better). For literal-token lookups (exact paths, exact identifier names) prefer plain `grep`.
- `op="resolve_link"`: verify a relative doc link resolves to a real file. Pass `source_file` (the doc containing the link) and `target` (the link as written, e.g. `./foo` or `../bar.md`; trailing `#anchors` are stripped). Catches broken `[text](path)` references that markdownlint won't.

## Effective use for agent integrations

These notes come from real review/localization agents driving this server. They matter because the host harness — not the server — usually decides whether tool calls actually fire.

### One tool call per intent — pass lists, not loops

For a PR with N changed symbols, call `code_localizer(op="trace", queries=[...all symbols...])` **once** rather than N times. The graph build is reused inside the call, so latency stays close to a single call. Same idea for `op="context"` with `node_ids=[...]`.

### Use a stable absolute `path` to hit the graph cache

The server keeps an LRU of recently built repo graphs. The first call against a repo is the expensive one (build); subsequent calls return in milliseconds. The filesystem watcher flips the cache to dirty as soon as any non-ignored file under the repo changes, so the next call rebuilds — there is no per-call mtime walk. Each cached graph also pairs with the FTS5 symbol index (and the doc index, when `doc_localizer` is used), so queries hit an inverted index rather than scanning every symbol or every section.

Always pass the **same absolute repo root** across calls within an agent turn — different relative paths or symlink variants will miss the cache and rebuild.

### Zero matches is a signal, not a failure

`op="trace"` returning 0 matches for a query almost always means the symbol is module-internal or newly introduced. Agents should report this as evidence ("symbol X is not referenced outside the changed files"), not retry with broader queries.

### For PR review, point `path` at a post-patch worktree

If you are localizing for a PR review, the symbols you care about most are the ones the PR *adds*. The base checkout does not contain them yet, so the graph (and `grep`) will correctly return zero for every newly introduced name. Set up a detached `git worktree` at the PR's head SHA and pass that path:

```bash
git fetch origin pull/<N>/head
git worktree add --detach /tmp/review-wt-<N> <head_sha>
# call code_localizer(path=/tmp/review-wt-<N>, op="trace", queries=[...])
git worktree remove --force /tmp/review-wt-<N>
```

This is harness-side work — the MCP server has no notion of PRs. Without it, "new symbol" localization looks like a tool failure when in fact the working tree is missing the symbol's definition.

### Frame agent prompts as **extraction** questions, not topical ones

A diff alone can answer "what does this change do". It cannot answer:

- "How many out-of-PR files reference symbol X?"
- "Which test files import this symbol?"
- "Which config files mention this name?"
- "Which existing doc sections discuss this concept?"

Phrasing the subagent's job as concrete extraction questions, with the consolidated tools listed as the primary way to answer them, makes tool use the shortest path to a complete response. Topical prompts ("review the impact") let the model satisfice without ever calling a tool.

### Require a structured `tool_log` in the agent's output schema

Have your localizer agent emit JSON with an explicit `tool_log: [{tool, args_summary, result_summary}]` field and a hard rule that *empty `tool_log` implies empty answer arrays*. This makes tool-skipping observable to the downstream consumer, which can then degrade confidence rather than silently trusting fabricated counts.

### Filter self-references out of "out-of-PR impact"

When using results to anchor cross-file impact claims, drop any callsite whose file is in the change set. The MCP returns true repo-wide references; deciding what counts as *out-of-scope* is the agent's responsibility.

### Permission gating in the host harness

This server's tools are read-only, but most agent harnesses route every tool call through a permission policy. If the harness is non-interactive and the policy is `interactive`, calls will be **rejected with a message about no approval channel** and the model will record a zero-result tool call (or skip the tool entirely). For non-interactive runs, configure the harness to auto-approve this MCP's tools (or all read-only tools). If you see agents producing empty result arrays despite the tools being advertised, check the harness permission policy first — the prompt is rarely the cause.

## Production notes

- Ignore patterns and scan safety limits are configurable via environment variables.
- Parsing is best-effort and non-fatal per file; errors are captured and returned in graph metadata.
- Graph node IDs are deterministic for stable automation and comparisons.
