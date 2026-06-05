from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from repo_flow_mcp import graph_cache
from repo_flow_mcp.graph_cache import clear_cache, get_graph

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "repo_sample"


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def test_get_graph_returns_same_object_on_repeat_call() -> None:
    g1 = get_graph(str(FIXTURE_ROOT))
    g2 = get_graph(str(FIXTURE_ROOT))
    assert g1 is g2


def test_get_graph_distinguishes_include_hidden() -> None:
    g_default = get_graph(str(FIXTURE_ROOT))
    g_hidden = get_graph(str(FIXTURE_ROOT), include_hidden=True)
    assert g_default is not g_hidden
    # Subsequent call with same flag still hits cache.
    assert get_graph(str(FIXTURE_ROOT), include_hidden=True) is g_hidden


def test_get_graph_invalidates_on_mtime_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("all:\n\techo hi\n")
    (repo / "main.py").write_text("def main():\n    pass\n")

    g1 = get_graph(str(repo))
    # Touch an existing file far enough in the future that the mtime
    # resolution (often 1s on ext4) is exceeded.
    target = repo / "main.py"
    future = time.time() + 5
    os.utime(target, (future, future))
    # The watcher delivers events asynchronously through inotify; give
    # the observer thread a chance to mark the entry dirty before we
    # query again.
    _wait_for_dirty(str(repo))

    g2 = get_graph(str(repo))
    assert g1 is not g2


def _wait_for_dirty(path: str, timeout: float = 2.0) -> None:
    """Spin until the watcher reports the cached entry as dirty."""
    from repo_flow_mcp.graph_cache import _cache  # noqa: PLC0415

    deadline = time.time() + timeout
    resolved = str(Path(path).resolve())
    while time.time() < deadline:
        for (root, _flag), entry in list(_cache.items()):
            if root == resolved and entry.watcher._dirty:
                return
        time.sleep(0.05)


def test_get_graph_evicts_lru_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_cache, "_CACHE_MAX_ENTRIES", 2)
    clear_cache()

    repos = []
    for i in range(3):
        r = tmp_path / f"repo{i}"
        r.mkdir()
        (r / "Makefile").write_text(f"all:\n\techo {i}\n")
        repos.append(r)

    g0 = get_graph(str(repos[0]))
    g1 = get_graph(str(repos[1]))
    g2 = get_graph(str(repos[2]))
    # repo0 should have been evicted; repo1 and repo2 still cached.
    assert get_graph(str(repos[1])) is g1
    assert get_graph(str(repos[2])) is g2
    # repo0 is rebuilt → new object identity.
    assert get_graph(str(repos[0])) is not g0
