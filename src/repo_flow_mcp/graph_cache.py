"""Per-process LRU cache for built graphs.

`build_graph(path)` walks the entire repo and parses every file, which is
expensive (hundreds of ms on a medium repo, seconds on a large one). The
MCP tools call it once per invocation, so a session that issues 10
single-symbol lookups builds the same graph 10 times.

This module memoizes the result keyed on
``(realpath(path), include_hidden, max_repo_mtime)`` so a build is reused
across calls to any tool, but is invalidated automatically whenever any
file under the repo is modified.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from repo_flow_mcp.graph_builder import build_graph
from repo_flow_mcp.models import GraphDocument

_CACHE_MAX_ENTRIES = 4
_MTIME_SCAN_MAX_FILES = 50_000

_cache: "OrderedDict[tuple[str, bool, float], GraphDocument]" = OrderedDict()
_lock = Lock()


def _max_mtime(root: Path) -> float:
    """Walk the tree and return the latest mtime under ``root``.

    Bounded by ``_MTIME_SCAN_MAX_FILES`` so a pathological repo cannot
    stall a tool call. Returns 0.0 if the walk hits the cap before
    finishing — that means the cache key is effectively static for that
    repo, and stale graphs will be returned until the cache is evicted
    by ``_CACHE_MAX_ENTRIES``.
    """
    latest = 0.0
    seen = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip dotdirs (other than .github) and common heavy dirs to
            # match what the graph builder itself ignores.
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") or d == ".github"
            ]
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if m > latest:
                    latest = m
                seen += 1
                if seen >= _MTIME_SCAN_MAX_FILES:
                    return latest
    except OSError:
        return latest
    return latest


def get_graph(path: str, include_hidden: bool = False) -> GraphDocument:
    """Return a cached or freshly built graph for ``path``."""
    root = Path(path).resolve()
    key = (str(root), bool(include_hidden), _max_mtime(root))
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    # Build outside the lock — graph building can take seconds and
    # blocking other tool calls during it would defeat the purpose.
    graph = build_graph(path, include_hidden=include_hidden)
    with _lock:
        _cache[key] = graph
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)
    return graph


def clear_cache() -> None:
    with _lock:
        _cache.clear()
