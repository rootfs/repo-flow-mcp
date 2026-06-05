"""PR-clone helpers shared by the e2e tests.

Kept in a regular Python module (rather than ``conftest.py``) so the test
file can ``from tests.e2e._helpers import ...`` style imports work without
relying on pytest's plugin loader.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    check: bool = True,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        pytest.skip(f"required tool not on PATH: {name}")


@dataclass(frozen=True)
class PullRequest:
    """The bits of a PR we need to reproduce its working tree locally."""

    repo_url: str
    number: int
    base_ref: str
    base_sha: str
    head_sha: str
    changed_files: tuple[str, ...]
    diff_text: str


def _repo_slug(repo_url: str) -> str:
    """Extract ``owner/name`` from a GitHub URL/ssh spec."""

    spec = repo_url
    if spec.startswith("git@"):
        spec = spec.split(":", 1)[1]
    else:
        for prefix in ("https://github.com/", "http://github.com/"):
            if spec.startswith(prefix):
                spec = spec[len(prefix):]
                break
    if spec.endswith(".git"):
        spec = spec[: -len(".git")]
    return spec


def fetch_pull_request(repo_url: str, number: int) -> PullRequest:
    """Pull PR metadata + the unified diff via the ``gh`` CLI.

    Uses ``gh api`` (the REST endpoint) rather than ``gh pr view`` because
    the older 2.x JSON schema didn't expose ``baseRefOid`` / ``headRefOid``.
    Requires ``gh`` to be authenticated against github.com.
    """

    _require_tool("gh")
    slug = _repo_slug(repo_url)

    metadata = _run(
        [
            "gh",
            "api",
            f"repos/{slug}/pulls/{number}",
        ]
    ).stdout
    meta = json.loads(metadata)
    base_ref = str(meta["base"]["ref"])
    base_sha = str(meta["base"]["sha"])
    head_sha = str(meta["head"]["sha"])

    files_payload = _run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{slug}/pulls/{number}/files",
        ]
    ).stdout
    # ``gh api --paginate`` concatenates JSON arrays with no separator;
    # parse each line as a JSON value and stitch them together.
    changed_files: list[str] = []
    decoder = json.JSONDecoder()
    idx = 0
    text = files_payload.strip()
    while idx < len(text):
        # Skip whitespace between concatenated arrays.
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        chunk, end = decoder.raw_decode(text, idx)
        if isinstance(chunk, list):
            for entry in chunk:
                if isinstance(entry, dict) and isinstance(entry.get("filename"), str):
                    changed_files.append(entry["filename"])
        idx = end

    diff_text = _run(
        ["gh", "pr", "diff", str(number), "--repo", slug, "--patch"]
    ).stdout

    return PullRequest(
        repo_url=repo_url,
        number=number,
        base_ref=base_ref,
        base_sha=base_sha,
        head_sha=head_sha,
        changed_files=tuple(changed_files),
        diff_text=diff_text,
    )


def clone_repo_at(repo_url: str, ref: str, dest: Path) -> None:
    """Shallow-clone ``repo_url`` and check out ``ref`` into ``dest``."""

    _require_tool("git")
    dest.mkdir(parents=True, exist_ok=False)

    try:
        _run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                repo_url,
                str(dest),
            ],
            timeout=900,
        )
    except subprocess.CalledProcessError:
        shutil.rmtree(dest, ignore_errors=True)
        _run(["git", "clone", repo_url, str(dest)], timeout=900)

    _run(["git", "fetch", "--depth", "1", "origin", ref], cwd=dest, timeout=600)
    _run(["git", "checkout", "--detach", ref], cwd=dest)


def apply_diff(work_tree: Path, diff_text: str) -> None:
    """Apply ``diff_text`` to the working tree at ``work_tree``.

    Tries ``git apply --3way`` first (best for renames / context drift) and
    falls back to a plain ``git apply``. Raises ``RuntimeError`` on failure
    so the test surfaces the real reason.
    """

    _require_tool("git")
    _run(["git", "config", "user.email", "e2e@example.invalid"], cwd=work_tree)
    _run(["git", "config", "user.name", "repo-flow-mcp e2e"], cwd=work_tree)

    attempts: list[list[str]] = [
        ["git", "apply", "--3way", "--whitespace=nowarn", "-"],
        ["git", "apply", "--whitespace=nowarn", "-"],
    ]
    last_err = ""
    for cmd in attempts:
        proc = _run(cmd, cwd=work_tree, input_text=diff_text, check=False)
        if proc.returncode == 0:
            return
        last_err = proc.stderr or proc.stdout
    raise RuntimeError(
        f"failed to apply diff to {work_tree}: {last_err.strip() or 'no error output'}"
    )
