from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from repo_flow_mcp.graph_builder import (
    artifact_lineage,
    broken_stage_contracts,
    build_graph,
    function_to_script_chains,
    neighborhood,
    repo_entrypoints,
    repo_overview,
    shortest_path,
)
from repo_flow_mcp.interfaces import (
    CodeLocalizerFunctionToScriptResponse,
    CodeLocalizerNeighborhoodResponse,
    RepoLocalizerEntrypointsResponse,
    RepoLocalizerOverviewResponse,
)

mcp = FastMCP("repo-flow-mcp")


@mcp.tool()
def build_flow_graph(path: str, include_hidden: bool = False) -> dict[str, object]:
    """Build a unified code/script/dependency graph for a repository path."""
    graph = build_graph(path, include_hidden=include_hidden)
    return graph.to_dict()


@mcp.tool()
def get_upstream_downstream(path: str, node_id: str, depth: int = 2) -> dict[str, object]:
    """Return upstream/downstream neighborhoods for a node."""
    graph = build_graph(path)
    return neighborhood(graph, node_id=node_id, depth=max(1, depth))


@mcp.tool()
def trace_artifact_lineage(path: str, artifact: str) -> dict[str, object]:
    """Trace producers and consumers for an artifact label."""
    graph = build_graph(path)
    return artifact_lineage(graph, artifact)


@mcp.tool()
def find_broken_stage_contracts(path: str) -> dict[str, object]:
    """Detect likely producer/consumer contract mismatches for artifacts."""
    graph = build_graph(path)
    return broken_stage_contracts(graph)


@mcp.tool()
def explain_runbook_path(path: str, start_node_id: str, end_node_id: str) -> dict[str, object]:
    """Explain a shortest dependency/execution path between two nodes."""
    graph = build_graph(path)
    return shortest_path(graph, start_node_id, end_node_id)


@mcp.tool()
def repo_localizer_overview(path: str, include_hidden: bool = False, top_k: int = 25) -> dict[str, object]:
    """TUI repo-localizer interface: repository summary and high-signal files."""
    graph = build_graph(path, include_hidden=include_hidden)
    payload = repo_overview(graph, top_k=max(1, top_k))
    return RepoLocalizerOverviewResponse.model_validate(payload).model_dump()


@mcp.tool()
def repo_localizer_entrypoints(path: str, include_hidden: bool = False, limit: int = 50) -> dict[str, object]:
    """TUI repo-localizer interface: scripts, targets, and workflow entrypoints."""
    graph = build_graph(path, include_hidden=include_hidden)
    payload = repo_entrypoints(graph, limit=max(1, limit))
    return RepoLocalizerEntrypointsResponse.model_validate(payload).model_dump()


@mcp.tool()
def code_localizer_function_to_script(path: str, function_query: str, limit: int = 10) -> dict[str, object]:
    """TUI code-localizer interface: trace function matches to invoking scripts via file bridges."""
    graph = build_graph(path)
    payload = function_to_script_chains(graph, function_query=function_query, limit=max(1, limit))
    return CodeLocalizerFunctionToScriptResponse.model_validate(payload).model_dump()


@mcp.tool()
def code_localizer_node_context(path: str, node_id: str, depth: int = 2) -> dict[str, object]:
    """TUI code-localizer interface: bounded node neighborhood for focused analysis."""
    graph = build_graph(path)
    payload = neighborhood(graph, node_id=node_id, depth=max(1, depth))
    return CodeLocalizerNeighborhoodResponse.model_validate(payload).model_dump()


def run_server() -> None:
    mcp.run()
