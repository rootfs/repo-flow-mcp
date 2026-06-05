"""Per-base-SHA worktree cache for review/PR workflows.

A worktree (the materialized repo contents at a specific commit) is the
biggest cold cost in the PR-review flow (~10 s tarball, ~11 s git). But
two PRs branched from the same ``base_sha`` produce identical worktrees,
so caching the base tree by SHA lets the second reviewer reuse the first
one's work.

For per-PR overlays (where we apply a diff on top of the base) we
copy-on-write into a scratch directory via ``cp --reflink=auto`` when
available. On reflink-capable filesystems (Btrfs, XFS with reflink) this
is a constant-time operation; elsewhere it's a regular recursive copy
(still cheap, ~1 s for 244 MB on SSD).

Layout::

    <cache>/worktrees/<base_sha[:12]>/      # the cached base (read-only intent)
    <cache>/worktrees/<base_sha[:12]>.lock  # flock target for fetch race

Eviction is LRU by mtime against a byte budget (default 10 GB, set via
``REPO_FLOW_WORKTREE_BUDGET_BYTES``). The graph cache is independent;
graphs survive worktree eviction (they're far smaller).
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from repo_flow_mcp.graph_persistence import _cache_root
from repo_flow_mcp.repo_fetcher import fetch_repo_at_sha, is_complete

_DEFAULT_BUDGET_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB


def _worktrees_dir() -> Path:
    return _cache_root() / "worktrees"


def _short(sha: str) -> str:
    if len(sha) < 12:
        raise ValueError(f"refusing to cache by short sha: {sha!r}")
    return sha[:12]


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Exclusive flock around the critical section of a single SHA fetch."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def get_base_worktree(repo_url: str, base_sha: str) -> Path:
    """Return the path of the cached read-only worktree at ``base_sha``.

    Fetches on miss; safe under concurrent callers (one process performs
    the fetch under flock, the others block until it completes and then
    see the completion marker).
    """

    short = _short(base_sha)
    dest = _worktrees_dir() / short
    lock = _worktrees_dir() / f"{short}.lock"

    if is_complete(dest):
        _touch(dest)
        return dest

    with _file_lock(lock):
        if is_complete(dest):
            _touch(dest)
            return dest
        fetch_repo_at_sha(repo_url, base_sha, dest)
        _touch(dest)
        _evict_if_over_budget()
    return dest


def materialize_overlay(base: Path, scratch: Path) -> Path:
    """Copy ``base`` into ``scratch`` so it can be mutated without
    affecting the cached entry.

    Uses ``cp --reflink=auto`` when ``cp`` is GNU and the filesystem
    supports CoW (Btrfs/XFS reflinks, APFS clones); falls back to
    :func:`shutil.copytree` otherwise. The destination is a regular
    writable tree.
    """

    scratch.parent.mkdir(parents=True, exist_ok=True)
    if scratch.exists():
        shutil.rmtree(scratch)

    cp_bin = shutil.which("cp")
    if cp_bin is not None:
        try:
            subprocess.run(
                [cp_bin, "-a", "--reflink=auto", str(base), str(scratch)],
                check=True,
                capture_output=True,
                timeout=300,
            )
            return scratch
        except subprocess.CalledProcessError:
            # cp present but failed (e.g. busybox cp doesn't grok --reflink);
            # fall through to the pure-Python copy.
            if scratch.exists():
                shutil.rmtree(scratch)

    shutil.copytree(base, scratch, symlinks=True)
    return scratch


def _touch(path: Path) -> None:
    """Update the mtime of ``path`` for LRU bookkeeping."""

    try:
        os.utime(path, None)
    except OSError:
        pass


def _dir_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            stat = entry.lstat()
            # Skip symlinks (avoid counting their target).
            if (stat.st_mode & 0o170000) == 0o120000:
                continue
            total += stat.st_size
        except OSError:
            continue
    return total


def _evict_if_over_budget() -> None:
    """Drop oldest worktrees until total size is below the byte budget."""

    budget_env = os.getenv("REPO_FLOW_WORKTREE_BUDGET_BYTES")
    if budget_env in {None, "", "0"}:
        budget = _DEFAULT_BUDGET_BYTES
    else:
        try:
            budget = int(budget_env or "0")
        except ValueError:
            budget = _DEFAULT_BUDGET_BYTES
    if budget <= 0:
        return

    root = _worktrees_dir()
    if not root.exists():
        return
    entries = [
        (p, p.stat().st_mtime, _dir_size(p))
        for p in root.iterdir()
        if p.is_dir() and is_complete(p)
    ]
    total = sum(size for _, _, size in entries)
    if total <= budget:
        return
    # Oldest first.
    entries.sort(key=lambda x: x[1])
    for path, _mtime, size in entries:
        if total <= budget:
            break
        shutil.rmtree(path, ignore_errors=True)
        lock = root / f"{path.name}.lock"
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
        total -= size
