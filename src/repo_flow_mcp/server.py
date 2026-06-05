from __future__ import annotations

from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from repo_flow_mcp.graph_builder import (
    artifact_lineage,
    broken_stage_contracts,
    function_to_script_chains,
    neighborhood,
    repo_entrypoints,
    repo_overview,
    shortest_path,
)
from repo_flow_mcp.graph_cache import get_doc_index, get_graph, get_symbol_index
from repo_flow_mcp.interfaces import (
    CodeLocalizerFunctionToScriptResponse,
    CodeLocalizerNeighborhoodResponse,
    RepoLocalizerEntrypointsResponse,
    RepoLocalizerOverviewResponse,
)

mcp = FastMCP("repo-flow-mcp")


_PATH_DESC = (
    "Absolute or relative path to the repository root. Use '.' for the current "
    "working directory. The graph is built once per call and covers code, "
    "scripts, CI/workflow files, Dockerfiles, and Make targets."
)


@mcp.tool(
    description=(
        "PRIMARY repo-localization tool — call FIRST when you need to understand "
        "an unfamiliar repository, locate where changes will land, or pick "
        "high-signal files to read. Returns a compact summary: subsystem layout, "
        "node-kind counts, and the top files ranked by graph fan-in/out (where "
        "code, scripts, workflows, and configs converge). Cheaper and more "
        "structural than running grep/view/glob/bash to map a repo manually — "
        "prefer this over `bash ls`, `view` of README, or wide grep when "
        "orienting on a new codebase or scoping a PR review."
    )
)
def repo_localizer_overview(
    path: Annotated[
        str,
        Field(description=_PATH_DESC, examples=[".", "/home/user/myrepo"]),
    ],
    include_hidden: Annotated[
        bool,
        Field(
            description="Include hidden files/directories (dotfiles). Defaults to false; turn on only when CI dotfiles are part of the question.",
        ),
    ] = False,
    top_k: Annotated[
        int,
        Field(
            description="How many high-signal files to return (ranked by graph centrality). 25 is a good default; raise for large monorepos, lower for quick orientation.",
            ge=1,
            le=200,
        ),
    ] = 25,
) -> dict[str, object]:
    graph = get_graph(path, include_hidden=include_hidden)
    payload = repo_overview(graph, top_k=max(1, top_k))
    return RepoLocalizerOverviewResponse.model_validate(payload).model_dump()


@mcp.tool(
    description=(
        "Repository entrypoints in ONE call: CLI mains, server boots, Make targets, "
        "shell scripts, and CI/workflow jobs. Use this AFTER `repo_localizer_overview` "
        "to find where execution begins for a given subsystem, or to discover which "
        "scripts/workflows touch a feature. Replaces `grep -r 'def main'`, "
        "`find . -name Makefile`, and ad-hoc `view` of `.github/workflows/*` — one "
        "tool call instead of a chain of bash/grep/view."
    )
)
def repo_localizer_entrypoints(
    path: Annotated[
        str,
        Field(description=_PATH_DESC, examples=[".", "/home/user/myrepo"]),
    ],
    include_hidden: Annotated[
        bool,
        Field(description="Include hidden files (e.g. CI dotfiles). Defaults to false."),
    ] = False,
    limit: Annotated[
        int,
        Field(
            description="Max entrypoints to return across scripts, targets, and workflows.",
            ge=1,
            le=500,
        ),
    ] = 50,
) -> dict[str, object]:
    graph = get_graph(path, include_hidden=include_hidden)
    payload = repo_entrypoints(graph, limit=max(1, limit))
    return RepoLocalizerEntrypointsResponse.model_validate(payload).model_dump()


@mcp.tool(
    description=(
        "Trace a function/method name OR a runner label (Make target, shell script, "
        "GitHub Actions workflow / job / step) to its connected runners. Use this for "
        "change-impact questions like \"where is X actually called from in production?\", "
        "\"which CI job runs this Make target?\", or \"what does this workflow step "
        "invoke?\". One call returns code-symbol → file → script/workflow chains with "
        "line numbers, OR a runner node with its 1-hop incoming/outgoing edges — saving "
        "a separate `code_localizer_node_context` round-trip."
        "\n\nMatch shapes (per `match_kind`):"
        "\n  - `code_symbol_chain`: legacy shape — { node, function, script_source, "
        "invoked_script, bridge, command }"
        "\n  - `ci_runner` (workflow / workflow_job / workflow_step): "
        "{ node, incoming[], outgoing[] } where each edge row is "
        "{ id, label, kind, path, edge_kind, command? }"
        "\n  - `build_target` (Make target): same incoming/outgoing shape"
        "\n  - `script` (shell script): same incoming/outgoing shape"
        "\n  - `module` (imported library, GHA `uses:`, etc.): same incoming/outgoing shape"
        "\n\nSCOPE: function/method/class names with a body, plus runner labels (Make "
        "target names, workflow / job / step labels, script paths, GHA `uses:` actions) "
        "are indexed. Struct fields, interface members, top-level const/var, type "
        "aliases, and plain identifiers not defined as a symbol are NOT in the graph — "
        "fall back to `grep` for those."
    )
)
def code_localizer_function_to_script(
    path: Annotated[
        str,
        Field(description=_PATH_DESC, examples=[".", "/home/user/myrepo"]),
    ],
    function_query: Annotated[
        str,
        Field(
            description="Function/method name (or partial). Pass the exact symbol changed in a PR for best results.",
            examples=["build_graph", "handle_request", "Session.login"],
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Max chains to return. 10 is plenty for review; raise for impact analysis.",
            ge=1,
            le=100,
        ),
    ] = 10,
) -> dict[str, object]:
    graph = get_graph(path)
    index = get_symbol_index(path)
    payload = function_to_script_chains(
        graph,
        function_query=function_query,
        limit=max(1, limit),
        symbol_index=index,
    )
    return CodeLocalizerFunctionToScriptResponse.model_validate(payload).model_dump()


@mcp.tool(
    description=(
        "Bounded neighborhood (callers, callees, dependencies) around a known node id. "
        "Use this AFTER another tool surfaced a node id you want to expand — never as the "
        "first call. Cheaper and more structural than `view` + `grep` to follow call edges."
    )
)
def code_localizer_node_context(
    path: Annotated[
        str,
        Field(description=_PATH_DESC, examples=[".", "/home/user/myrepo"]),
    ],
    node_id: Annotated[
        str,
        Field(
            description="Node id from another repo-flow tool's response (e.g. an entry from `repo_localizer_overview`).",
        ),
    ],
    depth: Annotated[
        int,
        Field(
            description="How many hops to expand. 2 is the sweet spot for review-context; 1 for tight focus, 3+ for deep impact.",
            ge=1,
            le=5,
        ),
    ] = 2,
) -> dict[str, object]:
    graph = get_graph(path)
    payload = neighborhood(graph, node_id=node_id, depth=max(1, depth))
    return CodeLocalizerNeighborhoodResponse.model_validate(payload).model_dump()


@mcp.tool(
    description=(
        "Build the full unified code/script/dependency graph for a repo. SECONDARY — "
        "prefer `repo_localizer_overview` for navigation; reach for this only when you "
        "need the raw graph (e.g. custom traversal not covered by other tools)."
    )
)
def build_flow_graph(
    path: Annotated[str, Field(description=_PATH_DESC, examples=[".", "/home/user/myrepo"])],
    include_hidden: Annotated[bool, Field(description="Include hidden files.")] = False,
) -> dict[str, object]:
    graph = get_graph(path, include_hidden=include_hidden)
    return graph.to_dict()


@mcp.tool(
    description=(
        "Upstream/downstream neighborhood for a node id. Lower-level alias of "
        "`code_localizer_node_context`; prefer that one for review/localization flows."
    )
)
def get_upstream_downstream(
    path: Annotated[str, Field(description=_PATH_DESC)],
    node_id: Annotated[str, Field(description="Node id from another tool's response.")],
    depth: Annotated[int, Field(description="Hops to expand.", ge=1, le=5)] = 2,
) -> dict[str, object]:
    graph = get_graph(path)
    return neighborhood(graph, node_id=node_id, depth=max(1, depth))


@mcp.tool(
    description=(
        "Trace producers and consumers for a build artifact label (e.g. a binary, "
        "container image, or generated file). Use for supply-chain or release-flow "
        "questions where grep would just hit string matches."
    )
)
def trace_artifact_lineage(
    path: Annotated[str, Field(description=_PATH_DESC)],
    artifact: Annotated[
        str,
        Field(
            description="Artifact name or label (image tag, binary name, generated path).",
            examples=["myapp:latest", "dist/app", "build/output.json"],
        ),
    ],
) -> dict[str, object]:
    graph = get_graph(path)
    return artifact_lineage(graph, artifact)


@mcp.tool(
    description=(
        "Detect likely producer/consumer contract mismatches for artifacts (an artifact "
        "consumed by stage B but never produced by any earlier stage, or produced but "
        "never consumed). Use for CI/release health checks."
    )
)
def find_broken_stage_contracts(
    path: Annotated[str, Field(description=_PATH_DESC)],
) -> dict[str, object]:
    graph = get_graph(path)
    return broken_stage_contracts(graph)


@mcp.tool(
    description=(
        "Shortest dependency/execution path between two known node ids. Use for "
        "\"how does X get triggered by Y?\" questions when you already have both node ids "
        "from another tool — never as a first call."
    )
)
def explain_runbook_path(
    path: Annotated[str, Field(description=_PATH_DESC)],
    start_node_id: Annotated[str, Field(description="Source node id.")],
    end_node_id: Annotated[str, Field(description="Destination node id.")],
) -> dict[str, object]:
    graph = get_graph(path)
    return shortest_path(graph, start_node_id, end_node_id)


@mcp.tool(
    description=(
        "BATCH version of `code_localizer_function_to_script`. Trace MANY function/method "
        "names OR runner labels (Make targets, scripts, GHA workflow/job/step labels) to "
        "their connected runners in ONE call — prefer this over issuing repeated "
        "single-symbol calls when reviewing a PR that changes multiple symbols or CI "
        "definitions. Builds the graph once (cached across calls in the same session) and "
        "returns one entry per query, with a global cap on total chains so the response "
        "stays bounded for large PRs."
        "\n\nEach match has a `match_kind` field: `code_symbol_chain`, `ci_runner`, "
        "`build_target`, `script`, or `module` — see the single-symbol tool for the per-"
        "kind shape."
        "\n\nSCOPE: same as the single-symbol variant. Plain identifiers that are not "
        "defined symbols / runners (struct fields, interface members, top-level "
        "const/var, type aliases) will return 0 matches; fall back to `grep` for those."
    )
)
def code_localizer_function_to_script_batch(
    path: Annotated[
        str,
        Field(description=_PATH_DESC, examples=[".", "/home/user/myrepo"]),
    ],
    function_queries: Annotated[
        list[str],
        Field(
            description="List of function/method names (or partials) to trace, one per changed symbol.",
            examples=[["build_graph", "handle_request", "Session.login"]],
            min_length=1,
            max_length=64,
        ),
    ],
    limit_per_query: Annotated[
        int,
        Field(
            description="Max chains per query. 5 is enough for review fan-in; raise for impact analysis.",
            ge=1,
            le=50,
        ),
    ] = 5,
    max_total_matches: Annotated[
        int,
        Field(
            description="Hard cap on total matches across all queries. Truncates fairly across queries when exceeded.",
            ge=1,
            le=500,
        ),
    ] = 60,
) -> dict[str, object]:
    graph = get_graph(path)
    index = get_symbol_index(path)
    results: list[dict[str, object]] = []
    total = 0
    truncated = False
    seen: set[str] = set()
    for raw_query in function_queries:
        query = (raw_query or "").strip()
        if not query or query in seen:
            continue
        seen.add(query)
        remaining = max_total_matches - total
        if remaining <= 0:
            truncated = True
            break
        per_query_limit = min(max(1, limit_per_query), remaining)
        payload = function_to_script_chains(
            graph,
            function_query=query,
            limit=per_query_limit,
            symbol_index=index,
        )
        validated = CodeLocalizerFunctionToScriptResponse.model_validate(payload).model_dump()
        matches = validated.get("matches") or []
        results.append({"query": query, "matches": matches})
        total += len(matches)
    return {
        "queries": len(results),
        "total_matches": total,
        "truncated": truncated,
        "results": results,
    }


@mcp.tool(
    description=(
        "BATCH version of `code_localizer_node_context`. Expand neighborhoods for MANY "
        "node ids in ONE call. Use after a batch function-to-script call surfaces multiple "
        "node ids worth expanding. Builds the graph once (cached across calls)."
    )
)
def code_localizer_node_context_batch(
    path: Annotated[
        str,
        Field(description=_PATH_DESC, examples=[".", "/home/user/myrepo"]),
    ],
    node_ids: Annotated[
        list[str],
        Field(
            description="Node ids from earlier tool responses. Duplicates are de-duplicated.",
            min_length=1,
            max_length=64,
        ),
    ],
    depth: Annotated[
        int,
        Field(
            description="Hops to expand for each node id.",
            ge=1,
            le=5,
        ),
    ] = 2,
) -> dict[str, object]:
    graph = get_graph(path)
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_id in node_ids:
        node_id = (raw_id or "").strip()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        payload = neighborhood(graph, node_id=node_id, depth=max(1, depth))
        validated = CodeLocalizerNeighborhoodResponse.model_validate(payload).model_dump()
        results.append({"node_id": node_id, "neighborhood": validated})
    return {"queries": len(results), "results": results}


@mcp.tool(
    description=(
        "BM25 full-text search over the repo's documentation prose (markdown / "
        "mdx / rst / txt), one row per heading-delimited section. Use this for "
        "DOCS-only review questions that grep can't rank: \"which existing pages "
        "discuss the same concepts as this new doc\", \"which sibling pages "
        "should cross-link to a renamed section\", \"where else is term X "
        "explained\". Returns each hit as path + section title + start_line + "
        "snippet + bm25 score (lower is better). For literal-token lookups "
        "(exact paths, exact identifier names) prefer plain `grep` — BM25 "
        "doesn't add value there."
    )
)
def doc_localizer_related_pages(
    path: Annotated[str, Field(description=_PATH_DESC)],
    query: Annotated[
        str,
        Field(
            description="Free-text query (e.g. a section title, a paragraph, or a list of related terms). Tokens are stemmed (porter) so 'calibrate' matches 'calibration'.",
            min_length=1,
            max_length=2000,
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Top-k passages to return, ranked by BM25.",
            ge=1,
            le=50,
        ),
    ] = 10,
    path_glob: Annotated[
        str | None,
        Field(
            description="Optional SQLite GLOB to scope results (e.g. 'docs/*.md', 'website/docs/**/*.mdx'). Omit to search the whole repo.",
        ),
    ] = None,
) -> dict[str, object]:
    docs = get_doc_index(path)
    matches = docs.search(query, limit=max(1, limit), path_glob=path_glob)
    return {"query": query, "matches": matches}


@mcp.tool(
    description=(
        "Resolve a relative doc link (e.g. `./embedding-design-principles`, "
        "`../config/foo.yaml`) against the repo's path set. Returns whether "
        "the target exists and the canonical repo-relative path it resolves "
        "to. Use this to verify cross-links in a docs-PR before approving — "
        "catches broken `[text](path)` references that markdownlint won't."
    )
)
def doc_localizer_anchor_resolve(
    path: Annotated[str, Field(description=_PATH_DESC)],
    source_file: Annotated[
        str,
        Field(
            description="Repo-relative path of the file containing the link (used to resolve relative targets).",
            examples=["website/docs/tutorials/embedding.md"],
        ),
    ],
    target: Annotated[
        str,
        Field(
            description="Link target as written in the source file (e.g. './embedding-design-principles', '../foo.yaml', '/website/docs/x'). Trailing anchors (#...) are stripped before resolving.",
            min_length=1,
            max_length=500,
        ),
    ],
) -> dict[str, object]:
    docs = get_doc_index(path)
    repo_root = Path(path).resolve()

    raw = target.split("#", maxsplit=1)[0].strip()
    if not raw:
        return {"resolved": False, "reason": "empty target", "candidates": []}

    src_dir = Path(source_file).parent
    if raw.startswith("/"):
        candidate = Path(raw.lstrip("/"))
    else:
        candidate = (src_dir / raw)

    # Normalize without touching the filesystem so the answer is
    # deterministic for missing files too.
    try:
        normalized = candidate.as_posix()
        normalized_path = Path(normalized)
        parts: list[str] = []
        for part in normalized_path.parts:
            if part == "..":
                if parts:
                    parts.pop()
            elif part == "." or part == "":
                continue
            else:
                parts.append(part)
        normalized = "/".join(parts)
    except ValueError:
        return {"resolved": False, "reason": "invalid path", "candidates": []}

    # Direct hit, then a few common docs-suffix expansions for
    # extension-less wiki-style links.
    candidates: list[str] = [normalized]
    if "." not in Path(normalized).name:
        candidates.extend(
            f"{normalized}{ext}" for ext in (".md", ".mdx", ".rst", "/index.md")
        )

    for cand in candidates:
        if docs.has_path(cand):
            return {
                "resolved": True,
                "canonical": cand,
                "exists_on_disk": (repo_root / cand).exists(),
                "candidates": candidates,
            }
    return {
        "resolved": False,
        "reason": "no path matched",
        "candidates": candidates,
    }


@mcp.tool(
    description=(
        "Find every doc section that mentions a term, ranked by BM25. Like "
        "`grep -n term docs/`, but section-aware: hits land on the heading "
        "that owns the line, with a highlighted snippet. Use this for terms "
        "that have many matches (acronyms, product names, config keys) where "
        "a flat grep dump is too noisy."
    )
)
def doc_localizer_term_usage(
    path: Annotated[str, Field(description=_PATH_DESC)],
    term: Annotated[
        str,
        Field(
            description="Term or short phrase to look up. Stemmed via porter.",
            min_length=1,
            max_length=200,
        ),
    ],
    limit: Annotated[
        int,
        Field(description="Max sections to return.", ge=1, le=100),
    ] = 20,
) -> dict[str, object]:
    docs = get_doc_index(path)
    matches = docs.search(term, limit=max(1, limit))
    return {"term": term, "matches": matches}


def run_server() -> None:
    mcp.run()
