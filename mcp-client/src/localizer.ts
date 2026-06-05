import type { McpSession, McpToolDef } from "./mcp.js";
import type { LlmConfig } from "./llm.js";
import { chatWithTools, type ToolCallRecord } from "./llm.js";
import type { PullFileEntry } from "./github.js";
import { extractChangedSymbols } from "./patch.js";

export interface LocalizerEvidence {
    verified_callsites: {
        symbol: string;
        file: string;
        line: number;
        snippet?: string;
    }[];
    verified_tests: { symbol: string; file: string; line: number }[];
    verified_configs: { symbol: string; file: string; line: number }[];
    verified_docs: {
        source: string;
        target_path: string;
        target_section?: string;
        line?: number;
        kind: "related" | "anchor" | "term";
        snippet?: string;
    }[];
    symbol_summary: {
        symbol: string;
        total_refs: number;
        out_of_pr_files: number;
    }[];
    tool_log: ToolCallRecord[];
    notes: string;
}

const EMPTY_EVIDENCE: LocalizerEvidence = {
    verified_callsites: [],
    verified_tests: [],
    verified_configs: [],
    verified_docs: [],
    symbol_summary: [],
    tool_log: [],
    notes: "",
};

interface ParsedJson {
    verified_callsites?: unknown[];
    verified_tests?: unknown[];
    verified_configs?: unknown[];
    verified_docs?: unknown[];
    symbol_summary?: unknown[];
    notes?: unknown;
}

function tryParseJson(raw: string): ParsedJson | null {
    const fenced = raw.match(/```(?:json)?\s*\n([\s\S]*?)\n```/);
    const candidate = (fenced ? fenced[1] : raw).trim();
    if (!candidate.startsWith("{")) return null;
    try {
        return JSON.parse(candidate) as ParsedJson;
    } catch {
        return null;
    }
}

function shapeEvidence(
    parsed: ParsedJson | null,
    toolLog: ToolCallRecord[],
): { ev: LocalizerEvidence; parseOk: boolean } {
    if (!parsed) return { ev: { ...EMPTY_EVIDENCE, tool_log: toolLog }, parseOk: false };
    const arr = <T>(x: unknown): T[] => (Array.isArray(x) ? (x as T[]) : []);
    const ev: LocalizerEvidence = {
        verified_callsites: arr<{
            symbol: string;
            file: string;
            line: number;
            snippet?: string;
        }>(parsed.verified_callsites),
        verified_tests: arr<{ symbol: string; file: string; line: number }>(
            parsed.verified_tests,
        ),
        verified_configs: arr<{ symbol: string; file: string; line: number }>(
            parsed.verified_configs,
        ),
        verified_docs: arr<Record<string, unknown>>(parsed.verified_docs).map((d) => ({
            source: String(d.source ?? ""),
            target_path: String(d.target_path ?? ""),
            ...(typeof d.target_section === "string" ? { target_section: d.target_section } : {}),
            ...(typeof d.line === "number" ? { line: d.line } : {}),
            kind: d.kind === "anchor" || d.kind === "term" ? d.kind : ("related" as const),
            ...(typeof d.snippet === "string" ? { snippet: d.snippet } : {}),
        })),
        symbol_summary: arr<{ symbol: string; total_refs: number; out_of_pr_files: number }>(
            parsed.symbol_summary,
        ),
        tool_log: toolLog,
        notes: typeof parsed.notes === "string" ? parsed.notes : "",
    };
    return { ev, parseOk: true };
}

function renderEvidenceMarkdown(ev: LocalizerEvidence, parseOk: boolean): string {
    const lines: string[] = [];
    const toolNames = ev.tool_log.map((t) => t.tool).filter(Boolean);
    const toolsUsedLabel = toolNames.length ? Array.from(new Set(toolNames)).join(", ") : "NONE";
    lines.push(
        `**Localizer evidence** (tools used: ${toolsUsedLabel}; tool calls: ${ev.tool_log.length})`,
    );
    if (!parseOk) {
        lines.push("");
        lines.push(
            "> WARNING: localizer output failed JSON validation — fields below may be empty.",
        );
    }
    if (ev.tool_log.length === 0) {
        lines.push("");
        lines.push(
            "> NOTE: localizer made ZERO tool calls. Cross-file claims below are NOT verified against the repo.",
        );
    }
    if (ev.symbol_summary.length) {
        lines.push("");
        lines.push("Per-symbol reference counts (repo-wide, from tools):");
        for (const s of ev.symbol_summary.slice(0, 20)) {
            lines.push(
                `- \`${s.symbol}\`: total_refs=${s.total_refs}, out_of_pr_files=${s.out_of_pr_files}`,
            );
        }
    }
    if (ev.verified_callsites.length) {
        lines.push("");
        lines.push("Out-of-PR call-sites (verified):");
        for (const c of ev.verified_callsites.slice(0, 30)) {
            const snip = c.snippet ? ` — \`${c.snippet.slice(0, 80)}\`` : "";
            lines.push(`- \`${c.symbol}\` at \`${c.file}:${c.line}\`${snip}`);
        }
    }
    if (ev.verified_tests.length) {
        lines.push("");
        lines.push("Tests referencing changed symbols (verified):");
        for (const t of ev.verified_tests.slice(0, 20)) {
            lines.push(`- \`${t.symbol}\` at \`${t.file}:${t.line}\``);
        }
    }
    if (ev.verified_configs.length) {
        lines.push("");
        lines.push("Configs referencing changed symbols (verified):");
        for (const c of ev.verified_configs.slice(0, 20)) {
            lines.push(`- \`${c.symbol}\` at \`${c.file}:${c.line}\``);
        }
    }
    if (ev.verified_docs.length) {
        lines.push("");
        lines.push("Doc cross-references (verified):");
        for (const d of ev.verified_docs.slice(0, 20)) {
            const sec = d.target_section ? ` § ${d.target_section}` : "";
            const ln = typeof d.line === "number" ? `:${d.line}` : "";
            const snip = d.snippet ? ` — \`${d.snippet.slice(0, 80)}\`` : "";
            lines.push(
                `- [${d.kind}] \`${d.source}\` → \`${d.target_path}${ln}\`${sec}${snip}`,
            );
        }
    }
    if (ev.notes?.trim()) {
        lines.push("");
        lines.push(`Notes: ${ev.notes.trim().slice(0, 500)}`);
    }
    return lines.join("\n");
}

export interface LocalizerOptions {
    mcp: McpSession;
    mcpTools: McpToolDef[];
    llm: LlmConfig;
    cwd: string;
    files: PullFileEntry[];
    log: (line: string) => void;
    budgetBytes?: number;
}

export interface LocalizerResult {
    markdown: string;
    evidence: LocalizerEvidence;
    parseOk: boolean;
    wallMs: number;
}

export async function gatherRepoContext(
    opts: LocalizerOptions,
): Promise<LocalizerResult> {
    const fileLines = opts.files
        .slice(0, 30)
        .map((f) => `- ${f.filename} (${f.status} +${f.additions}/-${f.deletions})`)
        .join("\n");
    const changedSymbols = extractChangedSymbols(opts.files);
    const prFilenames = new Set(opts.files.map((f) => f.filename));
    opts.log(`changed-symbols=${changedSymbols.length}`);

    const symbolsList = changedSymbols.length
        ? changedSymbols.map((s) => `  - ${s}`).join("\n")
        : "  (none extracted — fall back to grep on the diff's added APIs)";

    const DOC_EXT_RE = /\.(md|mdx|markdown|rst)$/i;
    const docFiles = opts.files.map((f) => f.filename).filter((n) => DOC_EXT_RE.test(n));
    const hasDocs = docFiles.length > 0;
    opts.log(`doc-files=${docFiles.length}`);

    const cwdJson = JSON.stringify(opts.cwd);
    const codeToolBlock = [
        "These answers CANNOT be inferred from the diff. They require lookup. Use the MCP tools listed in this turn — they are real and connected to the repo at the path above. Order:",
        `  1. **code_localizer** with path=${cwdJson}, op="trace", queries=<all changed symbols>. ONE call covers every symbol. Try this FIRST.`,
        `  2. If step 1 returns 0 matches for a symbol (common when newly introduced by this PR), fall back to **code_localizer** with op="context" for that symbol, or note the absence.`,
        `  3. Only call **repo_localizer** (path=${cwdJson}, view="overview") if you actually need a hotspot map — it returns 5-8 KB. For pure reference counts, SKIP it.`,
        "Do not duplicate work: if the trace op answered a symbol, do not look it up again.",
    ];

    const docFilesList = hasDocs ? docFiles.slice(0, 20).map((f) => `  - ${f}`).join("\n") : "";
    const docToolBlock = hasDocs
        ? [
              "",
              "**This PR touches documentation files:**",
              docFilesList,
              "",
              "For EACH changed doc file you ALSO must determine:",
              "  6. Up to 3 sibling pages that discuss the same concepts (so the reviewer can suggest cross-links).",
              "  7. For every relative link `[text](target)` added in the diff: whether `target` resolves to a real file in the repo.",
              "  8. For any prominent term/phrase introduced in the diff (a new heading, a new acronym): up to 3 other sections of the docs that already mention it.",
              "",
              "Use the doc-aware MCP tool — prefer it over raw search for prose lookup:",
              `  - **doc_localizer** (path=${cwdJson}, op="search", query=<heading, paragraph, or single term>, limit=5) — BM25 over heading-delimited sections; lower score = better.`,
              `  - **doc_localizer** (path=${cwdJson}, op="resolve_link", source_file=<changed doc>, target=<as-written link>) — verifies relative links. Call ONCE per added \`[text](target)\` link.`,
              "",
              "Record every doc finding in the `verified_docs` array.",
          ]
        : [];

    const docsSchemaLine = hasDocs
        ? '  "verified_docs":      [ { "source": str, "target_path": str, "target_section": str, "line": int, "kind": "related"|"anchor"|"term", "snippet": str } ],'
        : '  "verified_docs":      [],';

    const prompt = [
        "You are the **repo-context** localizer for a code review. Your output is consumed programmatically — it must be a single JSON object.",
        "",
        `**The repo is checked out locally at: ${opts.cwd}**`,
        "Files are on disk. Use the MCP tools to look at them. Do NOT invent paths or counts.",
        "",
        "Files changed in this PR (these count as IN-PR; everything else is out-of-PR):",
        fileLines,
        "",
        "Changed symbols (extracted from the diff):",
        symbolsList,
        "",
        "Your job is to answer EXTRACTION questions about the repo that the PR diff alone cannot answer. The diff tells the reviewer what changed inside these files; you must tell the reviewer what those changes touch elsewhere.",
        "",
        "For EACH changed symbol above you MUST determine, by issuing tool calls against the LOCAL repo:",
        "  1. Total repo-wide reference count (an integer).",
        "  2. Number of distinct OUT-OF-PR files that reference it (an integer).",
        "  3. Up to 3 out-of-PR call-sites as { file, line, snippet }.",
        "  4. Any test files (path matching /test/, /tests/, _test.*, .test.*, .spec.*) that reference it.",
        "  5. Any config files (yaml/yml/toml/json/ini under config/, .github/, infra/) that reference it.",
        "",
        ...codeToolBlock,
        ...docToolBlock,
        "",
        "**Output: ONE JSON object inside a single ```json fenced block. No prose outside the block.** Schema:",
        "```",
        "{",
        '  "verified_callsites": [ { "symbol": str, "file": str, "line": int, "snippet": str } ],',
        '  "verified_tests":     [ { "symbol": str, "file": str, "line": int } ],',
        '  "verified_configs":   [ { "symbol": str, "file": str, "line": int } ],',
        docsSchemaLine,
        '  "symbol_summary":     [ { "symbol": str, "total_refs": int, "out_of_pr_files": int } ],',
        '  "notes":              str',
        "}",
        "```",
        "",
        "Hard rules:",
        "- Every `file` in the verified arrays MUST be an out-of-PR path. Do NOT include the changed files listed above.",
        "- Every entry MUST come from a tool call you actually issued this turn. The runtime records your tool calls separately — do not invent results.",
        "- If a tool call returned zero matches for a symbol, leave the corresponding arrays empty for that symbol.",
        hasDocs
            ? "- For docs PRs: the `verified_docs` array MUST NOT be empty unless every doc-aware tool call returned zero matches."
            : "- This PR has no doc files; `verified_docs` MUST be an empty array.",
        "- Cap total bytes of the JSON to ~6 KB; truncate `verified_callsites` to the most informative entries if needed.",
        "- `notes` is for short caveats only (max 300 chars).",
    ].join("\n");

    const t0 = Date.now();
    const { text, toolCalls } = await chatWithTools({
        cfg: opts.llm,
        mcp: opts.mcp,
        tools: opts.mcpTools,
        user: prompt,
        log: opts.log,
        maxIterations: 12,
    });
    const wallMs = Date.now() - t0;

    const parsed = tryParseJson(text);
    const { ev, parseOk } = shapeEvidence(parsed, toolCalls);

    // Filter self-references — out-of-PR only.
    ev.verified_callsites = ev.verified_callsites.filter((c) => !prFilenames.has(c.file));
    ev.verified_tests = ev.verified_tests.filter((c) => !prFilenames.has(c.file));
    ev.verified_configs = ev.verified_configs.filter((c) => !prFilenames.has(c.file));
    ev.verified_docs = ev.verified_docs.filter((d) => !prFilenames.has(d.target_path));

    let markdown = renderEvidenceMarkdown(ev, parseOk);
    const cap = opts.budgetBytes ?? 8192;
    if (markdown.length > cap) markdown = markdown.slice(0, cap) + "\n... [truncated]";

    opts.log(
        `mode=mcp wall=${wallMs}ms parseOk=${parseOk} tool_calls=${toolCalls.length} callsites=${ev.verified_callsites.length} tests=${ev.verified_tests.length} docs=${ev.verified_docs.length}`,
    );

    return { markdown, evidence: ev, parseOk, wallMs };
}
