"""End-to-end test that drives the MCP tools against a real PR.

The test parametrizes ``(repo_url, pr_number)`` so callers can point it at
any repo + PR they care about. The default cases pin the semantic-router
docs PR (`#2059`) and a code-change PR (`#2061`).

What each test case does:

1. Resolve the PR's base SHA + unified diff (via ``gh``).
2. Shallow-clone the repo at that base SHA.
3. Apply the PR's diff (so the working tree mirrors the PR head).
4. Drive ``repo_localizer`` / ``code_localizer`` / ``doc_localizer`` against
   the resulting tree and assert the responses are non-degenerate.

E2E tests are skipped unless ``RUN_E2E_TESTS=1`` is set, because they need
network access and the ``gh`` / ``git`` CLIs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_flow_mcp.graph_cache import clear_cache
from repo_flow_mcp.server import (
    code_localizer,
    doc_localizer,
    pr_workspace,
    repo_localizer,
)

from ._helpers import PullRequest, fetch_pull_request


# Each case: (repo_url, pr_number, kind) where ``kind`` controls which
# follow-up MCP queries are issued after the graph is built.
PR_CASES: list[tuple[str, int, str]] = [
    ("https://github.com/vllm-project/semantic-router.git", 2059, "docs"),
    ("https://github.com/vllm-project/semantic-router.git", 2061, "code"),
]


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    clear_cache()
    yield
    clear_cache()


def _materialize_pr(
    repo_url: str, pr_number: int
) -> tuple[PullRequest, Path]:
    """Resolve a PR and have ``pr_workspace`` materialize its working tree.

    Uses the new MCP tool surface end-to-end — no test-side clone/apply,
    so this also exercises the shared worktree + overlay caches.
    """

    pr = fetch_pull_request(repo_url, pr_number)
    workspace = pr_workspace(
        repo_url=repo_url,
        base_sha=pr.base_sha,
        diff_text=pr.diff_text,
    )
    return pr, Path(str(workspace["worktree_path"]))


@pytest.mark.e2e
@pytest.mark.parametrize("repo_url,pr_number,kind", PR_CASES)
def test_pr_workflow_drives_mcp(
    repo_url: str, pr_number: int, kind: str, tmp_path: Path
) -> None:
    pr, repo_dir = _materialize_pr(repo_url, pr_number)

    # --- repo_localizer / overview --------------------------------------
    overview = repo_localizer(path=str(repo_dir), view="overview")
    assert overview["stats"]["node_count"] > 0, "graph must have nodes"
    assert overview["stats"]["edge_count"] > 0, "graph must have edges"
    # The default ranker promotes high-fan-in files. Just check we got some.
    assert len(overview["top_files_by_edges"]) > 0

    # --- repo_localizer / entrypoints -----------------------------------
    entrypoints = repo_localizer(path=str(repo_dir), view="entrypoints")
    # Real repos always have at least one of scripts/targets/workflows.
    total_entrypoints = (
        len(entrypoints["scripts"])
        + len(entrypoints["targets"])
        + len(entrypoints["workflows"])
    )
    assert total_entrypoints > 0, "expected at least one entrypoint"

    # --- code_localizer / contracts -------------------------------------
    contracts = code_localizer(path=str(repo_dir), op="contracts")
    assert contracts["op"] == "contracts"
    # Don't assert specific producer/consumer mismatches — those are
    # repo-dependent. Just confirm the response shape is healthy.
    assert "missing_producer" in contracts
    assert "missing_consumer" in contracts

    # --- kind-specific follow-up ----------------------------------------
    if kind == "docs":
        # For a docs PR, every changed .md file should be searchable by
        # filename via doc_localizer (proves the new content was indexed).
        md_files = [f for f in pr.changed_files if f.endswith(".md")]
        assert md_files, f"PR {pr_number} is tagged 'docs' but has no .md files"

        for md_path in md_files:
            stem = Path(md_path).stem
            if not stem:
                continue
            result = doc_localizer(
                path=str(repo_dir),
                op="search",
                query=stem.replace("-", " "),
                limit=20,
            )
            matched_paths = {m.get("path") for m in result.get("matches", [])}
            assert any(
                md_path in (p or "") for p in matched_paths
            ), (
                f"doc_localizer didn't surface {md_path} when querying "
                f"its own stem; got {sorted(p for p in matched_paths if p)}"
            )
    else:
        # For a code PR, trace at least one symbol-shaped token taken from
        # the diff's added lines.
        added_symbols = _extract_added_symbols(pr.diff_text)
        assert added_symbols, "couldn't extract any symbol-shaped tokens from diff"
        sample = list(added_symbols)[:8]
        traced = code_localizer(
            path=str(repo_dir),
            op="trace",
            queries=sample,
        )
        assert traced["op"] == "trace"
        assert traced["queries"] == len(sample)
        # We don't require every symbol to resolve (most won't, the graph
        # only indexes defined names) — just that the call shape is correct.
        assert isinstance(traced["results"], list)


def _extract_added_symbols(diff_text: str) -> list[str]:
    """Pull plausibly-symbol-shaped tokens from a diff's added lines.

    Used purely to feed ``code_localizer(op='trace')`` with realistic
    inputs derived from the PR itself. The shape filter mirrors what the
    graph indexer accepts (identifier-ish words, length >= 3).
    """

    import re

    seen: set[str] = set()
    out: list[str] = []
    pattern = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
    skip_prefixes = ("+++ ", "+++", "+++\t")
    for line in diff_text.splitlines():
        if not line.startswith("+"):
            continue
        if line.startswith(skip_prefixes):
            continue
        # Strip the leading '+' marker.
        body = line[1:]
        for tok in pattern.findall(body):
            if tok in {"import", "from", "return", "class", "def", "func"}:
                continue
            if tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
    return out
