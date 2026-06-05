# repo-flow-mcp-client (`mcp-review`)

Standalone TypeScript CLI that drives [repo-flow-mcp](../) over MCP stdio
and produces a structured GitHub PR review. Provider-agnostic: ships with
first-class support for GitHub Models, raw OpenAI, Azure OpenAI, and any
other OpenAI-compatible endpoint (vLLM, Ollama, llama.cpp, LiteLLM, ...).

Designed to also run as a GitHub Action — no editor, no TUI, no
vendored copilot SDK.

## What it does

1. Resolves the PR via the GitHub API (`@octokit/rest`).
2. Spawns `repo-flow-mcp serve` over stdio and lists its tools.
3. Calls `pr_workspace` to materialize the head tree on disk (uses the
   layered cache, so a re-run for the same `base_sha` hits in <10 ms).
4. Runs a **localizer** LLM pass with the MCP tools exposed as
   tool calls via the [Vercel AI SDK](https://sdk.vercel.ai). Produces a
   structured `verified_*` JSON blob: out-of-PR call-sites, related
   tests, config references, doc cross-references.
5. Runs a **reviewer** LLM pass over the annotated diff + localizer
   evidence. Produces a verdict (`approve` / `comment` /
   `request_changes`) plus a markdown body.
6. Runs a follow-up pass that emits validated inline comments anchored
   to diff lines (hunk-validated; out-of-hunk comments are dropped).
7. Optionally posts the review via `pulls.createReview` (`--post`).

## Install

```bash
cd mcp-client
npm install
npm run build
```

You also need `repo-flow-mcp` installed and on `PATH`, or set
`MCP_REVIEW_SERVER_CMD` to a custom command, e.g.
`uv run repo-flow-mcp serve`.

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

## LLM provider configuration

The CLI is built on the Vercel AI SDK and picks a provider in this order:

| Trigger | Provider |
|---------|----------|
| `--provider azure` or `AZURE_OPENAI_ENDPOINT` (or `AZURE_ENDPOINT`) is set | Azure OpenAI |
| Otherwise | OpenAI-compat (default: GitHub Models) |

### GitHub Models (default)

Defaults to `https://models.github.ai/inference` with model
`openai/gpt-4o` authenticated via `GITHUB_TOKEN` — works out of the
box in a GitHub Action.

### Azure OpenAI

Put your credentials in a dotenv-style file (or export them directly):

```bash
# ~/.env
AZURE_ENDPOINT=https://my-resource.openai.azure.com
AZURE_DEPLOYMENT=gpt-4o-mini
AZURE_API_VERSION=2024-10-21
API_KEY=...
```

then:

```bash
./bin/mcp-review.mjs owner/repo#123 --env-file ~/.env
```

Azure is auto-selected as soon as `AZURE_ENDPOINT` (or
`AZURE_OPENAI_ENDPOINT`) is present. The CLI also normalizes MCP tool
schemas to Azure's strict mode (`additionalProperties: false`, all
properties forced into `required`).

### Generic OpenAI-compatible (vLLM, Ollama, LiteLLM, raw OpenAI)

```bash
MCP_REVIEW_LLM_BASE_URL=https://api.openai.com/v1 \
MCP_REVIEW_LLM_API_KEY=sk-... \
MCP_REVIEW_LLM_MODEL=gpt-4o \
./bin/mcp-review.mjs owner/repo#123
```

Or with Ollama:

```bash
MCP_REVIEW_LLM_BASE_URL=http://localhost:11434/v1 \
MCP_REVIEW_LLM_MODEL=llama3.1:70b \
MCP_REVIEW_LLM_API_KEY=ollama \
./bin/mcp-review.mjs owner/repo#123
```

## Flags

| Flag | Default | Description |
|------|---------|-------------|
| `<pr-spec>` | (required) | `owner/repo#N`, full PR URL, or bare `N` inside a git checkout |
| `--cwd <dir>` | `$PWD` | Working directory (used as the localizer's repo path when `--no-workspace-tool`) |
| `--post` | off | Publish the review (default is dry-run to stdout) |
| `--post-event <event>` | inferred | `COMMENT` / `APPROVE` / `REQUEST_CHANGES` |
| `--max-files <n>` | 60 | Cap files fetched |
| `--max-file-bytes <n>` | 8192 | Per-file diff byte cap |
| `--allow-fork` | off | Allow reviewing PRs from forks |
| `--json` | off | Emit a JSON summary on stdout |
| `--no-context` | off | Skip the localizer pass |
| `--no-workspace-tool` | off | Skip `pr_workspace`; use `--cwd` instead |
| `--provider <p>` | auto | `openai` \| `azure` |
| `--model <id>` | provider-specific | Model id (or Azure deployment name) |
| `--base-url <url>` | provider-specific | OpenAI-compatible base URL |
| `--env-file <path>` | — | Load `KEY=VALUE` pairs (supports `~`) before reading env vars |

## Environment variables

| Variable | Purpose |
|----------|---------|
| `GITHUB_TOKEN` | Required for the GitHub API; also the default LLM key for GitHub Models |
| `MCP_REVIEW_SERVER_CMD` | MCP server spawn command (default: `repo-flow-mcp serve`) |
| `MCP_REVIEW_LLM_MODEL` | Override default model |
| `MCP_REVIEW_LLM_BASE_URL` | Override OpenAI-compatible base URL |
| `MCP_REVIEW_LLM_API_KEY` | Override LLM API key |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_ENDPOINT` | Azure endpoint (presence auto-selects Azure) |
| `AZURE_OPENAI_DEPLOYMENT` / `AZURE_DEPLOYMENT` | Azure deployment name |
| `AZURE_OPENAI_API_VERSION` / `AZURE_API_VERSION` | Azure API version (default: `2024-10-21`) |
| `AZURE_OPENAI_API_KEY` / `AZURE_API_KEY` / `API_KEY` | Azure API key |

## Architecture

- `src/cli.ts` — argv parser + dotenv loader.
- `src/review.ts` — orchestrator: PR fetch, MCP session, `pr_workspace`,
  localizer, reviewer, inline-comments, optional post.
- `src/localizer.ts` — repo-context evidence pass.
- `src/llm.ts` — Vercel AI SDK wrapper + Azure schema strictification.
- `src/mcp.ts` — MCP stdio client (`@modelcontextprotocol/sdk`).
- `src/github.ts` — GitHub API helpers.
- `src/patch.ts` — diff annotation, symbol extraction, hunk-anchored
  inline-comment validation.

## Validated runs

| PR | Provider | Model | Result |
|----|----------|-------|--------|
| [vllm-project/semantic-router#2058](https://github.com/vllm-project/semantic-router/pull/2058) | GitHub Models | `openai/gpt-4o-mini` | dry-run, full body + verdict |
| [vllm-project/semantic-router#2061](https://github.com/vllm-project/semantic-router/pull/2061) | Azure OpenAI | `gpt-5.4-mini` | posted: [review #4438331289](https://github.com/vllm-project/semantic-router/pull/2061#pullrequestreview-4438331289) |

## GitHub Action (planned)

A wrapper action will install `repo-flow-mcp` via `pip`, build this CLI,
and invoke it on `pull_request` events with `GITHUB_TOKEN`
(`pull-requests: write` and `models: read`). For Azure-backed reviews,
the action will read provider credentials from repo/org secrets.
