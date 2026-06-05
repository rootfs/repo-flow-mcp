from __future__ import annotations

from pathlib import Path

import pytest

from repo_flow_mcp.doc_index import DocIndex, iter_doc_chunks
from repo_flow_mcp.graph_cache import clear_cache, get_doc_index


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


def _write(tmp: Path, rel: str, body: str) -> None:
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_iter_doc_chunks_splits_on_atx_headings() -> None:
    text = "# Title\n\nintro\n\n## Sub A\n\nbody A\n\n## Sub B\n\nbody B\n"
    chunks = iter_doc_chunks("README.md", text)
    titles = [c.section for c in chunks]
    assert titles == ["Title", "Sub A", "Sub B"]
    assert chunks[1].start_line == 5
    assert "body A" in chunks[1].body


def test_iter_doc_chunks_handles_setext_headings() -> None:
    text = "Title\n=====\n\nbody\n\nSub\n---\n\nmore\n"
    chunks = iter_doc_chunks("a.md", text)
    titles = [c.section for c in chunks]
    assert titles == ["Title", "Sub"]


def test_iter_doc_chunks_emits_single_chunk_for_no_headings() -> None:
    chunks = iter_doc_chunks("notes.txt", "just a plain note\nwith two lines\n")
    assert len(chunks) == 1
    assert chunks[0].section == ""
    assert "plain note" in chunks[0].body


def test_doc_index_search_ranks_matching_passage_first(tmp_path: Path) -> None:
    _write(tmp_path, "docs/calibration.md", "# Anchor calibration\n\nWe calibrate anchors by sampling.\n")
    _write(tmp_path, "docs/unrelated.md", "# Cooking\n\nHow to bake bread.\n")
    _write(tmp_path, "README.md", "# Project\n\nSee docs.\n")

    index = DocIndex.from_root(
        tmp_path,
        ["docs/calibration.md", "docs/unrelated.md", "README.md"],
    )

    hits = index.search("calibrating anchors", limit=5)
    assert hits, "expected BM25 hits"
    assert hits[0]["path"] == "docs/calibration.md"
    assert hits[0]["section"] == "Anchor calibration"


def test_doc_index_search_returns_empty_for_unmatched_query(tmp_path: Path) -> None:
    _write(tmp_path, "docs/a.md", "# A\n\ntext\n")
    index = DocIndex.from_root(tmp_path, ["docs/a.md"])
    assert index.search("") == []
    assert index.search("    ") == []
    assert index.search("zzqxnonexistent") == []


def test_doc_index_search_respects_path_glob(tmp_path: Path) -> None:
    _write(tmp_path, "docs/x.md", "# X\n\nrouting calibration\n")
    _write(tmp_path, "website/docs/y.md", "# Y\n\nrouting calibration\n")
    index = DocIndex.from_root(tmp_path, ["docs/x.md", "website/docs/y.md"])

    hits = index.search("calibration", limit=10, path_glob="website/docs/*")
    paths = {h["path"] for h in hits}
    assert paths == {"website/docs/y.md"}


def test_doc_index_has_path_includes_non_doc_files(tmp_path: Path) -> None:
    _write(tmp_path, "src/main.py", "print('hi')\n")
    _write(tmp_path, "docs/a.md", "# A\n")
    index = DocIndex.from_root(tmp_path, ["src/main.py", "docs/a.md"])
    assert index.has_path("src/main.py") is True
    assert index.has_path("docs/a.md") is True
    assert index.has_path("missing.py") is False


def test_get_doc_index_uses_repo_cache(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# Hello\n\nworld\n")
    docs1 = get_doc_index(str(tmp_path))
    docs2 = get_doc_index(str(tmp_path))
    assert docs1 is docs2, "expected cached DocIndex instance"
    hits = docs1.search("hello", limit=3)
    assert any(h["path"] == "README.md" for h in hits)
