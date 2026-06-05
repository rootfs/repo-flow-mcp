"""Fetch repo contents at a specific commit SHA.

For GitHub-hosted repos we prefer the ``archive/<sha>.tar.gz`` endpoint
because it's about 1.5x faster than ``git clone --filter=blob:none`` for
the cold case (~7 s vs ~11 s on the semantic-router repo). For non-GitHub
remotes — or when the tarball endpoint 404s — we fall back to ``git``.

The fetched tree is **not** a git repo (no ``.git/``). Callers that need
``git apply --3way`` should initialize one after the fact, or use plain
``git apply`` which works against any directory.

A sentinel file ``.repo_flow_complete`` is written at the destination
root on success so callers can detect a complete fetch and skip work.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

# Strip ``.git`` and a leading ``https://github.com/`` / ``git@github.com:``
# to compute the repo slug ``owner/name``.
_GITHUB_HTTP_PREFIXES = ("https://github.com/", "http://github.com/")


def _is_github(repo_url: str) -> bool:
    return (
        repo_url.startswith(_GITHUB_HTTP_PREFIXES)
        or repo_url.startswith("git@github.com:")
    )


def _github_slug(repo_url: str) -> str:
    spec = repo_url
    if spec.startswith("git@github.com:"):
        spec = spec[len("git@github.com:") :]
    else:
        for prefix in _GITHUB_HTTP_PREFIXES:
            if spec.startswith(prefix):
                spec = spec[len(prefix) :]
                break
    if spec.endswith(".git"):
        spec = spec[: -len(".git")]
    return spec


_COMPLETE_MARKER = ".repo_flow_complete"


def is_complete(dest: Path) -> bool:
    """Return ``True`` if ``dest`` holds a previously completed fetch."""

    return (dest / _COMPLETE_MARKER).is_file()


def _mark_complete(dest: Path, sha: str) -> None:
    (dest / _COMPLETE_MARKER).write_text(f"sha={sha}\n", encoding="utf-8")


def fetch_repo_at_sha(repo_url: str, sha: str, dest: Path) -> Path:
    """Materialize the repo at ``sha`` into ``dest`` and return ``dest``.

    Idempotent: if ``dest`` already has the completion marker, returns
    immediately. Otherwise fetches via tarball (GitHub) or git (others).
    On failure ``dest`` is removed so the next call can retry from scratch.
    """

    if is_complete(dest):
        return dest

    dest.mkdir(parents=True, exist_ok=True)

    try:
        if _is_github(repo_url):
            _fetch_github_tarball(_github_slug(repo_url), sha, dest)
        else:
            _fetch_via_git(repo_url, sha, dest)
        _mark_complete(dest, sha)
    except Exception:
        # Don't leave a half-populated directory behind.
        shutil.rmtree(dest, ignore_errors=True)
        raise

    return dest


def _fetch_github_tarball(slug: str, sha: str, dest: Path) -> None:
    """Download and extract ``archive/<sha>.tar.gz`` into ``dest``.

    The tarball's top-level directory is ``<repo>-<sha>/``; we strip it
    with ``tarfile`` so files land directly under ``dest``.
    """

    url = f"https://github.com/{slug}/archive/{sha}.tar.gz"
    try:
        with urllib.request.urlopen(url, timeout=300) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        # 404 typically means the SHA isn't on a default branch; fall
        # back to git which can fetch any reachable SHA.
        if exc.code == 404:
            _fetch_via_git(f"https://github.com/{slug}.git", sha, dest)
            return
        raise

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise RuntimeError(f"tarball for {slug}@{sha} was empty")
        # All members share the ``<repo>-<sha>/`` prefix; compute and strip.
        prefix = members[0].name.split("/", 1)[0] + "/"
        filtered: list[tarfile.TarInfo] = []
        for member in members:
            if not member.name.startswith(prefix):
                # Defensive: ignore any unexpected top-level entries
                # rather than write outside ``dest``.
                continue
            stripped = member.name[len(prefix) :]
            if not stripped:
                continue
            member.name = stripped
            filtered.append(member)
        # tarfile's data filter (Python 3.12+) rejects unsafe paths.
        tar.extractall(path=dest, members=filtered, filter="data")


def _fetch_via_git(repo_url: str, sha: str, dest: Path) -> None:
    """Fallback for non-GitHub remotes or when the tarball isn't available."""

    # Two-step partial clone is faster than a full clone for repos with
    # long histories; matches what the e2e helpers used to do.
    subprocess.run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            repo_url,
            str(dest),
        ],
        check=True,
        capture_output=True,
        timeout=900,
    )
    subprocess.run(
        ["git", "fetch", "--depth", "1", "origin", sha],
        check=True,
        capture_output=True,
        cwd=str(dest),
        timeout=600,
    )
    subprocess.run(
        ["git", "checkout", "--detach", sha],
        check=True,
        capture_output=True,
        cwd=str(dest),
        timeout=120,
    )
