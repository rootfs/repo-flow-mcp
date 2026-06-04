from __future__ import annotations

from pathlib import Path

import pytest

from repo_flow_mcp.graph_cache import clear_cache
from repo_flow_mcp.server import (
    code_localizer_function_to_script_batch,
    code_localizer_node_context_batch,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "repo_sample"


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def test_function_to_script_batch_happy_path() -> None:
    result = code_localizer_function_to_script_batch(
        path=str(FIXTURE_ROOT),
        function_queries=["main", "build"],
    )
    assert result["queries"] == 2
    assert isinstance(result["results"], list)
    assert {r["query"] for r in result["results"]} == {"main", "build"}
    for r in result["results"]:
        assert "matches" in r
        assert isinstance(r["matches"], list)
    assert result["truncated"] is False
    assert result["total_matches"] == sum(len(r["matches"]) for r in result["results"])


def test_function_to_script_batch_dedups_and_skips_blanks() -> None:
    result = code_localizer_function_to_script_batch(
        path=str(FIXTURE_ROOT),
        function_queries=["main", "main", "  ", "", "main"],
    )
    assert result["queries"] == 1
    assert result["results"][0]["query"] == "main"


def test_function_to_script_batch_truncates_on_total_cap() -> None:
    result = code_localizer_function_to_script_batch(
        path=str(FIXTURE_ROOT),
        function_queries=["main", "build", "run", "test"],
        limit_per_query=5,
        max_total_matches=1,
    )
    assert result["total_matches"] <= 1
    # With cap=1, at most one query yields matches before we break.
    assert result["truncated"] is True or result["total_matches"] == 1


def test_node_context_batch_happy_path_and_dedup() -> None:
    # Pull a couple of real node ids from the fixture graph via the cache.
    from repo_flow_mcp.graph_cache import get_graph

    graph = get_graph(str(FIXTURE_ROOT))
    node_ids = list(graph.nodes.keys())[:2]
    assert len(node_ids) == 2

    result = code_localizer_node_context_batch(
        path=str(FIXTURE_ROOT),
        node_ids=[node_ids[0], node_ids[1], node_ids[0], "  "],
        depth=1,
    )
    assert result["queries"] == 2
    returned = {r["node_id"] for r in result["results"]}
    assert returned == set(node_ids)
    for r in result["results"]:
        assert "neighborhood" in r
        n = r["neighborhood"]
        assert "upstream" in n and "downstream" in n
