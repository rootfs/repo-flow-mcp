import * as fs from "node:fs";
import { runReview, type ReviewOptions } from "./review.js";

const USAGE = `mcp-review — PR review CLI backed by repo-flow-mcp

USAGE
  mcp-review <pr-spec> [options]

ARGS
  <pr-spec>    "owner/repo#NUMBER", "https://github.com/owner/repo/pull/NUMBER",
               or just "NUMBER" when run inside a github.com checkout.

OPTIONS
  --cwd <dir>              Working directory (default: \$PWD).
  --post                   Post the review as a PR comment (default: off — dry-run).
  --post-event <event>     COMMENT | APPROVE | REQUEST_CHANGES (default: inferred from verdict).
  --max-files <n>          Cap files fetched (default: 60).
  --max-file-bytes <n>     Per-file diff byte cap (default: 8192).
  --allow-fork             Allow reviewing PRs from forks.
  --json                   Emit a JSON summary on stdout instead of the markdown body.
  --no-context             Skip the localizer pass.
  --no-workspace-tool      Skip the pr_workspace MCP tool; use --cwd for the localizer's repo path.
  --model <id>             Override LLM model / Azure deployment name.
  --provider <p>           openai | azure (default: auto — azure if AZURE_*_ENDPOINT is set, else openai).
  --base-url <url>         Override OpenAI-compatible base URL (default: https://models.github.ai/inference).
  --env-file <path>        Load KEY=VALUE pairs from a dotenv-style file before reading env vars.
  -h, --help               Show this help.

ENV
  GITHUB_TOKEN             Required for GitHub API and the GitHub Models LLM endpoint.
  MCP_REVIEW_LLM_MODEL     Override default model.
  MCP_REVIEW_LLM_BASE_URL  Override default OpenAI-compatible base URL.
  MCP_REVIEW_LLM_API_KEY   Override the LLM API key (otherwise falls back to GITHUB_TOKEN).
  AZURE_OPENAI_ENDPOINT    Azure OpenAI endpoint (also: AZURE_ENDPOINT). When set, Azure is auto-selected.
  AZURE_OPENAI_DEPLOYMENT  Azure deployment name (also: AZURE_DEPLOYMENT).
  AZURE_OPENAI_API_VERSION Azure API version (also: AZURE_API_VERSION).
  AZURE_OPENAI_API_KEY     Azure API key (also: AZURE_API_KEY, API_KEY).
  MCP_REVIEW_SERVER_CMD    Override the MCP server spawn command (default: "repo-flow-mcp serve").

EXAMPLES
  mcp-review owner/repo#123
  mcp-review owner/repo#123 --post
  GITHUB_TOKEN=ghp_... mcp-review https://github.com/foo/bar/pull/42 --json
`;

function parseArgs(argv: string[]): { opts: ReviewOptions; help: boolean } {
    if (argv.length === 0 || argv.includes("-h") || argv.includes("--help")) {
        return {
            help: true,
            opts: {
                pr: "",
                cwd: process.cwd(),
                post: false,
                maxFiles: 60,
                maxFileBytes: 8192,
                allowFork: false,
                json: false,
                context: true,
                useWorkspaceTool: true,
            },
        };
    }

    let pr = "";
    let cwd = process.cwd();
    let post = false;
    let postEvent: "COMMENT" | "APPROVE" | "REQUEST_CHANGES" | undefined;
    let maxFiles = 60;
    let maxFileBytes = 8192;
    let allowFork = false;
    let json = false;
    let context = true;
    let useWorkspaceTool = true;
    let model: string | undefined;
    let baseUrl: string | undefined;
    let provider: "openai" | "azure" | undefined;
    const envFiles: string[] = [];

    for (let i = 0; i < argv.length; i++) {
        const a = argv[i];
        switch (a) {
            case "--cwd":
                cwd = argv[++i];
                break;
            case "--post":
                post = true;
                break;
            case "--post-event": {
                const v = argv[++i].toUpperCase();
                if (v !== "COMMENT" && v !== "APPROVE" && v !== "REQUEST_CHANGES") {
                    throw new Error(`--post-event must be COMMENT|APPROVE|REQUEST_CHANGES (got "${v}")`);
                }
                postEvent = v;
                break;
            }
            case "--max-files":
                maxFiles = Number(argv[++i]);
                break;
            case "--max-file-bytes":
                maxFileBytes = Number(argv[++i]);
                break;
            case "--allow-fork":
                allowFork = true;
                break;
            case "--json":
                json = true;
                break;
            case "--no-context":
                context = false;
                break;
            case "--no-workspace-tool":
                useWorkspaceTool = false;
                break;
            case "--model":
                model = argv[++i];
                break;
            case "--provider": {
                const v = argv[++i].toLowerCase();
                if (v !== "openai" && v !== "azure") {
                    throw new Error(`--provider must be openai|azure (got "${v}")`);
                }
                provider = v;
                break;
            }
            case "--base-url":
                baseUrl = argv[++i];
                break;
            case "--env-file":
                envFiles.push(argv[++i]);
                break;
            default:
                if (a.startsWith("--")) throw new Error(`unknown flag: ${a}`);
                if (pr) throw new Error(`unexpected extra arg: ${a}`);
                pr = a;
        }
    }
    if (!pr) throw new Error("missing <pr-spec>; pass `--help` for usage");

    const opts: ReviewOptions = {
        pr,
        cwd,
        post,
        maxFiles,
        maxFileBytes,
        allowFork,
        json,
        context,
        useWorkspaceTool,
    };
    if (postEvent) opts.postEvent = postEvent;
    for (const f of envFiles) loadEnvFile(f);
    const llmOverrides: { model?: string; baseUrl?: string; provider?: "openai" | "azure" } = {};
    if (model) llmOverrides.model = model;
    if (baseUrl) llmOverrides.baseUrl = baseUrl;
    if (provider) llmOverrides.provider = provider;
    if (Object.keys(llmOverrides).length) opts.llm = llmOverrides;

    return { opts, help: false };
}

/** Minimal dotenv parser — supports KEY=VALUE with optional surrounding quotes and # comments. */
function loadEnvFile(path: string): void {
    const expanded = path.replace(/^~(?=\/|$)/, process.env.HOME ?? "~");
    const raw = fs.readFileSync(expanded, "utf8");
    for (const line of raw.split(/\r?\n/)) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith("#")) continue;
        const eq = trimmed.indexOf("=");
        if (eq <= 0) continue;
        const key = trimmed.slice(0, eq).trim();
        let val = trimmed.slice(eq + 1).trim();
        if ((val.startsWith("\"") && val.endsWith("\"")) || (val.startsWith("'") && val.endsWith("'"))) {
            val = val.slice(1, -1);
        }
        if (!(key in process.env)) process.env[key] = val;
    }
}

export async function main(argv: string[]): Promise<void> {
    let parsed;
    try {
        parsed = parseArgs(argv);
    } catch (e) {
        process.stderr.write(`error: ${(e as Error).message}\n\n${USAGE}`);
        process.exit(2);
    }
    if (parsed.help) {
        process.stdout.write(USAGE);
        return;
    }
    try {
        await runReview(parsed.opts);
    } catch (e) {
        process.stderr.write(`fatal: ${(e as Error).stack ?? (e as Error).message}\n`);
        process.exit(1);
    }
}
