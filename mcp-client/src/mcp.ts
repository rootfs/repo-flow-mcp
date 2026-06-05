import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

export interface McpToolDef {
    name: string;
    description: string;
    inputSchema: Record<string, unknown>;
}

export interface McpServerConfig {
    /** Executable to spawn (default `repo-flow-mcp`). Override with MCP_REVIEW_SERVER_CMD. */
    command: string;
    args: string[];
    env?: Record<string, string>;
}

export function defaultServerConfig(): McpServerConfig {
    const cmd = process.env.MCP_REVIEW_SERVER_CMD?.trim();
    if (cmd) {
        const parts = cmd.split(/\s+/);
        return { command: parts[0], args: parts.slice(1) };
    }
    return { command: "repo-flow-mcp", args: ["serve"] };
}

/** Minimal MCP client: connects via stdio, lists tools, calls them. */
export class McpSession {
    private client: Client;
    private transport: StdioClientTransport;
    private connected = false;

    constructor(cfg: McpServerConfig) {
        this.client = new Client(
            { name: "repo-flow-mcp-client", version: "0.1.0" },
            { capabilities: {} },
        );
        // Inherit the parent's env by default; child also gets PATH so
        // `repo-flow-mcp` is discoverable when installed in the venv.
        const env: Record<string, string> = {};
        for (const [k, v] of Object.entries(process.env)) {
            if (typeof v === "string") env[k] = v;
        }
        if (cfg.env) Object.assign(env, cfg.env);
        this.transport = new StdioClientTransport({
            command: cfg.command,
            args: cfg.args,
            env,
            stderr: "pipe",
        });
    }

    async connect(): Promise<void> {
        if (this.connected) return;
        await this.client.connect(this.transport);
        this.connected = true;
    }

    async listTools(): Promise<McpToolDef[]> {
        const res = await this.client.listTools();
        return res.tools.map((t) => ({
            name: t.name,
            description: t.description ?? "",
            inputSchema: (t.inputSchema as Record<string, unknown>) ?? { type: "object" },
        }));
    }

    /**
     * Call a tool by name. Returns the concatenated text content of the
     * tool's `content` blocks plus an `isError` flag.
     */
    async callTool(
        name: string,
        args: Record<string, unknown>,
    ): Promise<{ text: string; isError: boolean }> {
        const res = await this.client.callTool({ name, arguments: args });
        const blocks = Array.isArray((res as { content?: unknown }).content)
            ? ((res as { content: unknown[] }).content as Array<Record<string, unknown>>)
            : [];
        const parts: string[] = [];
        for (const b of blocks) {
            if (b && (b as { type?: string }).type === "text" && typeof (b as { text?: unknown }).text === "string") {
                parts.push((b as { text: string }).text);
            } else {
                parts.push(JSON.stringify(b));
            }
        }
        return {
            text: parts.join("\n"),
            isError: Boolean((res as { isError?: boolean }).isError),
        };
    }

    async close(): Promise<void> {
        if (!this.connected) return;
        try {
            await this.client.close();
        } catch {
            // ignore
        }
        this.connected = false;
    }
}
