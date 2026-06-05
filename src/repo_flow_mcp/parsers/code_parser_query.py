"""Tree-sitter query-based code parser (replaces Python-side recursion).

This module mirrors :mod:`tree_sitter_parser` but uses ``tree_sitter.Query``
to extract every interesting node (function definitions, calls, imports) in
a single C-side traversal per file, rather than a recursive Python walk.

The hot path in the legacy parser was a ``_walk`` recursion that issued
``Node.child()`` / ``Node.kind()`` / ``Node.child_count()`` for every node in
the syntax tree — tens of millions of Python→C boundary crossings on a
medium repo. Queries push that traversal into C, so we only allocate Python
objects for nodes we actually emit graph edges for.

Both parsers can coexist: ``parse_code_file`` calls
:func:`parse_with_tree_sitter_query` first and falls back to the legacy
recursive parser if the grammar/binding combo can't satisfy the query API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from repo_flow_mcp.models import (
    EdgeKind,
    GraphDocument,
    GraphEdge,
    GraphNode,
    NodeKind,
    make_node_id,
)

# The ``tree_sitter`` package's Node/Parser/Query types are native and do not
# expose proper Python class types we can statically reference; use Any so the
# extractor signatures stay readable.
Node = Any
Parser = Any
Query = Any

LANG_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
}


# Cache: lang_key -> (Parser, Query) once compiled.
_PARSER_QUERY_CACHE: dict[str, tuple[Parser, Query]] = {}
_LOAD_FAILED: set[str] = set()


def _build_parser_and_query(lang_key: str) -> tuple[Parser, Query] | None:
    if lang_key in _PARSER_QUERY_CACHE:
        return _PARSER_QUERY_CACHE[lang_key]
    if lang_key in _LOAD_FAILED:
        return None
    try:
        from tree_sitter import Parser as TSParser  # type: ignore[import-not-found]
        from tree_sitter import Query as TSQuery  # type: ignore[import-not-found]
        from tree_sitter_language_pack import get_language  # type: ignore[import-not-found]
    except Exception:
        _LOAD_FAILED.add(lang_key)
        return None
    spec = SPECS.get(lang_key)
    if spec is None:
        _LOAD_FAILED.add(lang_key)
        return None
    try:
        lang = get_language(lang_key)
        parser = TSParser(lang)
        query = TSQuery(lang, spec.query_text)
    except Exception:
        _LOAD_FAILED.add(lang_key)
        return None
    _PARSER_QUERY_CACHE[lang_key] = (parser, query)
    return parser, query


def _node_text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _child_by_field(node: Node, name: str) -> Node | None:
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None


def _find_first(node: Node, kinds: set[str]) -> Node | None:
    for child in node.children:
        if child.type in kinds:
            return child
    return None


def _find_first_descendant(node: Node, kinds: set[str], max_depth: int = 6) -> Node | None:
    if max_depth <= 0:
        return None
    for child in node.children:
        if child.type in kinds:
            return child
        found = _find_first_descendant(child, kinds, max_depth - 1)
        if found is not None:
            return found
    return None


def _last_segment(text: str) -> str:
    for sep in ("::", "->", "."):
        if sep in text:
            text = text.split(sep)[-1]
    return text.strip()


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] in {'"', "'", "<", "`"}:
        end = {"<": ">"}.get(text[0], text[0])
        if text[-1] == end:
            return text[1:-1]
    return text


@dataclass
class _LangSpec:
    # Tree-sitter S-expression query. Must capture top-level def/call/import
    # nodes as ``@def`` / ``@call`` / ``@import`` respectively. Inner captures
    # are ignored; extractors pull names off the captured nodes themselves.
    query_text: str = ""
    def_name: Callable[[Node, bytes], str | None] = lambda n, s: None
    call_name: Callable[[Node, bytes], str | None] = lambda n, s: None
    import_modules: Callable[[Node, bytes], list[str]] = lambda n, s: []
    # When a @call should be reinterpreted as an import (JS require/import()),
    # return the module name; emits IMPORTS edge and suppresses the CALLS edge.
    call_as_import: Callable[[Node, bytes], str | None] = field(
        default=lambda n, s: None
    )


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def _py_def_name(node: Node, src: bytes) -> str | None:
    name = _child_by_field(node, "name")
    return _node_text(name, src) if name is not None else None


def _py_call_name(node: Node, src: bytes) -> str | None:
    fn = _child_by_field(node, "function")
    if fn is None:
        return None
    return _last_segment(_node_text(fn, src))


def _py_import_modules(node: Node, src: bytes) -> list[str]:
    kind = node.type
    out: list[str] = []
    if kind == "import_statement":
        for child in node.children:
            if child.type == "dotted_name":
                out.append(_node_text(child, src))
            elif child.type == "aliased_import":
                inner = _find_first(child, {"dotted_name"})
                if inner is not None:
                    out.append(_node_text(inner, src))
    elif kind == "import_from_statement":
        module = _child_by_field(node, "module_name")
        if module is None:
            module = _find_first(node, {"dotted_name", "relative_import"})
        if module is not None:
            out.append(_node_text(module, src))
    return out


PYTHON = _LangSpec(
    query_text="""
        (function_definition) @def
        (class_definition) @def
        (call) @call
        (import_statement) @import
        (import_from_statement) @import
    """,
    def_name=_py_def_name,
    call_name=_py_call_name,
    import_modules=_py_import_modules,
)


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------


def _jsts_def_name(node: Node, src: bytes) -> str | None:
    kind = node.type
    if kind in {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "function_expression",
    }:
        name = _child_by_field(node, "name")
        if name is not None:
            return _node_text(name, src)
    if kind == "variable_declarator":
        value = _child_by_field(node, "value")
        if value is None or value.type not in {
            "arrow_function",
            "function_expression",
            "function",
        }:
            return None
        name = _child_by_field(node, "name")
        return _node_text(name, src) if name is not None else None
    return None


def _jsts_call_name(node: Node, src: bytes) -> str | None:
    if node.type == "new_expression":
        target = _child_by_field(node, "constructor")
    else:
        target = _child_by_field(node, "function")
    if target is None:
        return None
    return _last_segment(_node_text(target, src))


def _jsts_import_modules(node: Node, src: bytes) -> list[str]:
    out: list[str] = []
    src_node = _child_by_field(node, "source")
    if src_node is None:
        src_node = _find_first(node, {"string"})
    if src_node is not None:
        frag = _find_first(src_node, {"string_fragment"}) or src_node
        out.append(_strip_quotes(_node_text(frag, src)))
    return out


def _jsts_call_as_import(node: Node, src: bytes) -> str | None:
    if node.type != "call_expression":
        return None
    fn = _child_by_field(node, "function")
    if fn is None:
        return None
    fn_text = _node_text(fn, src).strip()
    if fn_text not in {"require", "import"}:
        return None
    args = _child_by_field(node, "arguments")
    if args is None:
        return None
    string_node = _find_first_descendant(args, {"string"}, max_depth=3)
    if string_node is None:
        return None
    frag = _find_first(string_node, {"string_fragment"})
    text = _node_text(frag, src) if frag is not None else _node_text(string_node, src)
    return _strip_quotes(text) or None


JSTS = _LangSpec(
    query_text="""
        (function_declaration) @def
        (class_declaration) @def
        (method_definition) @def
        (variable_declarator) @def
        (call_expression) @call
        (new_expression) @call
        (import_statement) @import
    """,
    def_name=_jsts_def_name,
    call_name=_jsts_call_name,
    import_modules=_jsts_import_modules,
    call_as_import=_jsts_call_as_import,
)


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def _go_def_name(node: Node, src: bytes) -> str | None:
    name = _child_by_field(node, "name")
    if name is None:
        name = _find_first(node, {"field_identifier", "identifier"})
    return _node_text(name, src) if name is not None else None


def _go_call_name(node: Node, src: bytes) -> str | None:
    fn = _child_by_field(node, "function")
    if fn is None:
        return None
    return _last_segment(_node_text(fn, src))


def _go_import_modules(node: Node, src: bytes) -> list[str]:
    out: list[str] = []
    for spec_node in node.children:
        if spec_node.type == "import_spec_list":
            for inner in spec_node.children:
                if inner.type == "import_spec":
                    path = _find_first(
                        inner,
                        {"interpreted_string_literal", "raw_string_literal"},
                    )
                    if path is not None:
                        out.append(_strip_quotes(_node_text(path, src)))
        elif spec_node.type == "import_spec":
            path = _find_first(
                spec_node, {"interpreted_string_literal", "raw_string_literal"}
            )
            if path is not None:
                out.append(_strip_quotes(_node_text(path, src)))
    return out


GO = _LangSpec(
    query_text="""
        (function_declaration) @def
        (method_declaration) @def
        (call_expression) @call
        (import_declaration) @import
    """,
    def_name=_go_def_name,
    call_name=_go_call_name,
    import_modules=_go_import_modules,
)


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------


def _rust_def_name(node: Node, src: bytes) -> str | None:
    name = _child_by_field(node, "name")
    return _node_text(name, src) if name is not None else None


def _rust_call_name(node: Node, src: bytes) -> str | None:
    if node.type == "macro_invocation":
        macro = _child_by_field(node, "macro")
        if macro is None:
            return None
        return _last_segment(_node_text(macro, src)) + "!"
    fn = _child_by_field(node, "function")
    if fn is None:
        return None
    return _last_segment(_node_text(fn, src))


def _rust_import_modules(node: Node, src: bytes) -> list[str]:
    arg = _child_by_field(node, "argument")
    if arg is None:
        arg = _find_first(
            node,
            {
                "scoped_identifier",
                "identifier",
                "scoped_use_list",
                "use_as_clause",
                "use_list",
            },
        )
    if arg is None:
        return []
    return [_node_text(arg, src).strip()]


RUST = _LangSpec(
    query_text="""
        (function_item) @def
        (struct_item) @def
        (enum_item) @def
        (trait_item) @def
        (impl_item) @def
        (call_expression) @call
        (macro_invocation) @call
        (use_declaration) @import
    """,
    def_name=_rust_def_name,
    call_name=_rust_call_name,
    import_modules=_rust_import_modules,
)


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------


def _java_def_name(node: Node, src: bytes) -> str | None:
    name = _child_by_field(node, "name")
    return _node_text(name, src) if name is not None else None


def _java_call_name(node: Node, src: bytes) -> str | None:
    if node.type == "method_invocation":
        name = _child_by_field(node, "name")
        if name is not None:
            return _node_text(name, src)
    if node.type == "object_creation_expression":
        ty = _child_by_field(node, "type")
        if ty is not None:
            return _last_segment(_node_text(ty, src))
    return None


def _java_import_modules(node: Node, src: bytes) -> list[str]:
    target = _find_first(node, {"scoped_identifier", "identifier"})
    if target is None:
        return []
    return [_node_text(target, src)]


JAVA = _LangSpec(
    query_text="""
        (method_declaration) @def
        (class_declaration) @def
        (interface_declaration) @def
        (constructor_declaration) @def
        (method_invocation) @call
        (object_creation_expression) @call
        (import_declaration) @import
    """,
    def_name=_java_def_name,
    call_name=_java_call_name,
    import_modules=_java_import_modules,
)


# ---------------------------------------------------------------------------
# C / C++
# ---------------------------------------------------------------------------


def _cpp_def_name(node: Node, src: bytes) -> str | None:
    declarator = _child_by_field(node, "declarator")
    if declarator is None:
        return None
    inner = declarator
    seen = 0
    while inner is not None and seen < 6:
        if inner.type == "function_declarator":
            inner = _child_by_field(inner, "declarator")
        elif inner.type in {"pointer_declarator", "reference_declarator"}:
            inner = _child_by_field(inner, "declarator") or _find_first(
                inner, {"identifier", "field_identifier", "qualified_identifier"}
            )
        else:
            break
        seen += 1
    if inner is None:
        return None
    if inner.type in {"identifier", "field_identifier"}:
        return _node_text(inner, src)
    if inner.type == "qualified_identifier":
        return _last_segment(_node_text(inner, src))
    fallback = _find_first_descendant(
        declarator, {"identifier", "field_identifier", "qualified_identifier"}
    )
    if fallback is None:
        return None
    return _last_segment(_node_text(fallback, src))


def _cpp_call_name(node: Node, src: bytes) -> str | None:
    fn = _child_by_field(node, "function")
    if fn is None:
        return None
    return _last_segment(_node_text(fn, src))


def _cpp_import_modules(node: Node, src: bytes) -> list[str]:
    path = _find_first(node, {"system_lib_string", "string_literal"})
    if path is None:
        return []
    return [_strip_quotes(_node_text(path, src))]


CPP = _LangSpec(
    query_text="""
        (function_definition) @def
        (class_specifier) @def
        (struct_specifier) @def
        (call_expression) @call
        (preproc_include) @import
    """,
    def_name=_cpp_def_name,
    call_name=_cpp_call_name,
    import_modules=_cpp_import_modules,
)


SPECS: dict[str, _LangSpec] = {
    "python": PYTHON,
    "javascript": JSTS,
    "typescript": JSTS,
    "tsx": JSTS,
    "go": GO,
    "rust": RUST,
    "java": JAVA,
    "c": CPP,
    "cpp": CPP,
}


_CALLEE_BLOCKLIST = {
    "",
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "typeof",
    "new",
    "import",
    "require",
}


def language_for_suffix(suffix: str) -> str | None:
    return LANG_BY_SUFFIX.get(suffix.lower())


def parse_with_tree_sitter_query(
    rel_path: str, graph: GraphDocument, text: str
) -> bool:
    """Query-based parse. Returns ``True`` on success, ``False`` if grammar
    or the ``tree_sitter`` query API is unavailable (caller should fall back).
    """

    suffix = Path(rel_path).suffix
    lang_key = language_for_suffix(suffix)
    if lang_key is None:
        return False
    spec = SPECS.get(lang_key)
    if spec is None:
        return False
    bundle = _build_parser_and_query(lang_key)
    if bundle is None:
        return False
    parser, query = bundle

    try:
        from tree_sitter import QueryCursor  # type: ignore[import-not-found]
    except Exception:
        return False

    src_bytes = text.encode("utf-8", errors="replace")
    try:
        tree = parser.parse(src_bytes)
    except Exception:
        return False
    if tree is None:
        return False

    root = tree.root_node
    file_id = make_node_id(NodeKind.FILE, rel_path)
    graph.add_node(
        GraphNode(
            id=file_id,
            kind=NodeKind.FILE,
            label=Path(rel_path).name,
            path=rel_path,
        )
    )

    try:
        cursor = QueryCursor(query)
        caps = cursor.captures(root)
    except Exception:
        return False

    # Process defs first to build node_id -> symbol_id map for caller context.
    def_map: dict[int, str] = {}
    for node in caps.get("def", []):
        try:
            name = spec.def_name(node, src_bytes)
        except Exception:
            name = None
        if not name:
            continue
        symbol_id = make_node_id(NodeKind.CODE_SYMBOL, rel_path, name)
        line = node.start_point[0] + 1
        graph.add_node(
            GraphNode(
                id=symbol_id,
                kind=NodeKind.CODE_SYMBOL,
                label=name,
                path=rel_path,
                metadata={"line": str(line)},
            )
        )
        graph.add_edge(
            GraphEdge(source=file_id, target=symbol_id, kind=EdgeKind.DEFINES)
        )
        def_map[node.id] = symbol_id

    for node in caps.get("import", []):
        try:
            modules = spec.import_modules(node, src_bytes)
        except Exception:
            modules = []
        for module in modules:
            module = module.strip()
            if not module:
                continue
            mod_id = make_node_id(NodeKind.MODULE, module)
            graph.add_node(
                GraphNode(id=mod_id, kind=NodeKind.MODULE, label=module)
            )
            graph.add_edge(
                GraphEdge(source=file_id, target=mod_id, kind=EdgeKind.IMPORTS)
            )

    for node in caps.get("call", []):
        try:
            import_target = spec.call_as_import(node, src_bytes)
        except Exception:
            import_target = None
        if import_target:
            target = import_target.strip()
            if target:
                mod_id = make_node_id(NodeKind.MODULE, target)
                graph.add_node(
                    GraphNode(id=mod_id, kind=NodeKind.MODULE, label=target)
                )
                graph.add_edge(
                    GraphEdge(
                        source=file_id, target=mod_id, kind=EdgeKind.IMPORTS
                    )
                )
            continue
        try:
            callee = spec.call_name(node, src_bytes)
        except Exception:
            callee = None
        if not callee or callee in _CALLEE_BLOCKLIST:
            continue
        callee_id = make_node_id(NodeKind.CODE_SYMBOL, callee)
        graph.add_node(
            GraphNode(id=callee_id, kind=NodeKind.CODE_SYMBOL, label=callee)
        )
        caller = file_id
        cur_node = node.parent
        while cur_node is not None:
            if cur_node.id in def_map:
                caller = def_map[cur_node.id]
                break
            cur_node = cur_node.parent
        graph.add_edge(
            GraphEdge(source=caller, target=callee_id, kind=EdgeKind.CALLS)
        )

    return True
