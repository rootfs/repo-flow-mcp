# repo-flow-mcp-client

Standalone TypeScript CLI that drives [repo-flow-mcp](../) over MCP stdio and
produces a code review for a GitHub PR. Designed to also run as a GitHub
Action — no editor, no TUI, no vendored copilot SDK.

## What it does

1. Resolves the PR via the GitHub API (`@octokit/rest`).
2. Spawns `repo-flow-mcp` over stdio and lists its tools.
3. Calls `pr_workspace` to materialize the head tree on disk (uses the
   layered cache, so a re-run for the same `base_sha` is nearly free).
4. Runs a **localizer** LLM pass with the MCP tools exposed as
   OpenAI-style function calls. Produces a structured `verified_*` JSON
   blob: out-of-PR call-sites, related tests, config references, doc
   cross-references.
5. Runs a **reviewer** LLM pass over the diff + localizer evidence.
   Produces a verdict (`approve` / `comment` / `request_changes`) plus a
   markdown body.
6. Runs a follow-up pass to extract validated inline comments anchored on
   diff lines.
7. Optionally posts the review via `pulls.createReview`.

## Install

```bash
cd mcp-client
npm install
npm run build
```

You also need `repo-flow-mcp` installed and on `PATH` (or set
`MCP_REVIEW_SERVER_CMD` to a custom command, e.g.
`uv run repo-flow-mcp`).

## Usage

```bash
# dry-run (writes the review to stdout, doesn't post)
GITHUB_TOKEN=ghp_xxx ./bin/mcp-review.mjs owner/repo#123

# post the review back to the PR
GITHUB_TOKEN=ghp_xxx ./bin/mcp-review.mjs owner/repo#123 --post

# emit a JSON envelope for downstream tooling
GITHUB_TOKEN=ghp_xxx ./bin/mcp-review.mjs owner/repo#123 --json
```

Run `./bin/mcp-review.mjs --help` for the full flag list.

## LLM endpoint

Defaults to [GitHub Models](https://docs.github.com/en/github-models)
(`https://models.github.ai/inference`) authenticated via `GITHUB_TOKEN`
— this is the GHA-friendly default. Override with any
OpenAI-compatible endpoint:

```bash
MCP_REVIEW_LLM_BASE_URL=https://api.openai.com/v1 \
MCP_REVIEW_LLM_API_KEY=sk-... \
MCP_REVIEW_LLM_MODEL=gpt-4o \
./bin/mcp-review.mjs owner/repo#123
```

Also works with vLLM, Ollama (`http://localhost:11434/v1`), Azure OpenAI,
or any service that speaks the OpenAI chat-completions + function-calling
protocol.

## GitHub Action (planned)

A wrapper action will install `repo-flow-mcp` via pip, build this CLI,
and invoke it on `pull_request` events, passing `GITHUB_TOKEN` (with
`pull-requests: write` and `models: read`).
