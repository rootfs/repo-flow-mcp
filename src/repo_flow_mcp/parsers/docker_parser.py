from __future__ import annotations

from pathlib import Path

import yaml

from repo_flow_mcp.models import EdgeKind, GraphDocument, GraphEdge, GraphNode, NodeKind, make_node_id


def _parse_dockerfile(rel_path: str, graph: GraphDocument, text: str) -> None:
    docker_id = make_node_id(NodeKind.FILE, rel_path)
    graph.add_node(GraphNode(id=docker_id, kind=NodeKind.FILE, label=Path(rel_path).name, path=rel_path))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("FROM "):
            base = line.split(maxsplit=1)[1]
            base_id = make_node_id(NodeKind.CONTAINER_IMAGE, base)
            graph.add_node(GraphNode(id=base_id, kind=NodeKind.CONTAINER_IMAGE, label=base))
            graph.add_edge(GraphEdge(source=docker_id, target=base_id, kind=EdgeKind.BUILDS_FROM))
        if upper.startswith("COPY ") or upper.startswith("ADD "):
            parts = line.split()
            if len(parts) >= 3:
                artifact = parts[-2]
                artifact_id = make_node_id(NodeKind.ARTIFACT, artifact)
                graph.add_node(GraphNode(id=artifact_id, kind=NodeKind.ARTIFACT, label=artifact))
                graph.add_edge(GraphEdge(source=docker_id, target=artifact_id, kind=EdgeKind.CONSUMES))


def _parse_compose(rel_path: str, graph: GraphDocument, text: str) -> None:
    data = yaml.safe_load(text) or {}
    services = data.get("services", {})
    for svc_name, svc in services.items():
        svc_id = make_node_id(NodeKind.SERVICE, svc_name)
        graph.add_node(GraphNode(id=svc_id, kind=NodeKind.SERVICE, label=svc_name, path=rel_path))

        depends = svc.get("depends_on", []) if isinstance(svc, dict) else []
        if isinstance(depends, dict):
            depends = list(depends.keys())
        for dep in depends:
            dep_id = make_node_id(NodeKind.SERVICE, str(dep))
            graph.add_node(GraphNode(id=dep_id, kind=NodeKind.SERVICE, label=str(dep), path=rel_path))
            graph.add_edge(GraphEdge(source=svc_id, target=dep_id, kind=EdgeKind.DEPENDS_ON))

        image = svc.get("image") if isinstance(svc, dict) else None
        if image:
            image_id = make_node_id(NodeKind.CONTAINER_IMAGE, str(image))
            graph.add_node(GraphNode(id=image_id, kind=NodeKind.CONTAINER_IMAGE, label=str(image)))
            graph.add_edge(GraphEdge(source=svc_id, target=image_id, kind=EdgeKind.RUNS_IN))


def parse_docker_related(root: Path, rel_path: str, graph: GraphDocument, text: str) -> None:
    name = Path(rel_path).name.lower()
    if name == "dockerfile":
        _parse_dockerfile(rel_path, graph, text)
    if name in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}:
        _parse_compose(rel_path, graph, text)
