"""Marker registration and skip-by-default for the e2e suite.

E2E tests are opt-in via the ``RUN_E2E_TESTS=1`` env var. They require
``git`` + ``gh`` on ``$PATH`` and network access to github.com.

The actual clone / patch helpers live in ``tests/e2e/_helpers.py`` so that
the test module can import them as a normal Python module.
"""

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "e2e: end-to-end tests that clone real repos from GitHub and run the "
        "MCP tools against them (set RUN_E2E_TESTS=1 to enable).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip e2e tests unless ``RUN_E2E_TESTS`` is set in the environment."""

    if os.environ.get("RUN_E2E_TESTS"):
        return
    skip = pytest.mark.skip(reason="set RUN_E2E_TESTS=1 to run e2e tests")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)
