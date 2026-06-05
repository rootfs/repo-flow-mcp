import {
    fetchIssueComments,
    fetchPullFiles,
    fetchPullRequest,
    getGithubToken,
    makeOctokit,
    postReview,
    resolveCoord,
    type InlineComment,
    type IssueComment,
    type PullFileEntry,
    type PullRequestSummary,
} from "./github.js";
import { defaultServerConfig, McpSession } from "./mcp.js";
import { chatPlain, resolveLlmConfig, type LlmConfig } from "./llm.js";
import {
    annotatePatch,
    buildHunkIndex,
    eventFromVerdict,
    inferVerdict,
    splitInlineBlock,
    validateInline,
} from "./patch.js";
import { gatherRepoContext, type LocalizerEvidence } from "./localizer.js";

const DEFAULT_MAX_FILES = 60;
const DEFAULT_MAX_FILE_BYTES = 8192;
const DEFAULT_TOTAL_BUDGET = 150_000;

export interface ReviewOptions {
    pr: string;
    cwd: string;
    post: boolean;
    postEvent?: "COMMENT" | "APPROVE" | "REQUEST_CHANGES";
    maxFiles: number;
    maxFileBytes: number;
    allowFork: boolean;
    json: boolean;
    /** Run the localizer subagent. Default true. */
    context: boolean;
    /** Use the `pr_workspace` MCP tool to materialize a base+overlay tree. Default true. */
    useWorkspaceTool: boolean;
    /** Override LLM config. */
    llm?: Partial<LlmConfig>;
    log?: (line: string) => void;
}

export interface ReviewResult {
    coord: { owner: string; repo: string; number: number };
    verdict: "approve" | "comment" | "request_changes";
    bodyMarkdown: string;
    inline: InlineComment[];
    mode: "full" | "trunc" | "batched";
    dropped: number;
    posted?: { id: number; htmlUrl: string };
}

export async function runReview(opts: ReviewOptions): Promise<ReviewResult> {
    const log = opts.log ?? ((line: string) => process.stderr.write(`[review] ${line}\n`));
    const coord = resolveCoord(opts.pr, opts.cwd);
    const ghToken = getGithubToken();
    const octokit = makeOctokit(ghToken);
    const pr = await fetchPullRequest(octokit, coord);
    log(
        `pr=${coord.owner}/${coord.repo}#${coord.number} state=${pr.state} files=${pr.changedFiles} +${pr.additions}/-${pr.deletions}`,
    );

    if (pr.isFork && !opts.allowFork) {
        throw new Error(
            `PR is from a fork (${pr.headRepoFullName}); pass --allow-fork to review it`,
        );
    }

    const files = await fetchPullFiles(octokit, coord, opts.maxFiles);
    log(`fetched-files=${files.length} (cap=${opts.maxFiles})`);

    // Open the MCP session once; both `pr_workspace` and the localizer
    // tool-use loop share it.
    const mcp = new McpSession(defaultServerConfig());
    await mcp.connect();
    const mcpTools = await mcp.listTools();
    log(`mcp-tools=${mcpTools.map((t) => t.name).join(",")}`);

    try {
        // Materialize the head tree on-disk via the cache so we can hand it
        // to the localizer. Skipped if --no-workspace-tool was passed; in
        // that case we reuse the caller's cwd.
        let worktree = opts.cwd;
        if (opts.useWorkspaceTool) {
            try {
                const diff = await fetchPullDiffSafe(octokit, coord, log);
                const t0 = Date.now();
                const r = await mcp.callTool("pr_workspace", {
                    repo_url: `https://github.com/${coord.owner}/${coord.repo}.git`,
                    base_sha: pr.baseSha,
                    diff_text: diff,
                });
                if (r.isError) throw new Error(r.text.slice(0, 200));
                const parsed = JSON.parse(r.text) as { worktree_path?: string };
                if (typeof parsed.worktree_path === "string") {
                    worktree = parsed.worktree_path;
                    log(`worktree=${worktree} (${Date.now() - t0}ms)`);
                } else {
                    log(`pr_workspace returned no worktree_path; using cwd`);
                }
            } catch (e) {
                log(`pr_workspace failed (${(e as Error).message}); using cwd`);
            }
        }

        // PR conversation (excluding bots) gives the reviewer context.
        let prComments: IssueComment[] = [];
        try {
            const raw = await fetchIssueComments(octokit, coord, 30);
            prComments = raw.filter((c) => !c.author.endsWith("[bot]"));
            if (prComments.length) log(`comments=${prComments.length}`);
        } catch (e) {
            log(`comments fetch failed: ${(e as Error).message}`);
        }

        // Localizer pass (LLM-driven, MCP tool-use loop).
        let repoContext = "";
        let evidence: LocalizerEvidence | undefined;
        if (opts.context) {
            try {
                const llmCfg = resolveLlmConfig({ apiKey: ghToken, ...(opts.llm ?? {}) });
                const r = await gatherRepoContext({
                    mcp,
                    mcpTools,
                    llm: llmCfg,
                    cwd: worktree,
                    files,
                    log: (l: string) => log(`[localizer] ${l}`),
                });
                repoContext = r.markdown;
                evidence = r.evidence;
                log(`repo-context bytes=${repoContext.length}`);
            } catch (e) {
                log(`repo-context skipped: ${(e as Error).message}`);
            }
        }

        // Build the review prompt(s).
        const { batches, mode } = buildReviewBatches(
            pr,
            files,
            opts.maxFileBytes,
            DEFAULT_TOTAL_BUDGET,
            repoContext,
            prComments,
            evidence,
        );
        log(
            `mode=${mode} batches=${batches.length} prompt-bytes=${batches.reduce((n, b) => n + b.length, 0)}`,
        );

        const llmCfg = resolveLlmConfig({ apiKey: ghToken, ...(opts.llm ?? {}) });
        const reviewerSystem =
            "You are a senior code reviewer. Be concrete, anchor every claim on a path:line, and never invent facts. " +
            "When the localizer evidence is empty, say so explicitly and lower confidence on cross-file claims. " +
            "Always treat verified out-of-PR call-sites, tests, configs, and docs as the primary signal for what else may need to be updated alongside this PR.";

        // First turn: body + verdict (multi-batch concatenated for the LLM
        // call — this CLI does not maintain a stateful session, so we
        // simply concatenate the batches in one prompt. If the total is
        // too big the build step already truncates.)
        const combined = batches.join("\n\n---\n\n");
        const bodyRaw = await chatPlain(llmCfg, reviewerSystem, combined);
        const verdict = inferVerdict(bodyRaw);
        const { body: bodyOnly } = splitInlineBlock(bodyRaw);
        log(`verdict=${verdict} body-bytes=${bodyOnly.length}`);

        // Second turn: inline comments only.
        const inlinePrompt = [
            "Below is the PR diff you just reviewed. Now produce ONLY a fenced code block tagged `inline-comments` with a JSON array of line-anchored comments.",
            "",
            "Schema:",
            "```inline-comments",
            "[",
            '  {"path": "relative/file.ts", "line": 42, "side": "RIGHT", "body": "[risk:high|medium|low] short, actionable note"}',
            "]",
            "```",
            "",
            "Comment selection (confidence × risk):",
            "- Always: high-confidence + high-risk (bugs, data loss, security, correctness).",
            "- Confident: medium-risk items you are ≥80% sure about (missing error handling, API misuse, resource leaks).",
            "- Suggestion: lower-risk but high-confidence (naming, dead code, missing public-API docs, test gaps).",
            "- Skip: low-confidence hunches, pure style nitpicks, formatting, import order.",
            "",
            "Hard rules:",
            "- Output ONLY the fenced block.",
            "- Each diff line is tagged `[N###]` (RIGHT) or `[O###]` (LEFT). Copy `line` and `side` from those tags — do NOT count from `@@` headers.",
            "- Anchor on the most specific line (the one containing the symbol your comment names).",
            "- If nothing meets the bar, emit `[]`.",
            "",
            "---",
            combined,
        ].join("\n");
        const inlineRaw = await chatPlain(llmCfg, reviewerSystem, inlinePrompt);
        const { inline: rawInline } = splitInlineBlock(inlineRaw);
        const hunkIndex = buildHunkIndex(files);
        const { kept: inline, dropped } = validateInline(rawInline, hunkIndex);
        log(`inline=${inline.length} dropped=${dropped}`);

        let posted: { id: number; htmlUrl: string } | undefined;
        if (opts.post) {
            const event = opts.postEvent ?? eventFromVerdict(verdict);
            posted = await postReview(octokit, coord, bodyOnly, event, inline);
            log(
                `posted event=${event} inline=${inline.length} url=${posted.htmlUrl}`,
            );
        }

        const result: ReviewResult = {
            coord: { owner: coord.owner, repo: coord.repo, number: coord.number },
            verdict,
            bodyMarkdown: bodyOnly,
            inline,
            mode,
            dropped,
        };
        if (posted) result.posted = posted;

        if (opts.json) {
            process.stdout.write(
                JSON.stringify(
                    {
                        ...result,
                        pr: { title: pr.title, state: pr.state },
                    },
                    null,
                    2,
                ) + "\n",
            );
        } else {
            process.stdout.write(bodyOnly + "\n");
            if (inline.length) {
                process.stdout.write(`\n--- inline (${inline.length}) ---\n`);
                for (const c of inline) {
                    process.stdout.write(
                        `${c.path}:${c.line} (${c.side})\n  ${c.body.split("\n").join("\n  ")}\n`,
                    );
                }
            }
        }

        return result;
    } finally {
        await mcp.close();
    }
}

// ---------------------------------------------------------------------------
// Batch builder (port of TUI's buildReviewBatches, minus the batched-session
// logic — this CLI sends a single combined prompt)
// ---------------------------------------------------------------------------

function buildReviewBatches(
    pr: PullRequestSummary,
    files: PullFileEntry[],
    maxFileBytes: number,
    totalBudget: number,
    repoContext: string,
    comments: IssueComment[] = [],
    evidence?: LocalizerEvidence,
): { batches: string[]; mode: "full" | "trunc" | "batched" } {
    const MAX_COMMENT_BYTES = 2048;
    const commentSection = comments.length
        ? [
              "## Conversation",
              "_(PR comments from humans, excluding bots.)_",
              "",
              ...comments.map((c) => {
                  const body =
                      c.body.length > MAX_COMMENT_BYTES
                          ? c.body.slice(0, MAX_COMMENT_BYTES) + "\n...[truncated]"
                          : c.body;
                  return `### ${c.author} (${c.createdAt})\n${body.trim() || "(empty)"}\n`;
              }),
              "",
          ]
        : [];

    const header = [
        `# PR ${pr.number}: ${pr.title}`,
        `author=${pr.author} state=${pr.state}${pr.isDraft ? " (draft)" : ""}`,
        `base=${pr.baseRef}@${pr.baseSha.slice(0, 8)}  head=${pr.headRef}@${pr.headSha.slice(0, 8)}`,
        `changed=${pr.changedFiles}  additions=${pr.additions}  deletions=${pr.deletions}`,
        "",
        "## Description",
        pr.body.trim() || "(no description)",
        "",
        ...commentSection,
        ...(repoContext
            ? [
                  "## Repo context",
                  "_(structured evidence from the localizer. Empty fields = the localizer found nothing OR did not look. Branch your review on the actual counts; do not invent cross-file claims.)_",
                  "",
                  repoContext,
                  "",
              ]
            : []),
        "## Files & diff",
    ].join("\n");

    const evToolCalls = evidence?.tool_log.length ?? 0;
    const evCallsites = evidence?.verified_callsites.length ?? 0;
    const evTests = evidence?.verified_tests.length ?? 0;
    const evidenceWasUseful =
        evToolCalls > 0 &&
        (evCallsites > 0 || evTests > 0 || (evidence?.symbol_summary.length ?? 0) > 0);

    const evidenceBranch = evidence
        ? evidenceWasUseful
            ? [
                  `The localizer issued ${evToolCalls} tool call(s) and surfaced ${evCallsites} out-of-PR call-sites and ${evTests} related tests. Anchor your Risk and Tests sections on those concrete entries.`,
              ]
            : evToolCalls > 0
              ? [
                    `The localizer issued ${evToolCalls} tool call(s) but found no out-of-PR call-sites or tests. This is a real signal — the changed symbols are likely internal. Say so in Risk.`,
                ]
              : [
                    "**The localizer made ZERO tool calls** — the Repo context section is unverified. Mark Risk and Tests as LOW CONFIDENCE for any out-of-PR impact.",
                ]
        : [];

    const finalInstructions = [
        "",
        "---",
        "Reviewing this PR means evaluating the diff's IMPACT ON THE WHOLE REPO, not the diff alone.",
        "",
        ...evidenceBranch,
        "",
        "Now produce a code review covering ALL files shown, with these sections in order:",
        "1. **Verdict** — one of `approve`, `comment`, `request_changes` on its own line, then a one-sentence rationale.",
        "2. **Risk** — breaking changes, API removals, schema/migration concerns. Anchor each on a `path:line`. If a risk is not anchored, drop it.",
        "3. **Tests** — coverage of touched paths, missing tests, related tests in the repo that should be updated.",
        "4. **Out-of-PR follow-ups** — concrete files and `path:line` locations OUTSIDE this PR that should also be updated (or at minimum re-checked) for the change to land safely. Pull these directly from the Repo context block: `verified_callsites` (callers that depend on the changed contract), `verified_tests` (tests that exercise the touched code), `verified_configs` (config or schema files that mention the touched names), `verified_docs` (docs/READMEs that describe the touched behavior). Group bullets under those four labels and write `(none from localizer evidence)` under any label whose list is empty. Do NOT invent paths — if the localizer surfaced nothing, say so and explain why this PR is therefore self-contained.",
        "5. **Style/Hygiene** — readability, dead code, naming. Brief.",
        "6. **Size** — is the scope appropriate; suggest splits when warranted.",
        "Be concrete. Cite specific lines as `path:line`. A follow-up turn collects inline comments separately.",
    ].join("\n");

    function renderFileChunk(f: PullFileEntry): { chunk: string; truncated: boolean } {
        const head = `\n### ${f.filename} (${f.status} +${f.additions}/-${f.deletions})\n`;
        const patch = f.patch ?? "";
        if (!patch) return { chunk: head + "(no patch — binary or too large)\n", truncated: false };
        const annotated = annotatePatch(patch);
        if (annotated.length > maxFileBytes) {
            return {
                chunk: head + "```diff\n" + annotated.slice(0, maxFileBytes) + "\n... [truncated]\n```\n",
                truncated: true,
            };
        }
        return { chunk: head + "```diff\n" + annotated + "\n```\n", truncated: false };
    }

    // Pack files into batches.
    const reserve = header.length + finalInstructions.length + 1024;
    const batchSizeBudget = Math.max(8_000, totalBudget - reserve);
    const groups: PullFileEntry[][] = [[]];
    let cur = 0;
    let anyTrunc = false;
    for (const f of files) {
        const { chunk, truncated } = renderFileChunk(f);
        if (truncated) anyTrunc = true;
        const chunkLen = Math.min(chunk.length, batchSizeBudget);
        if (cur + chunkLen > batchSizeBudget && groups[groups.length - 1].length > 0) {
            groups.push([]);
            cur = 0;
        }
        groups[groups.length - 1].push(f);
        cur += chunkLen;
    }

    const batches: string[] = [];
    for (let i = 0; i < groups.length; i++) {
        const isFirst = i === 0;
        const isLast = i === groups.length - 1;
        let body = "";
        for (const f of groups[i]) {
            const { chunk } = renderFileChunk(f);
            body +=
                chunk.length > batchSizeBudget
                    ? chunk.slice(0, batchSizeBudget) + "\n... [truncated]\n```\n"
                    : chunk;
        }
        const intro = isFirst
            ? header
            : `# PR ${pr.number}: continuation batch ${i + 1}/${groups.length}\n\n## Files & diff (continued)`;
        const footer = isLast ? finalInstructions : "\n";
        batches.push(intro + body + footer);
    }

    const mode: "full" | "trunc" | "batched" =
        groups.length > 1 ? "batched" : anyTrunc ? "trunc" : "full";
    return { batches, mode };
}

async function fetchPullDiffSafe(
    octokit: ReturnType<typeof makeOctokit>,
    coord: { owner: string; repo: string; number: number },
    log: (l: string) => void,
): Promise<string> {
    try {
        const res = await octokit.request("GET /repos/{owner}/{repo}/pulls/{pull_number}", {
            owner: coord.owner,
            repo: coord.repo,
            pull_number: coord.number,
            mediaType: { format: "diff" },
        });
        return String(res.data ?? "");
    } catch (e) {
        log(`pull diff fetch failed: ${(e as Error).message}`);
        return "";
    }
}
