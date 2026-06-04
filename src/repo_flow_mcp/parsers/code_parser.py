from __future__ import annotations

import ast
import re
from pathlib import Path

from repo_flow_mcp.models import EdgeKind, GraphDocument, GraphEdge, GraphNode, NodeKind, make_node_id

JS_IMPORT_RE = re.compile(r"(?:import\s+.*?from\s+|require\()['\"]([^'\"]+)['\"]")
JS_DYNAMIC_IMPORT_RE = re.compile(r"import\(\s*['\"]([^'\"]+)['\"]\s*\)")
JS_FUNCTION_DEF_RE = re.compile(r"(?:^|\n)\s*(?:export\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)
JS_CLASS_DEF_RE = re.compile(r"(?:^|\n)\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)\b", re.MULTILINE)
JS_ARROW_DEF_RE = re.compile(
    r"(?:^|\n)\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\([^\)]*\)\s*=>",
    re.MULTILINE,
)
JS_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
GO_IMPORT_SINGLE_RE = re.compile(r"^\s*import\s+\"([^\"]+)\"", re.MULTILINE)
GO_IMPORT_BLOCK_RE = re.compile(r"^\s*import\s*\((.*?)\)", re.MULTILINE | re.DOTALL)
GO_IMPORT_IN_BLOCK_RE = re.compile(r"\"([^\"]+)\"")
RUST_USE_RE = re.compile(r"^\s*use\s+([a-zA-Z0-9_:{}*]+)\s*;", re.MULTILINE)
JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([a-zA-Z0-9_.*]+)\s*;", re.MULTILINE)
CPP_INCLUDE_RE = re.compile(r"^\s*#include\s*[<\"]([^>\"]+)[>\"]", re.MULTILINE)


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str, graph: GraphDocument) -> None:
        self.rel_path = rel_path
        self.graph = graph
        self.current_symbol: str | None = None

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            module = alias.name
            mod_id = make_node_id(NodeKind.MODULE, module)
            self.graph.add_node(GraphNode(id=mod_id, kind=NodeKind.MODULE, label=module))
            file_id = make_node_id(NodeKind.FILE, self.rel_path)
            self.graph.add_edge(GraphEdge(source=file_id, target=mod_id, kind=EdgeKind.IMPORTS))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or "<relative>"
        mod_id = make_node_id(NodeKind.MODULE, module)
        self.graph.add_node(GraphNode(id=mod_id, kind=NodeKind.MODULE, label=module))
        file_id = make_node_id(NodeKind.FILE, self.rel_path)
        self.graph.add_edge(GraphEdge(source=file_id, target=mod_id, kind=EdgeKind.IMPORTS))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        symbol_id = make_node_id(NodeKind.CODE_SYMBOL, self.rel_path, node.name)
        file_id = make_node_id(NodeKind.FILE, self.rel_path)
        self.graph.add_node(
            GraphNode(
                id=symbol_id,
                kind=NodeKind.CODE_SYMBOL,
                label=node.name,
                path=self.rel_path,
                metadata={"line": str(node.lineno)},
            )
        )
        self.graph.add_edge(GraphEdge(source=file_id, target=symbol_id, kind=EdgeKind.DEFINES))

        previous = self.current_symbol
        self.current_symbol = symbol_id
        self.generic_visit(node)
        self.current_symbol = previous

    def visit_Call(self, node: ast.Call) -> None:
        caller = self.current_symbol or make_node_id(NodeKind.FILE, self.rel_path)
        callee_label = None
        if isinstance(node.func, ast.Name):
            callee_label = node.func.id
        elif isinstance(node.func, ast.Attribute):
            callee_label = node.func.attr
        if callee_label:
            callee_id = make_node_id(NodeKind.CODE_SYMBOL, callee_label)
            self.graph.add_node(GraphNode(id=callee_id, kind=NodeKind.CODE_SYMBOL, label=callee_label))
            self.graph.add_edge(GraphEdge(source=caller, target=callee_id, kind=EdgeKind.CALLS))
        self.generic_visit(node)


def _parse_python(rel_path: str, graph: GraphDocument, text: str) -> None:
    file_id = make_node_id(NodeKind.FILE, rel_path)
    graph.add_node(GraphNode(id=file_id, kind=NodeKind.FILE, label=Path(rel_path).name, path=rel_path))
    tree = ast.parse(text)
    _PyVisitor(rel_path, graph).visit(tree)


def _parse_jsts(rel_path: str, graph: GraphDocument, text: str) -> None:
    file_id = make_node_id(NodeKind.FILE, rel_path)
    graph.add_node(GraphNode(id=file_id, kind=NodeKind.FILE, label=Path(rel_path).name, path=rel_path))

    imports = JS_IMPORT_RE.findall(text)
    imports.extend(JS_DYNAMIC_IMPORT_RE.findall(text))
    for match in imports:
        mod_id = make_node_id(NodeKind.MODULE, match)
        graph.add_node(GraphNode(id=mod_id, kind=NodeKind.MODULE, label=match))
        graph.add_edge(GraphEdge(source=file_id, target=mod_id, kind=EdgeKind.IMPORTS))

    symbol_ranges: list[tuple[int, int, str]] = []
    for regex in (JS_FUNCTION_DEF_RE, JS_CLASS_DEF_RE, JS_ARROW_DEF_RE):
        for found in regex.finditer(text):
            name = found.group(1)
            symbol_id = make_node_id(NodeKind.CODE_SYMBOL, rel_path, name)
            graph.add_node(
                GraphNode(
                    id=symbol_id,
                    kind=NodeKind.CODE_SYMBOL,
                    label=name,
                    path=rel_path,
                    metadata={"line": str(text.count("\n", 0, found.start()) + 1)},
                )
            )
            graph.add_edge(GraphEdge(source=file_id, target=symbol_id, kind=EdgeKind.DEFINES))
            symbol_ranges.append((found.start(), len(text), symbol_id))

    # Rough symbol range assignment: nearest preceding symbol declaration is treated as caller context.
    symbol_ranges.sort(key=lambda x: x[0])

    keyword_calls = {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "typeof",
        "new",
        "import",
    }
    for call in JS_CALL_RE.finditer(text):
        callee = call.group(1)
        if callee in keyword_calls:
            continue
        caller = file_id
        for start, _end, symbol_id in symbol_ranges:
            if start <= call.start():
                caller = symbol_id
            else:
                break
        callee_id = make_node_id(NodeKind.CODE_SYMBOL, callee)
        graph.add_node(GraphNode(id=callee_id, kind=NodeKind.CODE_SYMBOL, label=callee))
        graph.add_edge(GraphEdge(source=caller, target=callee_id, kind=EdgeKind.CALLS))


def _parse_generic_imports(rel_path: str, graph: GraphDocument, modules: list[str]) -> None:
    file_id = make_node_id(NodeKind.FILE, rel_path)
    graph.add_node(GraphNode(id=file_id, kind=NodeKind.FILE, label=Path(rel_path).name, path=rel_path))
    for module in modules:
        mod_id = make_node_id(NodeKind.MODULE, module)
        graph.add_node(GraphNode(id=mod_id, kind=NodeKind.MODULE, label=module))
        graph.add_edge(GraphEdge(source=file_id, target=mod_id, kind=EdgeKind.IMPORTS))


def _parse_go(rel_path: str, graph: GraphDocument, text: str) -> None:
    modules = GO_IMPORT_SINGLE_RE.findall(text)
    for block in GO_IMPORT_BLOCK_RE.findall(text):
        modules.extend(GO_IMPORT_IN_BLOCK_RE.findall(block))
    _parse_generic_imports(rel_path, graph, modules)


def _parse_rust(rel_path: str, graph: GraphDocument, text: str) -> None:
    modules = RUST_USE_RE.findall(text)
    _parse_generic_imports(rel_path, graph, modules)


def _parse_java(rel_path: str, graph: GraphDocument, text: str) -> None:
    modules = JAVA_IMPORT_RE.findall(text)
    _parse_generic_imports(rel_path, graph, modules)


def _parse_cpp(rel_path: str, graph: GraphDocument, text: str) -> None:
    modules = CPP_INCLUDE_RE.findall(text)
    _parse_generic_imports(rel_path, graph, modules)


def parse_code_file(root: Path, rel_path: str, graph: GraphDocument, text: str) -> None:
    suffix = Path(rel_path).suffix.lower()
    if suffix == ".py":
        _parse_python(rel_path, graph, text)
        return
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        _parse_jsts(rel_path, graph, text)
        return
    if suffix == ".go":
        _parse_go(rel_path, graph, text)
        return
    if suffix == ".rs":
        _parse_rust(rel_path, graph, text)
        return
    if suffix == ".java":
        _parse_java(rel_path, graph, text)
        return
    if suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}:
        _parse_cpp(rel_path, graph, text)
