"""SQLite FTS5 full-text index over the symbols in a graph.

`function_to_script_chains` previously scanned every ``code_symbol`` node
on every call (``query in symbol.label.lower()``). On larger repos that
walk dominates the latency of a localizer call. This module builds a
per-graph in-memory FTS5 index once and answers symbol lookups in
microseconds.

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


class SymbolIndex:
    """In-memory FTS5 index over ``code_symbol`` nodes."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = Lock()
        self._conn.execute(
            "CREATE VIRTUAL TABLE symbols USING fts5("
            "symbol_id UNINDEXED, label, file UNINDEXED, "
            "tokenize='unicode61')"
        )

    @classmethod
    def from_graph(cls, graph: GraphDocument) -> "SymbolIndex":
        index = cls()
        index._populate(graph)
        return index

    def _populate(self, graph: GraphDocument) -> None:
        rows: list[tuple[str, str, str]] = []
        for node in graph.nodes.values():
            if node.kind != NodeKind.CODE_SYMBOL:
                continue
            rows.append((node.id, node.label or "", node.path or ""))
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO symbols(symbol_id, label, file) VALUES (?, ?, ?)",
                rows,
            )

    def search(self, query: str, limit: int = 200) -> list[str]:
        """Return matching ``code_symbol`` node ids, ordered by FTS5 rank.

        Returns an empty list for an empty query or when no token survives
        normalization. The returned ids are guaranteed to refer to nodes
        that were present in the graph at index-build time; callers must
        still resolve them against the current graph.
        """
        tokens = _TOKEN_RE.findall(query or "")
        if not tokens:
            return []
        # Prefix-AND: every token must appear (as a prefix) for a row
        # to match. This keeps `build_graph` -> finds `build_graph` and
        # `main` -> finds `main`, while filtering out unrelated symbols.
        match_expr = " ".join(f"{t}*" for t in tokens)
        with self._lock:
            cur = self._conn.execute(
                "SELECT symbol_id FROM symbols WHERE symbols MATCH ? "
                "ORDER BY rank LIMIT ?",
                (match_expr, max(1, limit)),
            )
            return [row[0] for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
