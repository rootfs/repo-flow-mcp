"""Tree-sitter backed code parser.

Walks a single source file's syntax tree and emits ``GraphNode``/``GraphEdge``
records for files, modules, code symbols (functions/methods/classes), DEFINES,
CALLS, and IMPORTS edges. Supports Python, JavaScript, TypeScript, Go, Rust,
Java, C, and C++.

Designed as a drop-in primary backend for ``code_parser.parse_code_file``: if
tree-sitter or the requested grammar is unavailable, ``parse_with_tree_sitter``
returns ``False`` and the caller falls back to the legacy regex/ast parsers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from repo_flow_mcp.models import EdgeKind, GraphDocument, GraphEdge, GraphNode, NodeKind, make_node_id

# Tree-sitter exposes its Node and Parser as built-in classes that are not
# importable as proper types; use Any for static typing.
Node = Any
Parser = Any

_PARSER_CACHE: dict[str, Parser] = {}
_LOAD_FAILED: set[str] = set()


def _get_parser(lang_key: str) -> Parser | None:
    if lang_key in _PARSER_CACHE:
        return _PARSER_CACHE[lang_key]
    if lang_key in _LOAD_FAILED:
        return None
    try:
        from tree_sitter_language_pack import get_parser  # type: ignore[import-not-found]
    except Exception:
        _LOAD_FAILED.add(lang_key)
        return None
    try:
        parser = get_parser(lang_key)
    except Exception:
        _LOAD_FAILED.add(lang_key)
        return None
    _PARSER_CACHE[lang_key] = parser
    return parser


# Language detection by file suffix. Headers default to C++; tweak via env later.
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


@dataclass
class _LangSpec:
    # Node kinds whose presence introduces a new "current symbol" scope.
    def_kinds: set[str] = field(default_factory=set)
    # Node kinds that represent a function/method call.
    call_kinds: set[str] = field(default_factory=set)
    # Node kinds whose presence imports modules.
    import_kinds: set[str] = field(default_factory=set)
    # Pulls a definition's symbol name out of a definition node.
    def_name: Callable[[Node, bytes], str | None] = lambda n, s: None
    # Pulls the callee's short name out of a call node.
    call_name: Callable[[Node, bytes], str | None] = lambda n, s: None
    # Pulls the imported module name(s) out of an import node.
    import_modules: Callable[[Node, bytes], list[str]] = lambda n, s: []
    # Optional hook: when a call node should be reinterpreted as an import
    # (e.g. JS ``require("x")`` / dynamic ``import("x")``), return the module
    # name. Returning a non-empty string emits an IMPORTS edge and suppresses
    # the default CALLS edge for that node.
    call_as_import: Callable[[Node, bytes], str | None] = lambda n, s: None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node_text(node: Node, src: bytes) -> str:
    return src[node.start_byte() : node.end_byte()].decode("utf-8", errors="replace")


def _child_by_field(node: Node, name: str) -> Node | None:
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None


def _iter_children(node: Node) -> Any:
    for i in range(node.child_count()):
        yield node.child(i)


def _find_first(node: Node, kinds: set[str]) -> Node | None:
    for child in _iter_children(node):
        if child.kind() in kinds:
            return child
    return None


def _find_first_descendant(node: Node, kinds: set[str], max_depth: int = 6) -> Node | None:
    if max_depth <= 0:
        return None
    for child in _iter_children(node):
        if child.kind() in kinds:
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


# ---------------------------------------------------------------------------
# Per-language extractors
# ---------------------------------------------------------------------------


# --- Python -----------------------------------------------------------------


def _py_def_name(node: Node, src: bytes) -> str | None:
    name = _child_by_field(node, "name")
    return _node_text(name, src) if name is not None else None


def _py_call_name(node: Node, src: bytes) -> str | None:
    fn = _child_by_field(node, "function")
    if fn is None:
        return None
    return _last_segment(_node_text(fn, src))


def _py_import_modules(node: Node, src: bytes) -> list[str]:
    kind = node.kind()
    out: list[str] = []
    if kind == "import_statement":
        for child in _iter_children(node):
            if child.kind() == "dotted_name":
                out.append(_node_text(child, src))
            elif child.kind() == "aliased_import":
                inner = _find_first(child, {"dotted_name"})
                if inner is not None:
                    out.append(_node_text(inner, src))
    elif kind == "import_from_statement":
        # Module name is the dotted_name before "import"
        module = _child_by_field(node, "module_name")
        if module is None:
            module = _find_first(node, {"dotted_name", "relative_import"})
        if module is not None:
            out.append(_node_text(module, src))
    return out


PYTHON = _LangSpec(
    def_kinds={"function_definition", "class_definition"},
    call_kinds={"call"},
    import_kinds={"import_statement", "import_from_statement"},
    def_name=_py_def_name,
    call_name=_py_call_name,
    import_modules=_py_import_modules,
)


# --- JavaScript / TypeScript -----------------------------------------------


_JSTS_NAME_FIELDS = ("name",)


def _jsts_def_name(node: Node, src: bytes) -> str | None:
    kind = node.kind()
    if kind in {"function_declaration", "class_declaration", "method_definition", "function_expression"}:
        name = _child_by_field(node, "name")
        if name is not None:
            return _node_text(name, src)
    if kind == "variable_declarator":
        # Only treat declarators bound to function expressions as defs.
        value = _child_by_field(node, "value")
        if value is None or value.kind() not in {"arrow_function", "function_expression", "function"}:
            return None
        name = _child_by_field(node, "name")
        return _node_text(name, src) if name is not None else None
    return None


def _jsts_call_name(node: Node, src: bytes) -> str | None:
    if node.kind() == "new_expression":
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
    if node.kind() != "call_expression":
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
    def_kinds={"function_declaration", "class_declaration", "method_definition", "variable_declarator"},
    call_kinds={"call_expression", "new_expression"},
    import_kinds={"import_statement"},
    def_name=_jsts_def_name,
    call_name=_jsts_call_name,
    import_modules=_jsts_import_modules,
    call_as_import=_jsts_call_as_import,
)


# --- Go --------------------------------------------------------------------


def _go_def_name(node: Node, src: bytes) -> str | None:
    name = _child_by_field(node, "name")
    if name is None:
        # method_declaration on older grammars exposes name via field_identifier child
        name = _find_first(node, {"field_identifier", "identifier"})
    return _node_text(name, src) if name is not None else None


def _go_call_name(node: Node, src: bytes) -> str | None:
    fn = _child_by_field(node, "function")
    if fn is None:
        return None
    return _last_segment(_node_text(fn, src))


def _go_import_modules(node: Node, src: bytes) -> list[str]:
    out: list[str] = []
    for spec in _iter_children(node):
        if spec.kind() == "import_spec_list":
            for inner in _iter_children(spec):
                if inner.kind() == "import_spec":
                    path = _find_first(inner, {"interpreted_string_literal", "raw_string_literal"})
                    if path is not None:
                        out.append(_strip_quotes(_node_text(path, src)))
        elif spec.kind() == "import_spec":
            path = _find_first(spec, {"interpreted_string_literal", "raw_string_literal"})
            if path is not None:
                out.append(_strip_quotes(_node_text(path, src)))
    return out


GO = _LangSpec(
    def_kinds={"function_declaration", "method_declaration"},
    call_kinds={"call_expression"},
    import_kinds={"import_declaration"},
    def_name=_go_def_name,
    call_name=_go_call_name,
    import_modules=_go_import_modules,
)


# --- Rust ------------------------------------------------------------------


def _rust_def_name(node: Node, src: bytes) -> str | None:
    name = _child_by_field(node, "name")
    return _node_text(name, src) if name is not None else None


def _rust_call_name(node: Node, src: bytes) -> str | None:
    if node.kind() == "macro_invocation":
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
    def_kinds={"function_item", "struct_item", "enum_item", "trait_item", "impl_item"},
    call_kinds={"call_expression", "macro_invocation"},
    import_kinds={"use_declaration"},
    def_name=_rust_def_name,
    call_name=_rust_call_name,
    import_modules=_rust_import_modules,
)


# --- Java ------------------------------------------------------------------


def _java_def_name(node: Node, src: bytes) -> str | None:
    name = _child_by_field(node, "name")
    return _node_text(name, src) if name is not None else None


def _java_call_name(node: Node, src: bytes) -> str | None:
    if node.kind() == "method_invocation":
        name = _child_by_field(node, "name")
        if name is not None:
            return _node_text(name, src)
    if node.kind() == "object_creation_expression":
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
    def_kinds={"method_declaration", "class_declaration", "interface_declaration", "constructor_declaration"},
    call_kinds={"method_invocation", "object_creation_expression"},
    import_kinds={"import_declaration"},
    def_name=_java_def_name,
    call_name=_java_call_name,
    import_modules=_java_import_modules,
)


# --- C / C++ ---------------------------------------------------------------


def _cpp_def_name(node: Node, src: bytes) -> str | None:
    declarator = _child_by_field(node, "declarator")
    if declarator is None:
        return None
    # function_declarator -> declarator field is the actual identifier (or
    # qualified_identifier / pointer_declarator / etc.)
    inner = declarator
    seen = 0
    while inner is not None and seen < 6:
        if inner.kind() == "function_declarator":
            inner = _child_by_field(inner, "declarator")
        elif inner.kind() in {"pointer_declarator", "reference_declarator"}:
            inner = _child_by_field(inner, "declarator") or _find_first(
                inner, {"identifier", "field_identifier", "qualified_identifier"}
            )
        else:
            break
        seen += 1
    if inner is None:
        return None
    if inner.kind() in {"identifier", "field_identifier"}:
        return _node_text(inner, src)
    if inner.kind() == "qualified_identifier":
        return _last_segment(_node_text(inner, src))
    # Fallback: scan for an identifier-like descendant
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
    def_kinds={"function_definition", "class_specifier", "struct_specifier"},
    call_kinds={"call_expression"},
    import_kinds={"preproc_include"},
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


# Call-target names that should never be emitted (control-flow keywords parsed
# as identifiers by some grammars, plus a few generic noise tokens).
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


def parse_with_tree_sitter(rel_path: str, graph: GraphDocument, text: str) -> bool:
    """Parse ``text`` with tree-sitter and populate ``graph``.

    Returns ``True`` on a successful parse (including parses with syntax
    errors — tree-sitter is error-tolerant). Returns ``False`` if the grammar
    cannot be loaded or the text cannot be parsed at all, leaving the caller
    to fall back to the legacy regex parsers.
    """

    suffix = Path(rel_path).suffix
    lang_key = language_for_suffix(suffix)
    if lang_key is None:
        return False
    spec = SPECS.get(lang_key)
    if spec is None:
        return False
    parser = _get_parser(lang_key)
    if parser is None:
        return False
    try:
        tree = parser.parse(text)
    except Exception:
        return False
    if tree is None:
        return False

    src_bytes = text.encode("utf-8", errors="replace")
    file_id = make_node_id(NodeKind.FILE, rel_path)
    graph.add_node(GraphNode(id=file_id, kind=NodeKind.FILE, label=Path(rel_path).name, path=rel_path))

    _walk(tree.root_node(), src_bytes, rel_path, graph, spec, file_id, current_symbol=None)
    return True


def _walk(
    node: Node,
    src: bytes,
    rel_path: str,
    graph: GraphDocument,
    spec: _LangSpec,
    file_id: str,
    current_symbol: str | None,
) -> None:
    kind = node.kind()
    next_symbol = current_symbol

    if kind in spec.def_kinds:
        try:
            name = spec.def_name(node, src)
        except Exception:
            name = None
        if name:
            symbol_id = make_node_id(NodeKind.CODE_SYMBOL, rel_path, name)
            line = node.start_position().row + 1
            graph.add_node(
                GraphNode(
                    id=symbol_id,
                    kind=NodeKind.CODE_SYMBOL,
                    label=name,
                    path=rel_path,
                    metadata={"line": str(line)},
                )
            )
            graph.add_edge(GraphEdge(source=file_id, target=symbol_id, kind=EdgeKind.DEFINES))
            next_symbol = symbol_id

    if kind in spec.call_kinds:
        consumed_as_import = False
        try:
            import_target = spec.call_as_import(node, src)
        except Exception:
            import_target = None
        if import_target:
            import_target = import_target.strip()
            if import_target:
                mod_id = make_node_id(NodeKind.MODULE, import_target)
                graph.add_node(GraphNode(id=mod_id, kind=NodeKind.MODULE, label=import_target))
                graph.add_edge(GraphEdge(source=file_id, target=mod_id, kind=EdgeKind.IMPORTS))
                consumed_as_import = True
        if not consumed_as_import:
            try:
                callee = spec.call_name(node, src)
            except Exception:
                callee = None
            if callee and callee not in _CALLEE_BLOCKLIST:
                callee_id = make_node_id(NodeKind.CODE_SYMBOL, callee)
                graph.add_node(GraphNode(id=callee_id, kind=NodeKind.CODE_SYMBOL, label=callee))
                caller = current_symbol or file_id
                graph.add_edge(GraphEdge(source=caller, target=callee_id, kind=EdgeKind.CALLS))

    if kind in spec.import_kinds:
        try:
            modules = spec.import_modules(node, src)
        except Exception:
            modules = []
        for module in modules:
            module = module.strip()
            if not module:
                continue
            mod_id = make_node_id(NodeKind.MODULE, module)
            graph.add_node(GraphNode(id=mod_id, kind=NodeKind.MODULE, label=module))
            graph.add_edge(GraphEdge(source=file_id, target=mod_id, kind=EdgeKind.IMPORTS))

    for i in range(node.child_count()):
        _walk(node.child(i), src, rel_path, graph, spec, file_id, next_symbol)
