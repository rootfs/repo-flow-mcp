"""Per-process cache for built graphs.

`build_graph(path)` walks the entire repo and parses every file, which
is expensive (hundreds of ms on a medium repo, seconds on a large one).
The MCP tools call it once per invocation, so a session that issues 10
single-symbol lookups would otherwise build the same graph 10 times.

Caching strategy:

1. The first ``get_graph(path)`` builds the graph and starts a
   ``RepoWatcher`` (watchdog observer) on the repo root.
2. Subsequent calls reuse the cached graph as long as no filesystem
   event has fired since the last build. The next event after a build
   flips a dirty flag, so the following call rebuilds and the watcher
   keeps running.
3. Each cached entry also holds a ``SymbolIndex`` (SQLite FTS5) over
   the graph's ``code_symbol`` nodes so tools that look up function
   names by query don't have to scan every node.

When entries are evicted (LRU bound, ``clear_cache``, or shutdown via
``atexit``), the watcher is stopped and the FTS5 connection is closed
so we don't leak threads or file descriptors.
"""

from __future__ import annotations

import atexit
import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from repo_flow_mcp import graph_persistence
from repo_flow_mcp.doc_index import DocIndex
from repo_flow_mcp.graph_builder import build_graph
from repo_flow_mcp.models import GraphDocument
from repo_flow_mcp.repo_watcher import RepoWatcher
from repo_flow_mcp.settings import ScanSettings, load_settings
from repo_flow_mcp.symbol_index import SymbolIndex

_CACHE_MAX_ENTRIES = 4


@dataclass
class RepoEntry:
    graph: GraphDocument
    index: SymbolIndex
    docs: DocIndex
    watcher: RepoWatcher


_cache: "OrderedDict[tuple[str, bool], RepoEntry]" = OrderedDict()
_lock = Lock()


def _evict(entry: RepoEntry) -> None:
    entry.watcher.stop()
    entry.index.close()
    entry.docs.close()


def _walk_repo_paths(root: Path, settings: ScanSettings) -> list[str]:
    """Mirror the graph builder's walk to give the doc index a path list.

    Returns repo-relative POSIX paths for every file the graph builder
    would have visited, with the same ignore-dir / hidden-file rules
    so the doc index never indexes ``node_modules`` or ``.venv`` even
    when no source file there triggers a graph node.
    """
    paths: list[str] = []
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            rel = file_path.relative_to(root)
        except ValueError:
            continue
        skip = False
        for part in rel.parts:
            if part in settings.ignore_dirs:
                skip = True
                break
            if (
                not settings.include_hidden
                and part.startswith(".")
                and part != ".github"
            ):
                skip = True
                break
        if skip:
            continue
        paths.append(rel.as_posix())
        if len(paths) >= settings.max_files:
            break
    return paths


def _build_entry(path: Path, include_hidden: bool) -> RepoEntry:
    settings = load_settings()
    if include_hidden:
        settings = ScanSettings(
            max_files=settings.max_files,
            max_file_size_bytes=settings.max_file_size_bytes,
            include_hidden=True,
            ignore_dirs=settings.ignore_dirs,
            scan_exts=settings.scan_exts,
        )
    # Try the on-disk graph cache first. The key is content-addressed,
    # so any two worktrees with identical files share a hit. The hash
    # walk is itself an rglob+read of the whole tree, so we only use it
    # when the in-process cache already missed.
    graph: GraphDocument | None = None
    if os.getenv("REPO_FLOW_DISK_CACHE", "1") not in {"0", "false", ""}:
        try:
            key = graph_persistence.compute_worktree_key(
                path, include_hidden=include_hidden, settings=settings
            )
            graph = graph_persistence.read(key)
        except Exception:
            # Cache is best-effort; never fail a build because of it.
            graph = None
            key = None
    else:
        key = None
    if graph is None:
        graph = build_graph(str(path), include_hidden=include_hidden)
        if key is not None:
            try:
                graph_persistence.write(key, graph)
            except Exception:
                pass
    index = SymbolIndex.from_graph(graph)
    rel_paths = _walk_repo_paths(path, settings)
    docs = DocIndex.from_root(path, rel_paths)
    watcher = RepoWatcher(path, settings)
    watcher.start()
    return RepoEntry(
        graph=graph, index=index, docs=docs, watcher=watcher
    )


def _get_entry(path: str, include_hidden: bool) -> RepoEntry:
    root = Path(path).resolve()
    key = (str(root), bool(include_hidden))
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            if not cached.watcher.consume_dirty():
                _cache.move_to_end(key)
                return cached
            # Dirty: drop the stale entry before rebuilding.
            del _cache[key]
            _evict(cached)
    # Build outside the lock — graph building can take seconds and
    # blocking other tool calls during it would defeat the purpose.
    entry = _build_entry(root, include_hidden)
    with _lock:
        existing = _cache.get(key)
        if existing is not None:
            # Race: another thread built it concurrently. Keep theirs,
            # discard ours.
            _evict(entry)
            _cache.move_to_end(key)
            return existing
        _cache[key] = entry
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _, evicted = _cache.popitem(last=False)
            _evict(evicted)
        return entry


def get_graph(path: str, include_hidden: bool = False) -> GraphDocument:
    """Return a cached or freshly built graph for ``path``."""
    return _get_entry(path, include_hidden).graph


def get_symbol_index(path: str, include_hidden: bool = False) -> SymbolIndex:
    """Return the FTS5 symbol index that pairs with ``get_graph(path)``."""
    return _get_entry(path, include_hidden).index


def get_doc_index(path: str, include_hidden: bool = False) -> DocIndex:
    """Return the FTS5 BM25 doc index that pairs with ``get_graph(path)``."""
    return _get_entry(path, include_hidden).docs


def clear_cache() -> None:
    with _lock:
        for entry in _cache.values():
            _evict(entry)
        _cache.clear()


@atexit.register
def _shutdown() -> None:
    clear_cache()
