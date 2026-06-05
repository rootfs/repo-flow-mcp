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

    // Build runner-friendly query candidates from the changed file paths.
    // `code_localizer trace` matches on runner labels (Make targets, scripts,
    // CI workflow/job/step) and on function names that runners invoke — NOT
    // generic Go/TS function callers. So the highest-signal queries are:
    //   - changed file basenames (often == script / target name)
    //   - changed file path stems (e.g. `dashboard/backend/handlers/logs`)
    //   - the changed code symbols themselves (low hit rate, kept for completeness)
    const fileBasenames = Array.from(
        new Set(opts.files.map((f) => f.filename.split("/").pop()!.replace(/\.[^.]+$/, ""))),
    );
    const traceCandidates = Array.from(new Set([...fileBasenames, ...changedSymbols])).slice(0, 50);

    const symbolsList = changedSymbols.length
        ? changedSymbols.map((s) => `  - ${s}`).join("\n")
        : "  (none extracted)";
    const traceList = traceCandidates.map((s) => `  - ${s}`).join("\n");

    const DOC_EXT_RE = /\.(md|mdx|markdown|rst)$/i;
    const docFiles = opts.files.map((f) => f.filename).filter((n) => DOC_EXT_RE.test(n));
    const hasDocs = docFiles.length > 0;
    opts.log(`doc-files=${docFiles.length}`);

    const cwdJson = JSON.stringify(opts.cwd);

    const toolStrategy = [
        "Tool semantics — read carefully:",
        "  - `code_localizer` with op=\"trace\" surfaces **runner-graph** matches: Make targets, shell scripts, CI workflow/job/step, and the build_target/ci_runner/script/module nodes that depend on the queried label. It does **not** do generic Go/TS function-caller search. Empty matches for a function name usually means \"no Make/CI/script directly references this label\" — that is a real, useful signal.",
        "  - `repo_localizer` with view=\"overview\" returns subsystem layout + top files by edge count; use it once for orientation.",
        "  - `doc_localizer` with op=\"search\" does BM25 search over markdown/rst sections; great for finding docs that mention a changed concept or a changed file path.",
        "  - There is **no plain grep tool** in this server. If the runner-graph and doc-search both come up empty, the honest answer is empty arrays plus a note.",
        "",
        "Required call sequence (execute in this order, then stop):",
        `  1. **repo_localizer**(path=${cwdJson}, view="overview"). ONE call. Look at top_files_by_edges to spot any OUT-OF-PR files clustered around the changed code.`,
        `  2. **code_localizer**(path=${cwdJson}, op="trace", queries=<the trace candidates list below>). ONE call covers every candidate. Inspect each match's incoming/outgoing edges for runners/scripts/CI that depend on the changed files. Empty matches are fine — record them as zero refs.`,
        `  3. **doc_localizer**(path=${cwdJson}, op="search", query=<changed file basename OR a concept term from the diff>, limit=5). Issue ONE call per distinct query (max 4 queries). Use it to find docs/READMEs that reference the changed files or concepts.`,
        hasDocs
            ? `  4. For every relative link [text](target) added in a changed doc file, call **doc_localizer**(path=${cwdJson}, op="resolve_link", source_file=<changed doc>, target=<link as written>). One call per added link.`
            : "  4. (Skipped — no doc files in this PR.)",
    ];

    const prompt = [
        "You are the **repo-context** localizer for a code review. Your output is consumed programmatically — it must be a single JSON object.",
        "",
        `**The repo is checked out locally at: ${opts.cwd}**`,
        "",
        "Files changed in this PR (IN-PR; everything else is out-of-PR):",
        fileLines,
        "",
        "Changed symbols (extracted from the diff):",
        symbolsList,
        "",
        "Trace query candidates (file basenames + symbols — these are the labels code_localizer can match):",
        traceList,
        ...(hasDocs
            ? [
                  "",
                  "Changed doc files:",
                  ...docFiles.slice(0, 20).map((f) => `  - ${f}`),
              ]
            : []),
        "",
        "Your job: surface concrete OUT-OF-PR files and lines that the reviewer should also look at — runners (Make/CI/script targets) that depend on the changed files, related tests reachable through the runner graph, configs that mention the changed names, and docs that reference the changed concepts. Use ONLY the tool outputs you actually retrieve this turn. Never invent paths.",
        "",
        ...toolStrategy,
        "",
        "**Output: ONE JSON object inside a single ```json fenced block. No prose outside the block.** Schema:",
        "```",
        "{",
        '  "verified_callsites": [ { "symbol": str, "file": str, "line": int, "snippet": str } ],   // out-of-PR runner / script / CI nodes that depend on the changed files (from code_localizer trace incoming/outgoing edges). The "symbol" field is the label that produced the match; "file" is the runner file path; "snippet" is the edge command if available.',
        '  "verified_tests":     [ { "symbol": str, "file": str, "line": int } ],                   // out-of-PR test files surfaced by code_localizer (filenames matching /test/, /tests/, _test.*, .test.*, .spec.*).',
        '  "verified_configs":   [ { "symbol": str, "file": str, "line": int } ],                   // out-of-PR config files (yaml/yml/toml/json/ini under config/, .github/, infra/, deploy/).',
        '  "verified_docs":      [ { "source": str, "target_path": str, "target_section": str, "line": int, "kind": "related"|"anchor"|"term", "snippet": str } ],   // out-of-PR docs surfaced by doc_localizer search/resolve_link.',
        '  "symbol_summary":     [ { "symbol": str, "total_refs": int, "out_of_pr_files": int } ], // one entry per changed symbol, with the counts you actually observed.',
        '  "notes":              str  // short caveat (max 300 chars). Use this when every search returned empty so the reviewer knows it was checked.',
        "}",
        "```",
        "",
        "Hard rules:",
        "- Every `file` and `target_path` in the verified arrays MUST be an out-of-PR path. Do NOT include any changed file listed above.",
        "- Every entry MUST come from a tool call you actually issued this turn. The runtime separately records your tool calls — do not invent results.",
        "- Empty arrays are valid and informative when the searches truly returned nothing — record that fact in `notes`.",
        "- Cap total bytes of the JSON to ~6 KB.",
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
