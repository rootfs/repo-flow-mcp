import OpenAI from "openai";
import type {
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCall,
    ChatCompletionTool,
} from "openai/resources/chat/completions.mjs";
import type { McpSession, McpToolDef } from "./mcp.js";

export interface LlmConfig {
    /** OpenAI-compatible base URL. Default: GitHub Models. */
    baseUrl: string;
    /** API key (env-resolved upstream). */
    apiKey: string;
    /** Model identifier. */
    model: string;
    /** Optional max output tokens; some endpoints require it. */
    maxOutputTokens?: number;
    /** Optional temperature. */
    temperature?: number;
}

export function resolveLlmConfig(overrides: Partial<LlmConfig> = {}): LlmConfig {
    const baseUrl =
        overrides.baseUrl ??
        process.env.MCP_REVIEW_LLM_BASE_URL ??
        process.env.OPENAI_BASE_URL ??
        "https://models.github.ai/inference";
    const model =
        overrides.model ??
        process.env.MCP_REVIEW_LLM_MODEL ??
        process.env.OPENAI_MODEL ??
        "openai/gpt-4o";
    const keyEnv = process.env.MCP_REVIEW_LLM_API_KEY_ENV ?? "GITHUB_TOKEN";
    const apiKey =
        overrides.apiKey ??
        process.env.MCP_REVIEW_LLM_API_KEY ??
        process.env.OPENAI_API_KEY ??
        process.env[keyEnv] ??
        process.env.GITHUB_TOKEN ??
        process.env.GH_TOKEN ??
        "";
    if (!apiKey) {
        throw new Error(
            `no LLM API key. Set MCP_REVIEW_LLM_API_KEY, OPENAI_API_KEY, or ${keyEnv}. ` +
            "When using the GitHub Models endpoint (default), a GITHUB_TOKEN with `models:read` is sufficient.",
        );
    }
    const out: LlmConfig = { baseUrl, apiKey, model };
    if (overrides.maxOutputTokens !== undefined) out.maxOutputTokens = overrides.maxOutputTokens;
    if (overrides.temperature !== undefined) out.temperature = overrides.temperature;
    return out;
}

export function makeLlm(cfg: LlmConfig): OpenAI {
    return new OpenAI({ apiKey: cfg.apiKey, baseURL: cfg.baseUrl });
}

/** Convert MCP tool definitions into OpenAI tool-call format. */
export function mcpToolsToOpenAi(tools: McpToolDef[]): ChatCompletionTool[] {
    return tools.map((t) => ({
        type: "function" as const,
        function: {
            name: t.name,
            description: t.description.slice(0, 1024),
            parameters: t.inputSchema,
        },
    }));
}

export interface ToolCallRecord {
    tool: string;
    args: Record<string, unknown>;
    resultBytes: number;
    durationMs: number;
    isError: boolean;
}

export interface ChatWithToolsOptions {
    cfg: LlmConfig;
    mcp: McpSession;
    tools: McpToolDef[];
    /** System message; if undefined, no system message is sent. */
    system?: string;
    /** User prompt. */
    user: string;
    /** Cap on tool-call iterations. */
    maxIterations?: number;
    /** Per-tool output cap (bytes) shoved into the conversation. */
    toolResultCap?: number;
    /** Logger for progress lines. */
    log: (line: string) => void;
}

export interface ChatWithToolsResult {
    text: string;
    toolCalls: ToolCallRecord[];
}

/**
 * Run a single LLM turn with iterative tool-use. Each round either yields
 * the assistant's final text (loop ends) or one-or-more tool_calls that
 * are dispatched through the MCP session and fed back into the next round.
 * The loop caps both iterations and per-tool result size to keep the
 * conversation bounded.
 */
export async function chatWithTools(
    opts: ChatWithToolsOptions,
): Promise<ChatWithToolsResult> {
    const llm = makeLlm(opts.cfg);
    const openaiTools = mcpToolsToOpenAi(opts.tools);
    const maxIter = opts.maxIterations ?? 12;
    const cap = opts.toolResultCap ?? 8192;
    const messages: ChatCompletionMessageParam[] = [];
    if (opts.system) messages.push({ role: "system", content: opts.system });
    messages.push({ role: "user", content: opts.user });

    const calls: ToolCallRecord[] = [];

    for (let iter = 0; iter < maxIter; iter++) {
        const params: Parameters<typeof llm.chat.completions.create>[0] = {
            model: opts.cfg.model,
            messages,
            tools: openaiTools.length ? openaiTools : undefined,
            tool_choice: openaiTools.length ? "auto" : undefined,
        };
        if (opts.cfg.maxOutputTokens) params.max_tokens = opts.cfg.maxOutputTokens;
        if (opts.cfg.temperature !== undefined) params.temperature = opts.cfg.temperature;

        const resp = (await llm.chat.completions.create(params)) as ChatCompletion;
        const choice = resp.choices?.[0];
        if (!choice) throw new Error("LLM returned no choices");
        const msg = choice.message;
        messages.push(msg as ChatCompletionMessageParam);

        const toolCalls = (msg.tool_calls ?? []).filter(
            (c: ChatCompletionMessageToolCall) => c.type === "function",
        );

        if (!toolCalls.length) {
            return { text: msg.content ?? "", toolCalls: calls };
        }

        opts.log(
            `iter=${iter + 1}/${maxIter} tool_calls=${toolCalls.length}`,
        );

        for (const tc of toolCalls) {
            if (tc.type !== "function") continue;
            const name = tc.function.name;
            let parsed: Record<string, unknown> = {};
            try {
                parsed = JSON.parse(tc.function.arguments || "{}");
            } catch {
                parsed = { _raw: tc.function.arguments };
            }
            const t0 = Date.now();
            let toolOut: { text: string; isError: boolean };
            try {
                toolOut = await opts.mcp.callTool(name, parsed);
            } catch (e) {
                toolOut = {
                    text: `tool ${name} threw: ${(e as Error).message}`,
                    isError: true,
                };
            }
            const dur = Date.now() - t0;
            const text =
                toolOut.text.length > cap
                    ? toolOut.text.slice(0, cap) + `\n... [truncated ${toolOut.text.length - cap} bytes]`
                    : toolOut.text;
            calls.push({
                tool: name,
                args: parsed,
                resultBytes: toolOut.text.length,
                durationMs: dur,
                isError: toolOut.isError,
            });
            opts.log(
                `  -> ${name}(${JSON.stringify(parsed).slice(0, 80)}) bytes=${toolOut.text.length} dur=${dur}ms${toolOut.isError ? " ERROR" : ""}`,
            );
            messages.push({
                role: "tool",
                tool_call_id: tc.id,
                content: text,
            });
        }
    }

    opts.log(`hit iteration cap (${maxIter}); forcing a no-tools final turn`);
    const finalResp = (await llm.chat.completions.create({
        model: opts.cfg.model,
        messages,
        ...(opts.cfg.maxOutputTokens ? { max_tokens: opts.cfg.maxOutputTokens } : {}),
        ...(opts.cfg.temperature !== undefined ? { temperature: opts.cfg.temperature } : {}),
    })) as ChatCompletion;
    return {
        text: finalResp.choices?.[0]?.message?.content ?? "",
        toolCalls: calls,
    };
}

/** Plain (no-tools) single-turn chat. */
export async function chatPlain(
    cfg: LlmConfig,
    system: string | undefined,
    user: string,
): Promise<string> {
    const llm = makeLlm(cfg);
    const messages: ChatCompletionMessageParam[] = [];
    if (system) messages.push({ role: "system", content: system });
    messages.push({ role: "user", content: user });
    const params: Parameters<typeof llm.chat.completions.create>[0] = {
        model: cfg.model,
        messages,
    };
    if (cfg.maxOutputTokens) params.max_tokens = cfg.maxOutputTokens;
    if (cfg.temperature !== undefined) params.temperature = cfg.temperature;
    const resp = (await llm.chat.completions.create(params)) as ChatCompletion;
    return resp.choices?.[0]?.message?.content ?? "";
}
