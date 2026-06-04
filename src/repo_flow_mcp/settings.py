from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ScanSettings:
    max_files: int = 20000
    max_file_size_bytes: int = 2 * 1024 * 1024
    include_hidden: bool = False
    ignore_dirs: tuple[str, ...] = (
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "dist",
        "build",
        ".venv",
        "venv",
        "coverage",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
    )
    scan_exts: tuple[str, ...] = (
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".sh",
        ".bash",
        ".zsh",
        ".yml",
        ".yaml",
    )


def load_settings() -> ScanSettings:
    max_files = int(os.getenv("REPO_FLOW_MAX_FILES", "20000"))
    max_size = int(os.getenv("REPO_FLOW_MAX_FILE_SIZE", str(2 * 1024 * 1024)))
    include_hidden = os.getenv("REPO_FLOW_INCLUDE_HIDDEN", "false").lower() == "true"
    return ScanSettings(
        max_files=max_files,
        max_file_size_bytes=max_size,
        include_hidden=include_hidden,
    )
