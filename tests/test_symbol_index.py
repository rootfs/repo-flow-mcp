from __future__ import annotations

from pathlib import Path

import pytest

from repo_flow_mcp.graph_cache import clear_cache, get_graph, get_symbol_index
from repo_flow_mcp.symbol_index import SymbolIndex

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "repo_sample"


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def test_symbol_index_finds_symbol_by_exact_token() -> None:
    graph = get_graph(str(FIXTURE_ROOT))
    index = get_symbol_index(str(FIXTURE_ROOT))

    matches = index.search("main")
    assert matches, "expected at least one match for 'main'"
    for sid in matches:
        assert sid.startswith("code_symbol:")
        assert sid in graph.nodes


def test_symbol_index_supports_prefix_match() -> None:
    index = SymbolIndex()
    # Drop directly-shaped rows so we don't need a real graph.
    index._conn.executemany(
        "INSERT INTO symbols(symbol_id, label, file) VALUES (?, ?, ?)",
        [
            ("code_symbol:a.py:build_graph", "build_graph", "a.py"),
            ("code_symbol:b.py:build_router", "build_router", "b.py"),
            ("code_symbol:c.py:run", "run", "c.py"),
        ],
    )

    # Prefix match: "build" finds both build_* symbols.
    assert set(index.search("build")) == {
        "code_symbol:a.py:build_graph",
        "code_symbol:b.py:build_router",
    }
    # AND-of-tokens: both "build" and "graph" must be present, so only
    # build_graph qualifies.
    assert index.search("build_graph") == ["code_symbol:a.py:build_graph"]
    # Empty / non-tokenizable queries return nothing.
    assert index.search("") == []
    assert index.search("   ") == []
    index.close()


def test_symbol_index_returns_empty_for_unknown_token() -> None:
    index = get_symbol_index(str(FIXTURE_ROOT))
    assert index.search("zzz_definitely_not_a_symbol_xyz") == []
