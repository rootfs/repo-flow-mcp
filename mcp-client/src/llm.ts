import { generateText, jsonSchema, tool as aiTool } from "ai";
import type { CoreMessage, LanguageModelV1, ToolSet } from "ai";
import { createOpenAI } from "@ai-sdk/openai";
import { createAzure } from "@ai-sdk/azure";
import type { McpSession, McpToolDef } from "./mcp.js";

export interface AzureConfig {
    /** Full endpoint, e.g. https://my-resource.openai.azure.com */
    endpoint: string;
    /** Deployment name (Azure-side name, not the underlying model id). */
    deployment: string;
    /** API version, e.g. 2024-10-21. */
    apiVersion: string;
}

export interface LlmConfig {
    /** Provider selector. */
    provider: "openai" | "azure";
    /** API key. */
    apiKey: string;
    /**
     * Model id. For Azure this must match the deployment name; for OpenAI-compatible
     * endpoints (incl. GitHub Models) it's the served model id.
     */
    model: string;
    /** Base URL for the OpenAI-compatible provider. Ignored when provider==='azure'. */
    baseUrl?: string;
    /** Azure-specific settings. Required when provider==='azure'. */
    azure?: AzureConfig;
    /** Optional max output tokens. */
    maxOutputTokens?: number;
    /** Optional sampling temperature. */
    temperature?: number;
}

export function resolveLlmConfig(overrides: Partial<LlmConfig> = {}): LlmConfig {
    const azureEndpoint =
        overrides.azure?.endpoint ??
        process.env.AZURE_OPENAI_ENDPOINT ??
        process.env.AZURE_ENDPOINT;
    const azureDeployment =
        overrides.azure?.deployment ??
        process.env.AZURE_OPENAI_DEPLOYMENT ??
        process.env.AZURE_DEPLOYMENT ??
        overrides.model ??
        process.env.MCP_REVIEW_LLM_MODEL;
    const azureApiVersion =
        overrides.azure?.apiVersion ??
        process.env.AZURE_OPENAI_API_VERSION ??
        process.env.AZURE_API_VERSION ??
        "2024-10-21";
    const wantsAzure = overrides.provider === "azure" || Boolean(azureEndpoint);

    if (wantsAzure) {
        if (!azureEndpoint) {
            throw new Error("Azure OpenAI requested but no endpoint found. Set AZURE_OPENAI_ENDPOINT (or AZURE_ENDPOINT).");
        }
        if (!azureDeployment) {
            throw new Error("Azure OpenAI requested but no deployment found. Set AZURE_OPENAI_DEPLOYMENT (or AZURE_DEPLOYMENT) or pass --model.");
        }
        const apiKey =
            // Azure-specific key sources win; we deliberately do not pick `overrides.apiKey`
            // here so a caller-provided GitHub token doesn't get sent to Azure as the api-key.
            process.env.AZURE_OPENAI_API_KEY ??
            process.env.AZURE_API_KEY ??
            process.env.API_KEY ??
            process.env.MCP_REVIEW_LLM_API_KEY ??
            "";
        if (!apiKey) {
            throw new Error("Azure OpenAI requested but no API key found. Set AZURE_OPENAI_API_KEY (or API_KEY).");
        }
        const out: LlmConfig = {
            provider: "azure",
            apiKey,
            model: azureDeployment,
            azure: { endpoint: azureEndpoint, deployment: azureDeployment, apiVersion: azureApiVersion },
        };
        if (overrides.maxOutputTokens !== undefined) out.maxOutputTokens = overrides.maxOutputTokens;
        if (overrides.temperature !== undefined) out.temperature = overrides.temperature;
        return out;
    }

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
    const out: LlmConfig = { provider: "openai", apiKey, model, baseUrl };
    if (overrides.maxOutputTokens !== undefined) out.maxOutputTokens = overrides.maxOutputTokens;
    if (overrides.temperature !== undefined) out.temperature = overrides.temperature;
    return out;
}

// Azure endpoint shape varies; createAzure() wants either `resourceName` (which it expands to
// https://{resourceName}.openai.azure.com/openai/deployments/...) or a `baseURL` already
// pointing at /openai/deployments. Accept both forms in env input.
function azureProviderArgs(cfg: AzureConfig, apiKey: string) {
    const endpoint = cfg.endpoint.replace(/\/+$/, "");
    const m = endpoint.match(/^https?:\/\/([^.\/]+)\.openai\.azure\.com$/);
    const base = m
        ? { resourceName: m[1] }
        : { baseURL: endpoint.endsWith("/openai/deployments") ? endpoint : `${endpoint}/openai/deployments` };
    return { apiKey, apiVersion: cfg.apiVersion, ...base };
}

export function makeModel(cfg: LlmConfig): LanguageModelV1 {
    if (cfg.provider === "azure") {
        if (!cfg.azure) throw new Error("azure provider selected without azure config");
        const azure = createAzure(azureProviderArgs(cfg.azure, cfg.apiKey));
        return azure(cfg.azure.deployment);
    }
    const openai = createOpenAI({ apiKey: cfg.apiKey, baseURL: cfg.baseUrl });
    return openai(cfg.model);
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
 * Iterative tool-use turn delegated to the Vercel AI SDK. Each MCP tool is exposed
 * as an AI-SDK tool whose `execute` dispatches into the MCP session; the SDK
 * handles function-call parsing, scheduling, and message threading. We just cap
 * step count and per-tool output size.
 */
export async function chatWithTools(opts: ChatWithToolsOptions): Promise<ChatWithToolsResult> {
    const model = makeModel(opts.cfg);
    const cap = opts.toolResultCap ?? 8192;
    const maxSteps = opts.maxIterations ?? 12;
    const calls: ToolCallRecord[] = [];

    // Azure OpenAI tool schemas are validated under strict mode; the API rejects any
    // object schema that doesn't explicitly set `additionalProperties: false`.
    // The generic OpenAI / GitHub-Models endpoint is lax about it, but normalizing
    // doesn't hurt either side, so we always apply it.
    const tools: ToolSet = Object.fromEntries(
        opts.tools.map((t) => [
            t.name,
            aiTool({
                description: t.description.slice(0, 1024),
                parameters: jsonSchema(strictSchema(t.inputSchema) as Record<string, unknown>),
                execute: async (args: unknown) => {
                    const parsed = (args ?? {}) as Record<string, unknown>;
                    const t0 = Date.now();
                    let toolOut: { text: string; isError: boolean };
                    try {
                        toolOut = await opts.mcp.callTool(t.name, parsed);
                    } catch (e) {
                        toolOut = { text: `tool ${t.name} threw: ${(e as Error).message}`, isError: true };
                    }
                    const dur = Date.now() - t0;
                    calls.push({
                        tool: t.name,
                        args: parsed,
                        resultBytes: toolOut.text.length,
                        durationMs: dur,
                        isError: toolOut.isError,
                    });
                    opts.log(
                        `  -> ${t.name}(${JSON.stringify(parsed).slice(0, 80)}) bytes=${toolOut.text.length} dur=${dur}ms${toolOut.isError ? " ERROR" : ""}`,
                    );
                    return toolOut.text.length > cap
                        ? toolOut.text.slice(0, cap) + `\n... [truncated ${toolOut.text.length - cap} bytes]`
                        : toolOut.text;
                },
            }),
        ]),
    );

    const messages: CoreMessage[] = [{ role: "user", content: opts.user }];

    const result = await generateText({
        model,
        tools,
        maxSteps,
        ...(opts.system ? { system: opts.system } : {}),
        messages,
        ...(opts.cfg.maxOutputTokens ? { maxTokens: opts.cfg.maxOutputTokens } : {}),
        ...(opts.cfg.temperature !== undefined ? { temperature: opts.cfg.temperature } : {}),
        onStepFinish: ({ stepType, toolCalls, finishReason }) => {
            opts.log(`step=${stepType} tool_calls=${toolCalls?.length ?? 0} finish=${finishReason}`);
        },
    });

    return { text: result.text ?? "", toolCalls: calls };
}

/**
 * Make a JSON Schema conformant with Azure OpenAI's strict tool-call validation:
 * every object schema must set `additionalProperties: false` and list every
 * property name in `required`. This loses the "optional" distinction, but the
 * LLM simply ends up always providing every parameter (with sensible defaults
 * picked from the description), which is fine for our MCP tools.
 */
function strictSchema(schema: unknown): unknown {
    if (Array.isArray(schema)) return schema.map(strictSchema);
    if (!schema || typeof schema !== "object") return schema;
    const src = schema as Record<string, unknown>;
    const out: Record<string, unknown> = { ...src };
    if (out.type === "object") {
        if (out.additionalProperties === undefined) out.additionalProperties = false;
        if (out.properties && typeof out.properties === "object") {
            const props = out.properties as Record<string, unknown>;
            out.required = Object.keys(props);
            out.properties = Object.fromEntries(
                Object.entries(props).map(([k, v]) => [k, strictSchema(v)]),
            );
        }
    }
    if (out.items !== undefined) out.items = strictSchema(out.items);
    for (const key of ["anyOf", "oneOf", "allOf"] as const) {
        if (Array.isArray(out[key])) out[key] = (out[key] as unknown[]).map(strictSchema);
    }
    return out;
}

/** Plain (no-tools) single-turn chat. */
export async function chatPlain(
    cfg: LlmConfig,
    system: string | undefined,
    user: string,
): Promise<string> {
    const model = makeModel(cfg);
    const result = await generateText({
        model,
        ...(system ? { system } : {}),
        prompt: user,
        ...(cfg.maxOutputTokens ? { maxTokens: cfg.maxOutputTokens } : {}),
        ...(cfg.temperature !== undefined ? { temperature: cfg.temperature } : {}),
    });
    return result.text ?? "";
}
