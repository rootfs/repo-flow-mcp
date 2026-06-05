"""Repo-flow-mcp tool surface.

Three consolidated MCP tools, each with a discriminator flag:

* ``repo_localizer(view=...)``  — repo-wide views (overview, entrypoints).
* ``code_localizer(op=...)``    — graph queries (trace, context, path,
  artifact, contracts).
* ``doc_localizer(op=...)``     — doc-prose queries (search, resolve_link).

The flags replace what used to be 14 separate tools with overlapping
descriptions. Keeping the surface tight makes tool selection easier
for the agent and the per-op argument shapes more uniform for parsing.
Required arguments are validated at the entry of each tool with a
clear ``ValueError`` so the LLM gets a precise error rather than a
silent empty result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Callable, Literal

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


def _require(value: object, name: str, view_or_op: str) -> None:
    """Raise a clear error if a discriminator-required argument is missing."""
    if value is None or (isinstance(value, (list, str)) and not value):
        raise ValueError(
            f"`{name}` is required for `{view_or_op}` (got empty/None)"
        )


# ---------------------------------------------------------------------------
# Tool 1: repo_localizer — repo-wide views
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "PRIMARY repo-orientation tool. Returns one of two repo-wide views, "
        "selected by `view`:\n"
        "  - `overview` (default): subsystem layout, node-kind counts, top "
        "files ranked by graph fan-in/out. Call FIRST when scoping a PR or "
        "exploring an unfamiliar repo. Replaces `bash ls` + reading README + "
        "wide grep for orientation.\n"
        "  - `entrypoints`: CLI mains, server boots, Make targets, shell "
        "scripts, and CI/workflow jobs in one call. Replaces "
        "`grep -r 'def main'`, `find . -name Makefile`, and ad-hoc `view` of "
        "`.github/workflows/*`.\n\n"
        "`top_k` applies to `overview`; `limit` applies to `entrypoints`."
    )
)
def repo_localizer(
    path: Annotated[
        str,
        Field(description=_PATH_DESC, examples=[".", "/home/user/myrepo"]),
    ],
    view: Annotated[
        Literal["overview", "entrypoints"],
        Field(description="Which repo-wide view to return."),
    ] = "overview",
    include_hidden: Annotated[
        bool,
        Field(
            description="Include hidden files/directories (dotfiles). Turn on only when CI dotfiles are part of the question.",
        ),
    ] = False,
    top_k: Annotated[
        int,
        Field(
            description="`overview` only: how many high-signal files to return (ranked by graph centrality). 25 is a good default.",
            ge=1,
            le=200,
        ),
    ] = 25,
    limit: Annotated[
        int,
        Field(
            description="`entrypoints` only: max entrypoints across scripts, targets, and workflows.",
            ge=1,
            le=500,
        ),
    ] = 50,
) -> dict[str, object]:
    graph = get_graph(path, include_hidden=include_hidden)
    if view == "overview":
        payload = repo_overview(graph, top_k=max(1, top_k))
        return RepoLocalizerOverviewResponse.model_validate(payload).model_dump()
    # view == "entrypoints"
    payload = repo_entrypoints(graph, limit=max(1, limit))
    return RepoLocalizerEntrypointsResponse.model_validate(payload).model_dump()


# ---------------------------------------------------------------------------
# Tool 2: code_localizer — graph queries
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Graph queries against the repo's code/script/workflow graph. The "
        "`op` flag picks the operation; only the arguments named for that op "
        "are required.\n\n"
        "  - `trace` (most common): trace function/method names OR runner "
        "labels (Make targets, scripts, GHA workflow/job/step) to their "
        "connected runners. Pass `queries=[...]` (one element = old "
        "single-symbol behaviour). Each match has a `match_kind`: "
        "`code_symbol_chain` (legacy { function, script_source, "
        "invoked_script, bridge, command }), or `ci_runner` / `build_target` "
        "/ `script` / `module` (each with `node` + `incoming[]` / "
        "`outgoing[]` 1-hop edges including the `command` from invokes "
        "edges). SCOPE: defined symbols and runner labels are indexed; "
        "struct fields, top-level const/var, and type aliases are not — "
        "fall back to `grep` for those.\n"
        "  - `context`: expand 1+ node ids into their upstream/downstream "
        "neighborhood. Pass `node_ids=[...]` and optional `depth`. Use "
        "AFTER another op surfaced ids worth expanding — never as the first "
        "call.\n"
        "  - `path`: shortest dependency/execution path between two known "
        "node ids. Pass `start_node_id` and `end_node_id`.\n"
        "  - `artifact`: trace producers/consumers for a build artifact "
        "label. Pass `artifact`.\n"
        "  - `contracts`: detect producer/consumer mismatches across the "
        "whole graph (artifact consumed by stage B but never produced, or "
        "produced but never consumed). No extra args.\n"
    )
)
def code_localizer(
    path: Annotated[
        str,
        Field(description=_PATH_DESC, examples=[".", "/home/user/myrepo"]),
    ],
    op: Annotated[
        Literal["trace", "context", "path", "artifact", "contracts"],
        Field(description="Which graph query to run."),
    ] = "trace",
    queries: Annotated[
        list[str] | None,
        Field(
            description="`trace` only: function/method names or runner labels (one per changed symbol). One element behaves like the old single-symbol tool.",
            examples=[["build_graph", "publish", "release.yml"]],
            max_length=64,
        ),
    ] = None,
    node_ids: Annotated[
        list[str] | None,
        Field(
            description="`context` only: node ids from a previous response. Duplicates are deduped.",
            max_length=64,
        ),
    ] = None,
    depth: Annotated[
        int,
        Field(
            description="`context` only: hops to expand. 2 is the review sweet spot.",
            ge=1,
            le=5,
        ),
    ] = 2,
    start_node_id: Annotated[
        str | None,
        Field(description="`path` only: source node id."),
    ] = None,
    end_node_id: Annotated[
        str | None,
        Field(description="`path` only: destination node id."),
    ] = None,
    artifact: Annotated[
        str | None,
        Field(
            description="`artifact` only: artifact label (image tag, binary name, generated path).",
            examples=["myapp:latest", "dist/app", "build/output.json"],
        ),
    ] = None,
    limit_per_query: Annotated[
        int,
        Field(
            description="`trace` only: max chains per query.",
            ge=1,
            le=50,
        ),
    ] = 5,
    max_total_matches: Annotated[
        int,
        Field(
            description="`trace` only: hard cap on total matches across all queries.",
            ge=1,
            le=500,
        ),
    ] = 60,
) -> dict[str, object]:
    graph = get_graph(path)

    if op == "trace":
        _require(queries, "queries", "op=trace")
        assert queries is not None  # for type-checkers
        index = get_symbol_index(path)
        results: list[dict[str, object]] = []
        total = 0
        truncated = False
        seen: set[str] = set()
        for raw_query in queries:
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
            validated = CodeLocalizerFunctionToScriptResponse.model_validate(
                payload
            ).model_dump()
            matches = validated.get("matches", [])
            assert isinstance(matches, list)
            total += len(matches)
            results.append({"query": query, **validated})
        return {
            "op": "trace",
            "queries": len(results),
            "results": results,
            "total_matches": total,
            "truncated": truncated,
        }

    if op == "context":
        _require(node_ids, "node_ids", "op=context")
        assert node_ids is not None
        results = []
        seen = set()
        for raw_id in node_ids:
            node_id = (raw_id or "").strip()
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            payload = neighborhood(graph, node_id=node_id, depth=max(1, depth))
            validated = CodeLocalizerNeighborhoodResponse.model_validate(
                payload
            ).model_dump()
            results.append({"node_id": node_id, "neighborhood": validated})
        return {"op": "context", "queries": len(results), "results": results}

    if op == "path":
        _require(start_node_id, "start_node_id", "op=path")
        _require(end_node_id, "end_node_id", "op=path")
        assert start_node_id is not None and end_node_id is not None
        return {"op": "path", **shortest_path(graph, start_node_id, end_node_id)}

    if op == "artifact":
        _require(artifact, "artifact", "op=artifact")
        assert artifact is not None
        return {"op": "artifact", **artifact_lineage(graph, artifact)}

    # op == "contracts"
    return {"op": "contracts", **broken_stage_contracts(graph)}


# ---------------------------------------------------------------------------
# Tool 3: doc_localizer — doc-prose queries
# ---------------------------------------------------------------------------

def _resolve_doc_link(
    repo_root: Path,
    has_path: Callable[[str], bool],
    source_file: str,
    target: str,
) -> dict[str, object]:
    """Return the resolution result for a relative doc link.

    Pulled out of the tool function to keep the dispatcher readable and
    so future formats (e.g. mdx imports) can call it directly.
    """
    raw = target.split("#", maxsplit=1)[0].strip()
    if not raw:
        return {"resolved": False, "reason": "empty target", "candidates": []}

    src_dir = Path(source_file).parent
    if raw.startswith("/"):
        candidate = Path(raw.lstrip("/"))
    else:
        candidate = src_dir / raw

    try:
        normalized_path = Path(candidate.as_posix())
        parts: list[str] = []
        for part in normalized_path.parts:
            if part == "..":
                if parts:
                    parts.pop()
            elif part in ("", "."):
                continue
            else:
                parts.append(part)
        normalized = "/".join(parts)
    except ValueError:
        return {"resolved": False, "reason": "invalid path", "candidates": []}

    candidates: list[str] = [normalized]
    if "." not in Path(normalized).name:
        candidates.extend(
            f"{normalized}{ext}" for ext in (".md", ".mdx", ".rst", "/index.md")
        )

    for cand in candidates:
        if has_path(cand):
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
        "Doc-prose queries against the repo's markdown / mdx / rst / txt "
        "files (one row per heading-delimited section, BM25 ranked). The "
        "`op` flag picks the operation:\n\n"
        "  - `search` (default): BM25 full-text search. Use for DOCS-only "
        "review questions that grep can't rank — \"which existing pages "
        "discuss the same concepts\", \"where else is term X explained\", "
        "\"which sibling pages should cross-link to this section\". Pass "
        "`query` (free text or a single term — porter-stemmed so "
        "'calibrate' matches 'calibration'). Optional `path_glob` (SQLite "
        "GLOB, e.g. 'docs/*.md') scopes results. Returns each hit as "
        "{ path, section, start_line, snippet, score } (lower score = "
        "better). For literal-token lookups (exact paths, exact identifier "
        "names) prefer plain `grep`.\n"
        "  - `resolve_link`: verify a relative doc link resolves to a real "
        "file. Pass `source_file` (the doc containing the link) and "
        "`target` (the link as written, e.g. './foo' or '../bar.md'; "
        "trailing #anchors are stripped). Catches broken `[text](path)` "
        "references that markdownlint won't.\n"
    )
)
def doc_localizer(
    path: Annotated[str, Field(description=_PATH_DESC)],
    op: Annotated[
        Literal["search", "resolve_link"],
        Field(description="Which doc query to run."),
    ] = "search",
    query: Annotated[
        str | None,
        Field(
            description="`search` only: free-text query or single term.",
            max_length=2000,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description="`search` only: top-k passages to return.",
            ge=1,
            le=100,
        ),
    ] = 10,
    path_glob: Annotated[
        str | None,
        Field(
            description="`search` only: optional SQLite GLOB to scope results (e.g. 'docs/*.md').",
        ),
    ] = None,
    source_file: Annotated[
        str | None,
        Field(
            description="`resolve_link` only: repo-relative path of the doc containing the link.",
            examples=["website/docs/tutorials/embedding.md"],
        ),
    ] = None,
    target: Annotated[
        str | None,
        Field(
            description="`resolve_link` only: link target as written.",
            examples=["./embedding-design-principles", "../foo.yaml"],
            max_length=500,
        ),
    ] = None,
) -> dict[str, object]:
    docs = get_doc_index(path)

    if op == "search":
        _require(query, "query", "op=search")
        assert query is not None
        matches = docs.search(query, limit=max(1, limit), path_glob=path_glob)
        return {"op": "search", "query": query, "matches": matches}

    # op == "resolve_link"
    _require(source_file, "source_file", "op=resolve_link")
    _require(target, "target", "op=resolve_link")
    assert source_file is not None and target is not None
    repo_root = Path(path).resolve()
    payload = _resolve_doc_link(repo_root, docs.has_path, source_file, target)
    return {"op": "resolve_link", **payload}


# ---------------------------------------------------------------------------
# Tool 4: pr_workspace — materialize a PR's working tree on disk
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Materialize a PR's working tree on disk and return its path so the "
        "other localizer tools can be pointed at it. Backed by the shared "
        "worktree + graph cache, so a re-run for the same (repo, base_sha) "
        "is ~free and a re-run for the exact same (repo, base_sha, diff) "
        "also reuses the previously built graph.\n\n"
        "Workflow:\n"
        "  1. Fetch / reuse the base tree at ``base_sha`` (tarball when the "
        "remote is github.com, ``git clone`` otherwise).\n"
        "  2. CoW-copy it into a scratch directory (reflink when supported).\n"
        "  3. Apply ``diff_text`` with ``git apply`` (skipped when empty).\n"
        "  4. Return the resulting path.\n\n"
        "Callers can then issue ``repo_localizer(path=...)`` / "
        "``code_localizer(path=...)`` / ``doc_localizer(path=...)`` against "
        "the returned path. The worktree persists across calls until LRU "
        "eviction; don't delete it manually."
    )
)
def pr_workspace(
    repo_url: Annotated[
        str,
        Field(
            description="HTTPS or SSH URL of the remote, e.g. https://github.com/owner/repo.git.",
        ),
    ],
    base_sha: Annotated[
        str,
        Field(
            description="The full 40-char commit SHA the PR is branched from.",
            min_length=12,
            max_length=40,
        ),
    ],
    diff_text: Annotated[
        str,
        Field(
            description="Unified-diff text to apply on top of ``base_sha``. Pass an empty string to materialize the base tree only.",
        ),
    ] = "",
) -> dict[str, object]:
    from repo_flow_mcp import worktree_cache
    from repo_flow_mcp.graph_persistence import _cache_root
    import hashlib
    import subprocess

    base = worktree_cache.get_base_worktree(repo_url, base_sha)
    diff_text = diff_text or ""

    if not diff_text.strip():
        return {
            "worktree_path": str(base),
            "base_sha": base_sha,
            "diff_sha": None,
            "applied_diff": False,
        }

    # The overlay scratch dir is keyed by (base_sha, diff_sha) so two
    # callers with the same payload share a single materialization.
    diff_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()[:16]
    scratch = _cache_root() / "overlays" / f"{base_sha[:12]}-{diff_sha}"

    if not (scratch / ".repo_flow_overlay_complete").exists():
        worktree_cache.materialize_overlay(base, scratch)
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=str(scratch),
            input=diff_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            # Initialize a tiny git repo so --3way can run, then retry.
            subprocess.run(
                ["git", "init", "-q"], cwd=str(scratch), check=False, capture_output=True
            )
            subprocess.run(
                ["git", "add", "-A"], cwd=str(scratch), check=False, capture_output=True
            )
            subprocess.run(
                ["git", "-c", "user.email=mcp@invalid", "-c", "user.name=mcp", "commit", "-q", "-m", "base"],
                cwd=str(scratch),
                check=False,
                capture_output=True,
            )
            proc = subprocess.run(
                ["git", "apply", "--3way", "--whitespace=nowarn", "-"],
                cwd=str(scratch),
                input=diff_text,
                text=True,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"failed to apply diff to overlay: {(proc.stderr or proc.stdout).strip()}"
                )
        (scratch / ".repo_flow_overlay_complete").write_text(diff_sha, encoding="utf-8")

    return {
        "worktree_path": str(scratch),
        "base_sha": base_sha,
        "diff_sha": diff_sha,
        "applied_diff": True,
    }


def run_server() -> None:
    mcp.run()
