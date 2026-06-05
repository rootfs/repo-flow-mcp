"""SQLite FTS5 full-text index over the searchable nodes in a graph.

Originally this indexed only ``CODE_SYMBOL`` nodes, which made
``function_to_script_chains`` blind to CI runners (workflow jobs /
steps), Make targets, and shell scripts. We now index any labeled,
non-file node and tag rows with their ``kind`` so callers can ask
"give me workflow jobs that mention 'release'" without scanning the
full node table. Single MATCH expression, single inverted index.

Tokenization uses the default ``unicode61`` tokenizer, which splits on
``_`` and ``.`` — exactly the boundaries identifiers tend to use. The
caller's free-text query is normalized into prefix tokens
(``build_graph`` -> ``build* graph*``), which preserves the substring
behaviour of the old scan for typical identifier queries while using
FTS5's inverted index instead of a linear sweep.
"""

from __future__ import annotations

import re
import sqlite3
from threading import Lock

from repo_flow_mcp.models import GraphDocument, NodeKind

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# Kinds we want fast lookups against. Files are excluded because a
# file's label is its basename, which produces low-signal hits
# ("main.go" matches every other repo's main.go) — callers should
# resolve files via the graph after matching a symbol.
_INDEXED_KINDS: frozenset[NodeKind] = frozenset(
    {
        NodeKind.CODE_SYMBOL,
        NodeKind.MODULE,
        NodeKind.SCRIPT,
        NodeKind.TARGET,
        NodeKind.WORKFLOW,
        NodeKind.WORKFLOW_JOB,
        NodeKind.WORKFLOW_STEP,
    }
)


class SymbolIndex:
    """In-memory FTS5 index over searchable graph nodes."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = Lock()
        # ``kind`` is UNINDEXED — it's filterable in WHERE but not part
        # of the inverted index (which would dilute BM25 with kind
        # tokens like "workflow_job").
        self._conn.execute(
            "CREATE VIRTUAL TABLE symbols USING fts5("
            "symbol_id UNINDEXED, kind UNINDEXED, label, file UNINDEXED, "
            "tokenize='unicode61')"
        )

    @classmethod
    def from_graph(cls, graph: GraphDocument) -> "SymbolIndex":
        index = cls()
        index._populate(graph)
        return index

    def _populate(self, graph: GraphDocument) -> None:
        rows: list[tuple[str, str, str, str]] = []
        for node in graph.nodes.values():
            if node.kind not in _INDEXED_KINDS:
                continue
            rows.append(
                (
                    node.id,
                    node.kind.value,
                    node.label or "",
                    node.path or "",
                )
            )
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO symbols(symbol_id, kind, label, file) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )

    def search(
        self,
        query: str,
        limit: int = 200,
        kinds: frozenset[NodeKind] | None = None,
    ) -> list[str]:
        """Return matching node ids, ordered by FTS5 rank.

        Optionally restrict results to a set of ``NodeKind`` values
        (e.g. ``{NodeKind.CODE_SYMBOL}`` to preserve the legacy
        code-symbol-only behaviour). Returns an empty list for an
        empty query or when no token survives normalization. The
        returned ids are guaranteed to refer to nodes that were
        present in the graph at index-build time; callers must still
        resolve them against the current graph.
        """
        tokens = _TOKEN_RE.findall(query or "")
        if not tokens:
            return []
        # Prefix-AND: every token must appear (as a prefix) for a row
        # to match. This keeps `build_graph` -> finds `build_graph` and
        # `main` -> finds `main`, while filtering out unrelated symbols.
        match_expr = " ".join(f"{t}*" for t in tokens)
        sql = (
            "SELECT symbol_id FROM symbols WHERE symbols MATCH ?"
        )
        params: list[object] = [match_expr]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(k.value for k in kinds)
        sql += " ORDER BY rank LIMIT ?"
        params.append(max(1, limit))
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [row[0] for row in cur.fetchall()]

    def search_with_kinds(
        self,
        query: str,
        limit: int = 200,
        kinds: frozenset[NodeKind] | None = None,
    ) -> list[tuple[str, str]]:
        """Like :meth:`search` but returns ``(symbol_id, kind)`` pairs."""
        tokens = _TOKEN_RE.findall(query or "")
        if not tokens:
            return []
        match_expr = " ".join(f"{t}*" for t in tokens)
        sql = (
            "SELECT symbol_id, kind FROM symbols WHERE symbols MATCH ?"
        )
        params: list[object] = [match_expr]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
            params.extend(k.value for k in kinds)
        sql += " ORDER BY rank LIMIT ?"
        params.append(max(1, limit))
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [(row[0], row[1]) for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
