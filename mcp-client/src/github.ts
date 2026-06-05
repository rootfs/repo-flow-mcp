import { execFileSync } from "node:child_process";
import { Octokit } from "@octokit/rest";

// ---------------------------------------------------------------------------
// Auth + coord
// ---------------------------------------------------------------------------

/** Resolve a token from env or `gh auth token`. */
export function getGithubToken(): string {
    const env =
        process.env.GITHUB_TOKEN ??
        process.env.GH_TOKEN ??
        process.env.MCP_REVIEW_GITHUB_TOKEN;
    if (env && env.trim()) return env.trim();
    try {
        const tok = execFileSync("gh", ["auth", "token"], {
            encoding: "utf8",
            stdio: ["ignore", "pipe", "ignore"],
        }).trim();
        if (tok) return tok;
    } catch {
        // gh not installed / not logged in
    }
    throw new Error(
        "no GitHub token. Set GITHUB_TOKEN (or GH_TOKEN, MCP_REVIEW_GITHUB_TOKEN) or run `gh auth login`.",
    );
}

export function makeOctokit(token = getGithubToken()): Octokit {
    return new Octokit({ auth: token, userAgent: "repo-flow-mcp-client" });
}

export interface RepoCoord {
    owner: string;
    repo: string;
    number: number;
}

export function resolveCoord(input: string, cwd: string): RepoCoord {
    const trimmed = input.trim();
    const url = trimmed.match(
        /^https?:\/\/github\.com\/([^/]+)\/([^/]+)\/(?:pull|issues)\/(\d+)/i,
    );
    if (url) return { owner: url[1], repo: url[2], number: Number(url[3]) };
    const slash = trimmed.match(/^([^/]+)\/([^#]+)#(\d+)$/);
    if (slash) return { owner: slash[1], repo: slash[2], number: Number(slash[3]) };
    if (/^\d+$/.test(trimmed)) {
        const remote = readRepoFromRemote(cwd);
        if (!remote)
            throw new Error(
                "bare PR number given but cwd is not a github.com checkout",
            );
        return { ...remote, number: Number(trimmed) };
    }
    throw new Error(`could not parse "${input}" as a github coordinate`);
}

function readRepoFromRemote(cwd: string): { owner: string; repo: string } | null {
    try {
        const url = execFileSync(
            "git",
            ["-C", cwd, "config", "--get", "remote.origin.url"],
            { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
        ).trim();
        const ssh = url.match(/^git@github\.com:([^/]+)\/(.+?)(?:\.git)?$/);
        if (ssh) return { owner: ssh[1], repo: ssh[2] };
        const https = url.match(/^https?:\/\/github\.com\/([^/]+)\/(.+?)(?:\.git)?$/);
        if (https) return { owner: https[1], repo: https[2] };
    } catch {
        // not a git repo
    }
    return null;
}

// ---------------------------------------------------------------------------
// PR + files
// ---------------------------------------------------------------------------

export interface PullRequestSummary {
    number: number;
    title: string;
    body: string;
    state: string;
    isDraft: boolean;
    author: string;
    baseRef: string;
    baseSha: string;
    headRef: string;
    headSha: string;
    headRepoFullName: string;
    isFork: boolean;
    additions: number;
    deletions: number;
    changedFiles: number;
}

export interface PullFileEntry {
    filename: string;
    status: string;
    additions: number;
    deletions: number;
    patch?: string;
}

export async function fetchPullRequest(
    octokit: Octokit,
    coord: RepoCoord,
): Promise<PullRequestSummary> {
    const { data } = await octokit.pulls.get({
        owner: coord.owner,
        repo: coord.repo,
        pull_number: coord.number,
    });
    const baseRepoFull = `${data.base.repo.owner.login}/${data.base.repo.name}`;
    const headRepoFull = data.head.repo
        ? `${data.head.repo.owner.login}/${data.head.repo.name}`
        : "(deleted)";
    return {
        number: data.number,
        title: data.title,
        body: data.body ?? "",
        state: data.state,
        isDraft: !!data.draft,
        author: data.user?.login ?? "?",
        baseRef: data.base.ref,
        baseSha: data.base.sha,
        headRef: data.head.ref,
        headSha: data.head.sha,
        headRepoFullName: headRepoFull,
        isFork: headRepoFull !== baseRepoFull,
        additions: data.additions ?? 0,
        deletions: data.deletions ?? 0,
        changedFiles: data.changed_files ?? 0,
    };
}

export async function fetchPullFiles(
    octokit: Octokit,
    coord: RepoCoord,
    limit: number,
): Promise<PullFileEntry[]> {
    const out: PullFileEntry[] = [];
    let page = 1;
    while (out.length < limit) {
        const { data } = await octokit.pulls.listFiles({
            owner: coord.owner,
            repo: coord.repo,
            pull_number: coord.number,
            per_page: 100,
            page,
        });
        if (data.length === 0) break;
        for (const f of data) {
            out.push({
                filename: f.filename,
                status: f.status,
                additions: f.additions,
                deletions: f.deletions,
                patch: f.patch,
            });
            if (out.length >= limit) break;
        }
        if (data.length < 100) break;
        page++;
    }
    return out;
}

/** Fetch the raw PR diff via the GitHub API (`Accept: application/vnd.github.v3.diff`). */
export async function fetchPullDiff(
    octokit: Octokit,
    coord: RepoCoord,
): Promise<string> {
    const res = await octokit.request("GET /repos/{owner}/{repo}/pulls/{pull_number}", {
        owner: coord.owner,
        repo: coord.repo,
        pull_number: coord.number,
        mediaType: { format: "diff" },
    });
    return String(res.data ?? "");
}

// ---------------------------------------------------------------------------
// Issue comments (PR conversation)
// ---------------------------------------------------------------------------

export interface IssueComment {
    author: string;
    createdAt: string;
    body: string;
}

export async function fetchIssueComments(
    octokit: Octokit,
    coord: RepoCoord,
    limit: number,
): Promise<IssueComment[]> {
    const out: IssueComment[] = [];
    let page = 1;
    while (out.length < limit) {
        const { data } = await octokit.issues.listComments({
            owner: coord.owner,
            repo: coord.repo,
            issue_number: coord.number,
            per_page: 100,
            page,
        });
        if (data.length === 0) break;
        for (const c of data) {
            out.push({
                author: c.user?.login ?? "?",
                createdAt: c.created_at,
                body: c.body ?? "",
            });
            if (out.length >= limit) break;
        }
        if (data.length < 100) break;
        page++;
    }
    return out;
}

// ---------------------------------------------------------------------------
// Post review
// ---------------------------------------------------------------------------

export interface InlineComment {
    path: string;
    line: number;
    side: "RIGHT" | "LEFT";
    body: string;
    startLine?: number;
    startSide?: "RIGHT" | "LEFT";
}

export async function postReview(
    octokit: Octokit,
    coord: RepoCoord,
    body: string,
    event: "COMMENT" | "APPROVE" | "REQUEST_CHANGES" = "COMMENT",
    inline: InlineComment[] = [],
): Promise<{ id: number; htmlUrl: string }> {
    const { data } = await octokit.pulls.createReview({
        owner: coord.owner,
        repo: coord.repo,
        pull_number: coord.number,
        body,
        event,
        comments: inline.length
            ? inline.map((c) => ({
                path: c.path,
                line: c.line,
                side: c.side,
                body: c.body,
                ...(c.startLine ? { start_line: c.startLine } : {}),
                ...(c.startSide ? { start_side: c.startSide } : {}),
            }))
            : undefined,
    });
    return { id: data.id, htmlUrl: data.html_url };
}
