from __future__ import annotations

from pathlib import Path

import pytest

from repo_flow_mcp.graph_builder import function_to_script_chains
from repo_flow_mcp.graph_cache import clear_cache, get_graph, get_symbol_index
from repo_flow_mcp.models import NodeKind

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "repo_sample"


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def test_symbol_index_returns_workflow_kind() -> None:
    """The index now indexes WORKFLOW / WORKFLOW_JOB / WORKFLOW_STEP."""
    index = get_symbol_index(str(FIXTURE_ROOT))
    pairs = index.search_with_kinds("ci", limit=20)
    kinds = {kind for _, kind in pairs}
    assert kinds, "expected at least one match for 'ci'"
    # Workflow `name: ci` produces a WORKFLOW node labeled "ci".
    assert "workflow" in kinds


def test_symbol_index_kinds_filter_excludes_other_kinds() -> None:
    index = get_symbol_index(str(FIXTURE_ROOT))
    only_jobs = index.search(
        "publish",
        limit=20,
        kinds=frozenset({NodeKind.WORKFLOW_JOB}),
    )
    only_scripts = index.search(
        "publish",
        limit=20,
        kinds=frozenset({NodeKind.SCRIPT}),
    )
    # Both kinds exist in the fixture (workflow_job 'publish' and
    # scripts/publish.py); the kind filter must keep them disjoint.
    assert only_jobs, "expected at least one workflow_job match"
    assert only_scripts, "expected at least one script match"
    assert set(only_jobs).isdisjoint(only_scripts)


def test_function_to_script_chains_emits_ci_runner_for_workflow_job() -> None:
    """A workflow-job query should produce a `ci_runner` match with the
    job's incoming/outgoing edges (depends_on the build job, defines
    its step)."""
    graph = get_graph(str(FIXTURE_ROOT))
    index = get_symbol_index(str(FIXTURE_ROOT))
    payload = function_to_script_chains(
        graph,
        function_query="publish",
        limit=10,
        symbol_index=index,
    )
    matches = payload["matches"]
    runner_matches = [m for m in matches if m.get("match_kind") == "ci_runner"]
    assert runner_matches, f"expected ci_runner match, got {matches}"

    job = next(
        m for m in runner_matches
        if m["node"]["kind"] == "workflow_job"
    )
    assert job["node"]["label"] == "publish"
    # publish depends on build (depends_on edge -> outgoing).
    outgoing_kinds = {row["edge_kind"] for row in job["outgoing"]}
    assert "depends_on" in outgoing_kinds or "defines" in outgoing_kinds
    # publish is defined-by its workflow (defines edge -> incoming).
    incoming_kinds = {row["edge_kind"] for row in job["incoming"]}
    assert "defines" in incoming_kinds


def test_function_to_script_chains_preserves_code_symbol_shape() -> None:
    """Existing code-symbol matches must keep all legacy fields so
    downstream parsers don't break."""
    graph = get_graph(str(FIXTURE_ROOT))
    index = get_symbol_index(str(FIXTURE_ROOT))
    payload = function_to_script_chains(
        graph,
        function_query="main",
        limit=10,
        symbol_index=index,
    )
    code_matches = [
        m for m in payload["matches"]
        if m.get("match_kind") == "code_symbol_chain"
    ]
    if not code_matches:
        pytest.skip("fixture has no code_symbol -> script chain for 'main'")
    sample = code_matches[0]
    for key in ("function", "script_source", "invoked_script", "bridge", "command"):
        assert key in sample, f"missing legacy field {key!r}"
    # New uniform field is also present.
    assert sample["node"]["kind"] == "code_symbol"
