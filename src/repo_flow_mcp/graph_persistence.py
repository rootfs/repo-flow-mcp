"""Disk-persisted cache for built ``GraphDocument`` objects.

A single ``get_graph(worktree)`` call against a fresh worktree currently
costs ~10 s (tree-sitter parse + markdown-it). When a reviewer iterates on
the same PR — or two reviewers look at the same SHA — we re-pay that cost
every process restart, because the in-process LRU in ``graph_cache.py``
doesn't survive across runs.

This module adds a second cache tier underneath the in-process LRU,
keyed by the **content** of the worktree (not its path), so any caller
materializing the same files at the same revision shares the same blob:

    key = sha256( sorted(rel_path + "\\0" + sha256(file_bytes)) )[:16]

Layout: ``$XDG_CACHE_HOME/repo-flow-mcp/graphs/<key>.pkl.gz``.

Concurrency
-----------
Writers use the temp-then-``os.replace()`` pattern so a reader either
sees the previous complete file or the new complete file — never a
partial. Two concurrent processes that both miss simply duplicate work
and the last writer wins (the inputs are identical, so the output is
too). This keeps the lock-free hot path fast at the cost of one wasted
build in the rare same-PR-same-instant collision.

Schema versioning
-----------------
The pickled blob is wrapped in ``{"v": SCHEMA_VERSION, "graph": ...}``.
Readers that find an older ``v`` treat the entry as a miss; bump
``SCHEMA_VERSION`` when ``GraphDocument`` or any nested type changes
shape.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import pickle
import tempfile
from pathlib import Path

from repo_flow_mcp.models import GraphDocument
from repo_flow_mcp.settings import ScanSettings, load_settings

SCHEMA_VERSION = 1

_DEFAULT_CACHE_HOME = Path.home() / ".cache" / "repo-flow-mcp"


def _cache_root() -> Path:
    override = os.getenv("REPO_FLOW_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.getenv("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "repo-flow-mcp"
    return _DEFAULT_CACHE_HOME


def _graphs_dir() -> Path:
    return _cache_root() / "graphs"


def compute_worktree_key(
    root: Path,
    *,
    include_hidden: bool,
    settings: ScanSettings | None = None,
) -> str:
    """Return a stable 16-char hex key for the contents of ``root``.

    The hash covers (rel_path, sha256(bytes)) for every file the graph
    builder would visit. Two worktrees with identical content collide
    by design — that's the whole point of the cache.
    """

    if settings is None:
        settings = load_settings()
    hasher = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    for file_path in sorted(root.rglob("*")):
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
                not include_hidden
                and part.startswith(".")
                and part != ".github"
            ):
                skip = True
                break
        if skip:
            continue
        try:
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        except OSError:
            # Unreadable files can't change the graph, but record their
            # absence so two worktrees that differ only by permission
            # don't collide.
            digest = "unreadable"
        entries.append((rel.as_posix(), digest))
        if len(entries) >= settings.max_files:
            break
    hasher.update(b"v1\n")
    hasher.update(b"hidden=1\n" if include_hidden else b"hidden=0\n")
    for rel_str, digest in entries:
        hasher.update(rel_str.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(digest.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()[:16]


def read(key: str) -> GraphDocument | None:
    """Return the cached graph for ``key`` or ``None`` on miss."""

    path = _graphs_dir() / f"{key}.pkl.gz"
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rb") as fh:
            payload = pickle.load(fh)
    except (OSError, pickle.UnpicklingError, EOFError):
        # Corrupted entry — treat as miss; a fresh write will overwrite.
        return None
    if not isinstance(payload, dict) or payload.get("v") != SCHEMA_VERSION:
        return None
    graph = payload.get("graph")
    if not isinstance(graph, GraphDocument):
        return None
    return graph


def write(key: str, graph: GraphDocument) -> None:
    """Persist ``graph`` under ``key`` atomically."""

    out_dir = _graphs_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{key}.pkl.gz"
    payload = {"v": SCHEMA_VERSION, "graph": graph}
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{key}.", suffix=".tmp", dir=out_dir
    )
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.open(raw, "wb", compresslevel=6) as gz:
                pickle.dump(payload, gz, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp_name, final)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def clear() -> None:
    """Remove the entire on-disk graph cache (test helper)."""

    out_dir = _graphs_dir()
    if not out_dir.exists():
        return
    for entry in out_dir.iterdir():
        try:
            entry.unlink()
        except IsADirectoryError:
            continue
        except FileNotFoundError:
            continue
