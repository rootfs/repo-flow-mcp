from __future__ import annotations

from pathlib import Path

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


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "repo_sample"


def test_build_graph_contains_expected_layers() -> None:
    graph = build_graph(str(FIXTURE_ROOT))
    payload = graph.to_dict()

    kinds = {node["kind"] for node in payload["nodes"]}
    assert "code_symbol" in kinds
    assert "script" in kinds
    assert "target" in kinds
    assert "workflow_job" in kinds
    assert "artifact" in kinds


def test_artifact_lineage_and_contracts() -> None:
    graph = build_graph(str(FIXTURE_ROOT))

    lineage = artifact_lineage(graph, "dist/app.tar.gz")
    assert lineage["consumers"]

    contracts = broken_stage_contracts(graph)
    assert "missing_producer" in contracts
    assert "missing_consumer" in contracts


def test_neighborhood_and_shortest_path() -> None:
    graph = build_graph(str(FIXTURE_ROOT))
    node_ids = sorted(graph.nodes.keys())
    assert node_ids

    sample = node_ids[0]
    n = neighborhood(graph, sample, depth=2)
    assert "upstream" in n
    assert "downstream" in n

    result = shortest_path(graph, sample, sample)
    assert result["found"] is True
    assert result["path"] == [sample]


def test_extended_code_import_parsers() -> None:
    graph = build_graph(str(FIXTURE_ROOT))
    payload = graph.to_dict()

    modules = {n["label"] for n in payload["nodes"] if n["kind"] == "module"}
    assert "net/http" in modules
    assert "std::collections::HashMap" in modules
    assert "java.util.List" in modules
    assert "vector" in modules


def test_extended_ci_and_build_parsers() -> None:
    graph = build_graph(str(FIXTURE_ROOT))
    payload = graph.to_dict()

    kinds = {n["kind"] for n in payload["nodes"]}
    assert "workflow" in kinds
    assert "workflow_job" in kinds
    assert "target" in kinds

    edges = {e["kind"] for e in payload["edges"]}
    assert "depends_on" in edges
    assert "invokes" in edges


def test_typescript_symbol_and_call_parsing() -> None:
    graph = build_graph(str(FIXTURE_ROOT))
    payload = graph.to_dict()

    symbols = {n["label"] for n in payload["nodes"] if n["kind"] == "code_symbol"}
    assert "bootstrap" in symbols
    assert "Router" in symbols
    assert "execute" in symbols

    modules = {n["label"] for n in payload["nodes"] if n["kind"] == "module"}
    assert "path" in modules
    assert "fs" in modules

    call_edges = [e for e in payload["edges"] if e["kind"] == "calls"]
    assert any(edge["target"].endswith("execute") for edge in call_edges)


def test_markdown_dependency_and_tool_parsing() -> None:
    graph = build_graph(str(FIXTURE_ROOT))
    payload = graph.to_dict()

    file_nodes = {n["path"] for n in payload["nodes"] if n["kind"] == "file" and n.get("path")}
    assert "docs/SKILL.md" in file_nodes
    assert "docs/../.github/workflows/ci.yml" in file_nodes

    modules = {n["label"] for n in payload["nodes"] if n["kind"] == "module"}
    assert "tool:grep" in modules
    assert "tool:terminal" in modules
    assert "tool:openaiDeveloperDocs" in modules


def test_localizer_interfaces_helpers() -> None:
    graph = build_graph(str(FIXTURE_ROOT))

    overview = repo_overview(graph, top_k=5)
    assert overview["stats"]["node_count"] > 0
    assert "file" in overview["node_kinds"]

    entry = repo_entrypoints(graph, limit=20)
    assert entry["scripts"]
    assert entry["targets"]


def test_function_to_script_chain() -> None:
    graph = build_graph(str(FIXTURE_ROOT))
    chains = function_to_script_chains(graph, function_query="main", limit=10)
    assert chains["query"] == "main"
    assert isinstance(chains["matches"], list)
    assert chains["matches"]
