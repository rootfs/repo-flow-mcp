from __future__ import annotations

import re
from pathlib import Path

from repo_flow_mcp.models import EdgeKind, GraphDocument, GraphEdge, GraphNode, NodeKind, make_node_id

CMAKE_TARGET_RE = re.compile(r"^\s*add_(?:library|executable)\(([^\s\)]+)", re.MULTILINE)
CMAKE_LINK_RE = re.compile(r"^\s*target_link_libraries\(([^\s\)]+)\s+([^\)]+)\)", re.MULTILINE)
BAZEL_RULE_RE = re.compile(r"\bname\s*=\s*\"([^\"]+)\"")
BAZEL_DEPS_RE = re.compile(r"\bdeps\s*=\s*\[(.*?)\]", re.DOTALL)
BAZEL_LABEL_RE = re.compile(r"\"([^\"]+)\"")


def parse_cmake(rel_path: str, graph: GraphDocument, text: str) -> None:
    targets = CMAKE_TARGET_RE.findall(text)
    for target in targets:
        target_id = make_node_id(NodeKind.TARGET, rel_path, target)
        graph.add_node(GraphNode(id=target_id, kind=NodeKind.TARGET, label=target, path=rel_path))

    for lhs, rhs in CMAKE_LINK_RE.findall(text):
        src_id = make_node_id(NodeKind.TARGET, rel_path, lhs)
        graph.add_node(GraphNode(id=src_id, kind=NodeKind.TARGET, label=lhs, path=rel_path))
        for dep in rhs.split():
            if dep.upper() in {"PUBLIC", "PRIVATE", "INTERFACE"}:
                continue
            dep_id = make_node_id(NodeKind.TARGET, rel_path, dep)
            graph.add_node(GraphNode(id=dep_id, kind=NodeKind.TARGET, label=dep, path=rel_path))
            graph.add_edge(GraphEdge(source=src_id, target=dep_id, kind=EdgeKind.DEPENDS_ON))


def parse_bazel_build(rel_path: str, graph: GraphDocument, text: str) -> None:
    names = BAZEL_RULE_RE.findall(text)
    for name in names:
        target_id = make_node_id(NodeKind.TARGET, rel_path, name)
        graph.add_node(GraphNode(id=target_id, kind=NodeKind.TARGET, label=name, path=rel_path))

    deps_blocks = BAZEL_DEPS_RE.findall(text)
    if not names:
        return
    current = make_node_id(NodeKind.TARGET, rel_path, names[0])
    for block in deps_blocks:
        for dep in BAZEL_LABEL_RE.findall(block):
            dep_id = make_node_id(NodeKind.TARGET, dep)
            graph.add_node(GraphNode(id=dep_id, kind=NodeKind.TARGET, label=dep, path=Path(rel_path).as_posix()))
            graph.add_edge(GraphEdge(source=current, target=dep_id, kind=EdgeKind.DEPENDS_ON))
