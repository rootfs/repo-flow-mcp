"""SQLite FTS5 BM25 index over markdown / rst doc sections.

The symbol index answers "where is this function called?". This index
answers the doc-localizer questions:

- "Which existing pages discuss the same concepts as my new doc?"
- "Where else in the repo is the term 'anchor calibration' used?"
- "Does this relative link target an actual file in the repo?"

Each row is a *section* (split on the nearest heading) rather than a
whole file, so BM25 hits land on a specific ``path:start_line`` we can
cite. Tokenization uses ``porter unicode61`` so "calibrate" and
"calibration" share an inverted-list entry.

The index is held next to the ``SymbolIndex`` in the per-repo
``RepoEntry`` so the watchdog observer rebuilds both atomically when
any file changes.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

_DOC_SUFFIXES = {".md", ".mdx", ".markdown", ".rst", ".txt"}

# Heading detectors. We capture heading text so we can store it as the
# section title. ATX headings (markdown ``# foo``) and Setext headings
# (markdown ``Foo\n===``) are supported; rst-style headings reuse the
# Setext rule because they share the underline pattern.
_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SETEXT_UNDERLINE_RE = re.compile(r"^[=\-~^\"'`*+]{3,}\s*$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class DocChunk:
    path: str
    section: str
    start_line: int
    body: str


def _split_sections(text: str) -> list[tuple[str, int, list[str]]]:
    """Split ``text`` into ``(section_title, start_line, body_lines)``.

    The first chunk is always emitted (with an empty title) so files
    with no headings still produce a row.
    """
    lines = text.splitlines()
    sections: list[tuple[str, int, list[str]]] = []
    current_title = ""
    current_start = 1
    current_body: list[str] = []

    def flush() -> None:
        if current_body or sections == []:
            sections.append((current_title, current_start, list(current_body)))

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        atx = _ATX_HEADING_RE.match(line)
        if atx:
            flush()
            current_title = atx.group(2).strip()
            current_start = i + 1
            current_body = []
            i += 1
            continue
        # Setext: this line + a following underline-only line.
        if (
            i + 1 < n
            and line.strip()
            and _SETEXT_UNDERLINE_RE.match(lines[i + 1])
        ):
            flush()
            current_title = line.strip()
            current_start = i + 1
            current_body = []
            i += 2
            continue
        current_body.append(line)
        i += 1
    flush()
    return sections


def iter_doc_chunks(rel_path: str, text: str) -> list[DocChunk]:
    chunks: list[DocChunk] = []
    for title, start_line, body_lines in _split_sections(text):
        body = "\n".join(body_lines).strip()
        if not body and not title:
            continue
        chunks.append(
            DocChunk(
                path=rel_path,
                section=title,
                start_line=start_line,
                body=body,
            )
        )
    return chunks


class DocIndex:
    """In-memory FTS5 BM25 index over markdown/rst doc sections."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = Lock()
        # ``content=''`` makes this an "external content" table backed
        # by the rows we insert; bm25() ranking is enabled by default.
        self._conn.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "path UNINDEXED, section, start_line UNINDEXED, body, "
            "tokenize='porter unicode61')"
        )
        # Path-set table for fast anchor-resolve checks. Kept as a plain
        # rowid table because we only need set membership, not search.
        self._conn.execute(
            "CREATE TABLE paths (path TEXT PRIMARY KEY)"
        )

    @classmethod
    def from_root(cls, root: Path, rel_paths: list[str]) -> "DocIndex":
        index = cls()
        index._populate(root, rel_paths)
        return index

    def _populate(self, root: Path, rel_paths: list[str]) -> None:
        path_rows: list[tuple[str]] = []
        chunk_rows: list[tuple[str, str, int, str]] = []
        for rel in rel_paths:
            path_rows.append((rel,))
            suffix = Path(rel).suffix.lower()
            if suffix not in _DOC_SUFFIXES:
                continue
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for chunk in iter_doc_chunks(rel, text):
                chunk_rows.append(
                    (chunk.path, chunk.section, chunk.start_line, chunk.body)
                )
        with self._lock:
            if path_rows:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO paths(path) VALUES (?)", path_rows
                )
            if chunk_rows:
                self._conn.executemany(
                    "INSERT INTO docs(path, section, start_line, body) "
                    "VALUES (?, ?, ?, ?)",
                    chunk_rows,
                )

    def search(
        self,
        query: str,
        limit: int = 10,
        path_glob: str | None = None,
    ) -> list[dict[str, object]]:
        """BM25-ranked passage search over the indexed doc sections.

        Returns one row per matching section, ordered by ascending
        ``bm25(docs)`` (lower = better). Empty / non-tokenizable
        queries return an empty list.
        """
        tokens = _TOKEN_RE.findall(query or "")
        if not tokens:
            return []
        # OR-of-tokens: any token can match. BM25 still ranks passages
        # that contain more (and rarer) tokens higher. This is what you
        # want for "what existing pages discuss this" — too strict an
        # AND will miss obvious siblings.
        match_expr = " OR ".join(f"{t}*" for t in tokens)
        sql = (
            "SELECT path, section, start_line, "
            "snippet(docs, 3, '<mark>', '</mark>', '...', 16), "
            "bm25(docs) AS score "
            "FROM docs WHERE docs MATCH ?"
        )
        params: list[object] = [match_expr]
        if path_glob:
            sql += " AND path GLOB ?"
            params.append(path_glob)
        sql += " ORDER BY score ASC LIMIT ?"
        params.append(max(1, limit))
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [
                {
                    "path": row[0],
                    "section": row[1],
                    "start_line": int(row[2]),
                    "snippet": row[3],
                    "score": float(row[4]),
                }
                for row in cur.fetchall()
            ]

    def has_path(self, rel_path: str) -> bool:
        """Return True if ``rel_path`` was scanned into the index."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM paths WHERE path = ? LIMIT 1", (rel_path,)
            )
            return cur.fetchone() is not None

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
