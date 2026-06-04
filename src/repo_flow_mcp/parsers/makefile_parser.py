from __future__ import annotations

import re
from pathlib import Path

from repo_flow_mcp.models import EdgeKind, GraphDocument, GraphEdge, GraphNode, NodeKind, make_node_id
from repo_flow_mcp.parsers.shell_parser import extract_command_edges

TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+):\s*(.*)$")


def parse_makefile(root: Path, rel_path: str, graph: GraphDocument, text: str) -> None:
    current_target_id: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        match = TARGET_RE.match(line)
        if match and not line.startswith("\t"):
            target_name = match.group(1)
            deps = [d for d in match.group(2).split() if d]
            target_id = make_node_id(NodeKind.TARGET, rel_path, target_name)
            graph.add_node(
                GraphNode(
                    id=target_id,
                    kind=NodeKind.TARGET,
                    label=target_name,
                    path=rel_path,
                )
            )
            for dep in deps:
                dep_id = make_node_id(NodeKind.TARGET, rel_path, dep)
                graph.add_node(GraphNode(id=dep_id, kind=NodeKind.TARGET, label=dep, path=rel_path))
                graph.add_edge(GraphEdge(source=target_id, target=dep_id, kind=EdgeKind.DEPENDS_ON))
            current_target_id = target_id
            continue

        if line.startswith("\t") and current_target_id:
            cmd = line.strip()
            extract_command_edges(graph, current_target_id, cmd, rel_path)
