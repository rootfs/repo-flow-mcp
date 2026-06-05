"""Snapshot test that pins the fixture's graph schema.

The repo-flow-mcp graph schema is part of the public contract consumed by the
MCP tools. After the parser hardening pass we lock the per-kind node and
edge counts on the standard fixture so any future change that drops or
adds whole categories of nodes/edges trips a test.

The fixture lives at ``tests/fixtures/repo_sample/``; the expected counts
below match the baseline captured before the regex→tree-sitter migration.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from repo_flow_mcp.graph_builder import build_graph


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "repo_sample"


# Counts captured against the regex-era parsers, so the snapshot also doubles
# as a regression guard that the tree-sitter / markdown-it rewrites are
# functionally equivalent on the canonical fixture.
EXPECTED_NODE_KINDS: dict[str, int] = {
    "file": 13,
    "code_symbol": 29,
    "module": 17,
    "script": 9,
    "target": 10,
    "workflow": 2,
    "workflow_job": 4,
    "workflow_step": 5,
    "artifact": 2,
    "env_var": 1,
    "container_image": 3,
    "service": 2,
}

EXPECTED_EDGE_KINDS: dict[str, int] = {
    "imports": 15,
    "calls": 24,
    "defines": 23,
    "invokes": 10,
    "depends_on": 10,
    "produces": 2,
    "consumes": 4,
    "sets_env": 1,
    "uses_env": 1,
    "runs_in": 2,
    "builds_from": 1,
}


def _kind_counts(items: list[dict[str, str]]) -> dict[str, int]:
    return dict(Counter(item["kind"] for item in items))


def test_fixture_graph_node_kinds_match_baseline() -> None:
    graph = build_graph(str(FIXTURE_ROOT))
    payload = graph.to_dict()
    actual = _kind_counts(payload["nodes"])
    assert actual == EXPECTED_NODE_KINDS, (
        f"node kind drift\nexpected={EXPECTED_NODE_KINDS}\nactual={actual}"
    )


def test_fixture_graph_edge_kinds_match_baseline() -> None:
    graph = build_graph(str(FIXTURE_ROOT))
    payload = graph.to_dict()
    actual = _kind_counts(payload["edges"])
    assert actual == EXPECTED_EDGE_KINDS, (
        f"edge kind drift\nexpected={EXPECTED_EDGE_KINDS}\nactual={actual}"
    )


def test_fixture_graph_has_no_warnings() -> None:
    """No file in the fixture should produce a ParserError-warning today."""

    graph = build_graph(str(FIXTURE_ROOT))
    assert graph.warnings == [], graph.warnings
