"""Shared tree-sitter helpers used by the format-specific parsers.

These helpers wrap the ``tree_sitter_language_pack`` API so the rest of the
package never has to deal with raw native objects. Behaviour rules enforced
here (so callers can trust the contract):

* ``get_parser_or_raise`` raises :class:`ParserError` when the grammar is not
  available or fails to load. Callers MUST NOT silently fall back.
* ``parse_or_raise`` returns the parsed tree; if more than 50% of the source
  is covered by ``ERROR`` / ``MISSING`` nodes it raises :class:`ParserError`
  so the caller can surface the failure via ``graph.warnings`` instead of
  producing a half-empty graph.
* All node-walking helpers operate on UTF-8 ``bytes`` because tree-sitter
  positions are byte offsets.

The native bindings exposed by ``tree_sitter_language_pack`` 1.8.x / 
``tree_sitter`` 0.25.x use callable accessors (``node.kind()``,
``node.child(i)``, ``node.start_byte()``, ``tree.root_node()`` …) rather than
properties. All wrappers below honor that.
"""

from __future__ import annotations

from typing import Any, Iterator

Node = Any
Parser = Any
Tree = Any


class ParserError(RuntimeError):
    """Raised when a parser cannot produce a trustworthy graph for an input.

    The graph builder catches this per-file and records a warning. Within a
    parser, raise this instead of silently returning so the caller can decide.
    """

    def __init__(self, parser: str, path: str, reason: str) -> None:
        super().__init__(f"{parser}: {path}: {reason}")
        self.parser = parser
        self.path = path
        self.reason = reason


_PARSER_CACHE: dict[str, Parser] = {}


def get_parser_or_raise(lang_key: str, *, parser_name: str, path: str) -> Parser:
    """Return a cached tree-sitter parser; raise :class:`ParserError` otherwise.

    ``parser_name`` and ``path`` are only used to build the error message so
    the graph builder can attribute the failure.
    """

    cached = _PARSER_CACHE.get(lang_key)
    if cached is not None:
        return cached
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore[import-not-found]
    except Exception as exc:
        raise ParserError(parser_name, path, f"tree_sitter_language_pack import failed: {exc}") from exc
    try:
        parser = get_parser(lang_key)
    except Exception as exc:
        raise ParserError(parser_name, path, f"grammar {lang_key!r} unavailable: {exc}") from exc
    if parser is None:
        raise ParserError(parser_name, path, f"grammar {lang_key!r} returned None")
    _PARSER_CACHE[lang_key] = parser
    return parser


_ERROR_BUDGET_RATIO = 0.5
_MIN_TEXT_FOR_ERROR_CHECK = 8  # avoid div-by-zero / spurious failures on tiny inputs


def parse_or_raise(parser: Parser, text: str, *, parser_name: str, path: str) -> Tree:
    """Parse ``text`` and return the tree, raising :class:`ParserError` on
    fatal failure or when the error budget is exceeded.
    """

    if not isinstance(text, str):
        raise ParserError(parser_name, path, f"expected str source, got {type(text).__name__}")
    try:
        tree = parser.parse(text)
    except Exception as exc:
        raise ParserError(parser_name, path, f"tree-sitter parse failed: {exc}") from exc
    if tree is None:
        raise ParserError(parser_name, path, "tree-sitter returned no tree")

    src_bytes = text.encode("utf-8", errors="replace")
    total = max(len(src_bytes), 1)
    if total >= _MIN_TEXT_FOR_ERROR_CHECK:
        error_bytes = _error_bytes(tree.root_node())
        ratio = error_bytes / total
        if ratio > _ERROR_BUDGET_RATIO:
            raise ParserError(
                parser_name,
                path,
                f"tree-sitter error coverage {ratio:.0%} exceeds budget "
                f"({_ERROR_BUDGET_RATIO:.0%}); refusing partial parse",
            )
    return tree


def _error_bytes(node: Node) -> int:
    """Sum of byte spans covered by ERROR or MISSING nodes."""

    if node.is_error() or node.is_missing():
        return int(max(node.end_byte() - node.start_byte(), 0))
    total = 0
    for i in range(node.child_count()):
        total += _error_bytes(node.child(i))
    return int(total)


def node_text(node: Node, src: bytes) -> str:
    """Return the substring of ``src`` covered by ``node`` as UTF-8 text."""

    return src[node.start_byte() : node.end_byte()].decode("utf-8", errors="replace")


def walk(node: Node) -> Iterator[Node]:
    """Pre-order traversal yielding every node, including the root."""

    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        yield current
        # push children in reverse so traversal stays left-to-right
        for i in range(current.child_count() - 1, -1, -1):
            stack.append(current.child(i))


def iter_children(node: Node) -> Iterator[Node]:
    """Yield direct children of ``node`` in source order."""

    for i in range(node.child_count()):
        yield node.child(i)


def iter_named_children(node: Node) -> Iterator[Node]:
    """Yield direct named children of ``node`` in source order."""

    for i in range(node.named_child_count()):
        yield node.named_child(i)


def first_child_of_kind(node: Node, kinds: set[str]) -> Node | None:
    for child in iter_children(node):
        if child.kind() in kinds:
            return child
    return None


def find_descendant_of_kind(node: Node, kinds: set[str], *, max_depth: int = 12) -> Node | None:
    if max_depth <= 0:
        return None
    for child in iter_children(node):
        if child.kind() in kinds:
            return child
        found = find_descendant_of_kind(child, kinds, max_depth=max_depth - 1)
        if found is not None:
            return found
    return None


def child_by_field(node: Node, name: str) -> Node | None:
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None
