import type { InlineComment, PullFileEntry } from "./github.js";

// ---------------------------------------------------------------------------
// Changed-symbol extraction (per-language regexes over the diff hunks)
// ---------------------------------------------------------------------------

interface SymbolCandidate {
    name: string;
    file: string;
    score: number;
}

const SYMBOL_RULES: Array<{
    ext: RegExp;
    patterns: RegExp[];
    exported: (name: string) => boolean;
}> = [
    {
        ext: /\.go$/,
        patterns: [
            /\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)/,
            /\btype\s+([A-Za-z_][A-Za-z0-9_]*)\b/,
        ],
        exported: (n) => /^[A-Z]/.test(n),
    },
    {
        ext: /\.rs$/,
        patterns: [
            /\b(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)/,
            /\b(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)/,
            /\b(?:pub\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)/,
        ],
        exported: () => false,
    },
    {
        ext: /\.py$/,
        patterns: [
            /\bdef\s+([A-Za-z_][A-Za-z0-9_]*)/,
            /\bclass\s+([A-Za-z_][A-Za-z0-9_]*)/,
        ],
        exported: (n) => !n.startsWith("_"),
    },
    {
        ext: /\.(ts|tsx|js|jsx|mjs|cjs)$/,
        patterns: [
            /\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)/,
            /\b(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)/,
            /\b(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=/,
            /\b(?:export\s+)?interface\s+([A-Za-z_$][A-Za-z0-9_$]*)/,
            /\b(?:export\s+)?type\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=/,
        ],
        exported: () => false,
    },
    {
        ext: /\.(c|cc|cpp|cxx|h|hpp)$/,
        patterns: [
            /\bclass\s+([A-Za-z_][A-Za-z0-9_]*)/,
            /\bstruct\s+([A-Za-z_][A-Za-z0-9_]*)/,
        ],
        exported: () => false,
    },
    {
        ext: /\.java$/,
        patterns: [
            /\bclass\s+([A-Za-z_][A-Za-z0-9_]*)/,
            /\binterface\s+([A-Za-z_][A-Za-z0-9_]*)/,
        ],
        exported: () => false,
    },
];

const CHANGED_KEYWORDS = new Set([
    "if", "else", "for", "while", "return", "switch", "case", "break",
    "continue", "import", "package", "from", "as", "in", "is", "and",
    "or", "not", "true", "false", "null", "nil", "None", "True", "False",
    "self", "this", "new", "void", "int", "string", "bool", "let", "const",
    "var", "fn", "def", "func", "type", "class", "struct", "enum", "impl",
    "interface", "pub", "static", "public", "private", "protected", "final",
    "async", "await", "function", "export",
]);

export function extractChangedSymbols(
    files: PullFileEntry[],
    opts: { maxTotal?: number; maxPerFile?: number } = {},
): string[] {
    const maxTotal = opts.maxTotal ?? 20;
    const maxPerFile = opts.maxPerFile ?? 5;
    const candidates: SymbolCandidate[] = [];

    for (const f of files) {
        if (!f.patch) continue;
        const rule = SYMBOL_RULES.find((r) => r.ext.test(f.filename));
        if (!rule) continue;
        const counts = new Map<string, number>();
        for (const ln of f.patch.split("\n")) {
            if (!(ln.startsWith("+") || ln.startsWith("-"))) continue;
            if (ln.startsWith("+++") || ln.startsWith("---")) continue;
            const body = ln.slice(1);
            for (const pat of rule.patterns) {
                const m = body.match(pat);
                if (!m) continue;
                const name = m[1];
                if (!name || CHANGED_KEYWORDS.has(name)) continue;
                if (name.length < 2) continue;
                counts.set(name, (counts.get(name) ?? 0) + 1);
            }
        }
        const fileScored: SymbolCandidate[] = [];
        for (const [name, count] of counts) {
            const score = count + (rule.exported(name) ? 2 : 0);
            fileScored.push({ name, file: f.filename, score });
        }
        fileScored.sort((a, b) => b.score - a.score);
        for (const c of fileScored.slice(0, maxPerFile)) candidates.push(c);
    }

    candidates.sort((a, b) => b.score - a.score);
    const seen = new Set<string>();
    const out: string[] = [];
    for (const c of candidates) {
        if (seen.has(c.name)) continue;
        seen.add(c.name);
        out.push(c.name);
        if (out.length >= maxTotal) break;
    }
    return out;
}

// ---------------------------------------------------------------------------
// Diff annotation + hunk index for inline-comment validation
// ---------------------------------------------------------------------------

/** Prefix each non-header diff line with [N###] (RIGHT) or [O###] (LEFT). */
export function annotatePatch(patch: string): string {
    const out: string[] = [];
    let oldLine = 0;
    let newLine = 0;
    for (const line of patch.split("\n")) {
        const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        if (m) {
            oldLine = parseInt(m[1], 10);
            newLine = parseInt(m[2], 10);
            out.push(line);
            continue;
        }
        if (
            line.startsWith("+++") ||
            line.startsWith("---") ||
            line.startsWith("diff ") ||
            line.startsWith("index ")
        ) {
            out.push(line);
            continue;
        }
        if (line.startsWith("+")) {
            out.push(`+ [N${newLine}] ${line.slice(1)}`);
            newLine++;
        } else if (line.startsWith("-")) {
            out.push(`- [O${oldLine}] ${line.slice(1)}`);
            oldLine++;
        } else if (line.startsWith(" ") || line === "") {
            out.push(`  [N${newLine}] ${line.slice(1)}`);
            oldLine++;
            newLine++;
        } else {
            out.push(line);
        }
    }
    return out.join("\n");
}

export function parsePatchHunks(patch: string): {
    right: Set<number>;
    left: Set<number>;
} {
    const right = new Set<number>();
    const left = new Set<number>();
    if (!patch) return { right, left };
    let oldLine = 0;
    let newLine = 0;
    for (const ln of patch.split("\n")) {
        const m = ln.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        if (m) {
            oldLine = parseInt(m[1], 10);
            newLine = parseInt(m[2], 10);
            continue;
        }
        if (ln.startsWith("+++") || ln.startsWith("---")) continue;
        if (ln.startsWith("+")) {
            right.add(newLine);
            newLine++;
        } else if (ln.startsWith("-")) {
            left.add(oldLine);
            oldLine++;
        } else if (ln.startsWith(" ") || ln === "") {
            right.add(newLine);
            left.add(oldLine);
            newLine++;
            oldLine++;
        }
    }
    return { right, left };
}

export function buildHunkIndex(
    files: PullFileEntry[],
): Map<string, { right: Set<number>; left: Set<number> }> {
    const idx = new Map<string, { right: Set<number>; left: Set<number> }>();
    for (const f of files) idx.set(f.filename, parsePatchHunks(f.patch ?? ""));
    return idx;
}

// ---------------------------------------------------------------------------
// Inline-comments block parsing + validation
// ---------------------------------------------------------------------------

interface RawInline {
    path?: unknown;
    line?: unknown;
    side?: unknown;
    body?: unknown;
    start_line?: unknown;
    startLine?: unknown;
    start_side?: unknown;
    startSide?: unknown;
}

export function splitInlineBlock(text: string): { body: string; inline: RawInline[] } {
    const re = /```(?:inline-comments|inline|json)\s*\n([\s\S]*?)\n```/i;
    const m = text.match(re);
    if (!m) return { body: text.trimEnd(), inline: [] };
    const body = (text.slice(0, m.index ?? 0) + text.slice((m.index ?? 0) + m[0].length)).trimEnd();
    let parsed: unknown;
    try {
        parsed = JSON.parse(m[1]);
    } catch {
        return { body, inline: [] };
    }
    if (!Array.isArray(parsed)) return { body, inline: [] };
    return { body, inline: parsed as RawInline[] };
}

export function validateInline(
    raw: RawInline[],
    hunks: Map<string, { right: Set<number>; left: Set<number> }>,
): { kept: InlineComment[]; dropped: number } {
    const kept: InlineComment[] = [];
    let dropped = 0;
    for (const r of raw) {
        const path = typeof r.path === "string" ? r.path : null;
        const line = typeof r.line === "number" ? r.line : Number(r.line);
        const side = (r.side === "LEFT" ? "LEFT" : "RIGHT") as "LEFT" | "RIGHT";
        const body = typeof r.body === "string" ? r.body.trim() : "";
        if (!path || !Number.isFinite(line) || !body) {
            dropped++;
            continue;
        }
        const h = hunks.get(path);
        if (!h) {
            dropped++;
            continue;
        }
        const set = side === "LEFT" ? h.left : h.right;
        if (!set.has(line)) {
            dropped++;
            continue;
        }
        const startLineRaw = r.start_line ?? r.startLine;
        const startLine =
            typeof startLineRaw === "number" ? startLineRaw : Number(startLineRaw);
        const startSideRaw = r.start_side ?? r.startSide;
        const startSide =
            startSideRaw === "LEFT"
                ? "LEFT"
                : startSideRaw === "RIGHT"
                  ? "RIGHT"
                  : undefined;
        const c: InlineComment = { path, line, side, body };
        if (Number.isFinite(startLine) && startLine < line) {
            const startSet = (startSide ?? side) === "LEFT" ? h.left : h.right;
            if (startSet.has(startLine)) {
                c.startLine = startLine;
                if (startSide) c.startSide = startSide;
            }
        }
        kept.push(c);
    }
    return { kept, dropped };
}

export function inferVerdict(
    text: string,
): "approve" | "comment" | "request_changes" {
    const m = text.match(
        /(?:^|\n)\s*(?:\*\*Verdict\*\*|Verdict)[^\n]*?(approve|comment|request[_\s]?changes)/i,
    );
    if (m) {
        const raw = m[1].toLowerCase().replace(/\s/g, "_");
        if (raw === "approve") return "approve";
        if (raw === "request_changes") return "request_changes";
    }
    return "comment";
}

export function eventFromVerdict(
    verdict: "approve" | "comment" | "request_changes",
): "APPROVE" | "REQUEST_CHANGES" | "COMMENT" {
    if (verdict === "approve") return "APPROVE";
    if (verdict === "request_changes") return "REQUEST_CHANGES";
    return "COMMENT";
}
