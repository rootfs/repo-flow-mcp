from __future__ import annotations

import re
from pathlib import Path

from repo_flow_mcp.models import EdgeKind, GraphDocument, GraphEdge, GraphNode, NodeKind, make_node_id

MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^\)]+)\)")
MD_INLINE_TOOL_RE = re.compile(r"`([A-Za-z_][\w.-]+)`")
MD_TOOL_LIST_RE = re.compile(r"^\s*-\s*([A-Za-z_][\w.-]+)\s*$", re.MULTILINE)


def _is_local_link(link: str) -> bool:
    lowered = link.lower()
    return not (
        lowered.startswith("http://")
        or lowered.startswith("https://")
        or lowered.startswith("mailto:")
        or lowered.startswith("#")
    )


def parse_markdown_dependencies(rel_path: str, graph: GraphDocument, text: str) -> None:
    file_id = make_node_id(NodeKind.FILE, rel_path)
    graph.add_node(GraphNode(id=file_id, kind=NodeKind.FILE, label=Path(rel_path).name, path=rel_path))

    for link in MD_LINK_RE.findall(text):
        clean = link.strip()
        if not _is_local_link(clean):
            continue
        target = clean.split("#", maxsplit=1)[0].strip()
        if not target:
            continue
        target_path = Path(rel_path).parent.joinpath(target).as_posix()
        target_id = make_node_id(NodeKind.FILE, target_path)
        graph.add_node(GraphNode(id=target_id, kind=NodeKind.FILE, label=Path(target_path).name, path=target_path))
        graph.add_edge(GraphEdge(source=file_id, target=target_id, kind=EdgeKind.DEPENDS_ON))

    tool_names: set[str] = set(MD_TOOL_LIST_RE.findall(text))
    for inline in MD_INLINE_TOOL_RE.findall(text):
        if "/" in inline or " " in inline:
            continue
        if len(inline) >= 3:
            tool_names.add(inline)

    for tool in sorted(tool_names):
        tool_id = make_node_id(NodeKind.MODULE, "tool", tool)
        graph.add_node(GraphNode(id=tool_id, kind=NodeKind.MODULE, label=f"tool:{tool}"))
        graph.add_edge(GraphEdge(source=file_id, target=tool_id, kind=EdgeKind.DEPENDS_ON))
