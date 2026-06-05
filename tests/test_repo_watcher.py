from __future__ import annotations

import time
from pathlib import Path

import pytest

from repo_flow_mcp.graph_cache import _cache, clear_cache, get_graph


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def _wait_for_dirty(repo: Path, timeout: float = 2.0) -> bool:
    """Spin until the watcher marks ``repo`` dirty, or timeout."""
    deadline = time.time() + timeout
    resolved = str(repo.resolve())
    while time.time() < deadline:
        for (root, _flag), entry in list(_cache.items()):
            if root == resolved and entry.watcher._dirty:
                return True
        time.sleep(0.05)
    return False


def test_watcher_rebuilds_when_new_file_added(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("all:\n\techo hi\n")
    g1 = get_graph(str(repo))
    initial_node_count = len(g1.nodes)

    (repo / "newfile.py").write_text("def added():\n    return 1\n")
    assert _wait_for_dirty(repo), "watcher did not flag dirty after file creation"

    g2 = get_graph(str(repo))
    assert g1 is not g2
    assert len(g2.nodes) > initial_node_count
    assert any(
        n.label == "added" and n.kind.value == "code_symbol"
        for n in g2.nodes.values()
    )


def test_watcher_rebuilds_on_file_deletion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("all:\n\techo hi\n")
    target = repo / "main.py"
    target.write_text("def main():\n    pass\n")

    g1 = get_graph(str(repo))
    target.unlink()
    assert _wait_for_dirty(repo), "watcher did not flag dirty after deletion"

    g2 = get_graph(str(repo))
    assert g1 is not g2


def test_watcher_ignores_changes_in_ignored_dirs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("all:\n\techo hi\n")
    (repo / "node_modules").mkdir()

    g1 = get_graph(str(repo))
    # A change inside node_modules must not invalidate the cache.
    (repo / "node_modules" / "junk.js").write_text("// noise\n")
    # Give watchdog a beat so a real event would have arrived.
    time.sleep(0.3)

    g2 = get_graph(str(repo))
    assert g1 is g2
