from __future__ import annotations

from pathlib import Path

import pytest

from repo_flow_mcp.graph_cache import clear_cache, get_graph
from repo_flow_mcp.server import code_localizer, doc_localizer, repo_localizer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "repo_sample"


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


# --- code_localizer(op="trace") ---------------------------------------------


def test_trace_happy_path() -> None:
    result = code_localizer(
        path=str(FIXTURE_ROOT),
        op="trace",
        queries=["main", "build"],
    )
    assert result["op"] == "trace"
    assert result["queries"] == 2
    assert isinstance(result["results"], list)
    assert {r["query"] for r in result["results"]} == {"main", "build"}
    for r in result["results"]:
        assert "matches" in r
        assert isinstance(r["matches"], list)
    assert result["truncated"] is False
    assert result["total_matches"] == sum(len(r["matches"]) for r in result["results"])


def test_trace_dedups_and_skips_blanks() -> None:
    result = code_localizer(
        path=str(FIXTURE_ROOT),
        op="trace",
        queries=["main", "main", "  ", "", "main"],
    )
    assert result["queries"] == 1
    assert result["results"][0]["query"] == "main"


def test_trace_truncates_on_total_cap() -> None:
    result = code_localizer(
        path=str(FIXTURE_ROOT),
        op="trace",
        queries=["main", "build", "run", "test"],
        limit_per_query=5,
        max_total_matches=1,
    )
    assert result["total_matches"] <= 1
    assert result["truncated"] is True or result["total_matches"] == 1


def test_trace_requires_queries() -> None:
    with pytest.raises(ValueError, match="queries"):
        code_localizer(path=str(FIXTURE_ROOT), op="trace")


# --- code_localizer(op="context") -------------------------------------------


def test_context_happy_path_and_dedup() -> None:
    graph = get_graph(str(FIXTURE_ROOT))
    node_ids = list(graph.nodes.keys())[:2]
    assert len(node_ids) == 2

    result = code_localizer(
        path=str(FIXTURE_ROOT),
        op="context",
        node_ids=[node_ids[0], node_ids[1], node_ids[0], "  "],
        depth=1,
    )
    assert result["op"] == "context"
    assert result["queries"] == 2
    returned = {r["node_id"] for r in result["results"]}
    assert returned == set(node_ids)
    for r in result["results"]:
        n = r["neighborhood"]
        assert "upstream" in n and "downstream" in n


def test_context_requires_node_ids() -> None:
    with pytest.raises(ValueError, match="node_ids"):
        code_localizer(path=str(FIXTURE_ROOT), op="context")


# --- code_localizer(op="contracts" / "artifact" / "path") -------------------


def test_contracts_returns_payload() -> None:
    result = code_localizer(path=str(FIXTURE_ROOT), op="contracts")
    assert result["op"] == "contracts"


def test_artifact_requires_arg() -> None:
    with pytest.raises(ValueError, match="artifact"):
        code_localizer(path=str(FIXTURE_ROOT), op="artifact")


def test_path_requires_endpoints() -> None:
    with pytest.raises(ValueError, match="start_node_id"):
        code_localizer(path=str(FIXTURE_ROOT), op="path")


# --- repo_localizer ---------------------------------------------------------


def test_repo_localizer_overview() -> None:
    result = repo_localizer(path=str(FIXTURE_ROOT), view="overview")
    assert "node_kinds" in result
    assert "stats" in result


def test_repo_localizer_entrypoints() -> None:
    result = repo_localizer(path=str(FIXTURE_ROOT), view="entrypoints", limit=10)
    assert isinstance(result, dict)


# --- doc_localizer ----------------------------------------------------------


def test_doc_localizer_search_requires_query() -> None:
    with pytest.raises(ValueError, match="query"):
        doc_localizer(path=str(FIXTURE_ROOT), op="search")


def test_doc_localizer_resolve_requires_inputs() -> None:
    with pytest.raises(ValueError, match="source_file"):
        doc_localizer(path=str(FIXTURE_ROOT), op="resolve_link")
