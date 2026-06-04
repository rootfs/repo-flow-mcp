from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class NodeKind(StrEnum):
    FILE = "file"
    CODE_SYMBOL = "code_symbol"
    MODULE = "module"
    SCRIPT = "script"
    TARGET = "target"
    WORKFLOW = "workflow"
    WORKFLOW_JOB = "workflow_job"
    WORKFLOW_STEP = "workflow_step"
    ARTIFACT = "artifact"
    ENV_VAR = "env_var"
    CONTAINER_IMAGE = "container_image"
    SERVICE = "service"


class EdgeKind(StrEnum):
    IMPORTS = "imports"
    CALLS = "calls"
    DEFINES = "defines"
    INVOKES = "invokes"
    DEPENDS_ON = "depends_on"
    PRODUCES = "produces"
    CONSUMES = "consumes"
    SETS_ENV = "sets_env"
    USES_ENV = "uses_env"
    RUNS_IN = "runs_in"
    BUILDS_FROM = "builds_from"


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: NodeKind
    label: str
    path: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: EdgeKind
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class GraphDocument:
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: set[tuple[str, str, str]] = field(default_factory=set)
    edge_rows: list[GraphEdge] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_node(self, node: GraphNode) -> None:
        if node.id not in self.nodes:
            self.nodes[node.id] = node

    def add_edge(self, edge: GraphEdge) -> None:
        key = (edge.source, edge.target, edge.kind.value)
        if key in self.edges:
            return
        self.edges.add(key)
        self.edge_rows.append(edge)

    def to_dict(self) -> dict[str, object]:
        nodes = [
            {
                "id": node.id,
                "kind": node.kind.value,
                "label": node.label,
                "path": node.path,
                "metadata": node.metadata,
            }
            for node in sorted(self.nodes.values(), key=lambda x: x.id)
        ]
        edges = [
            {
                "source": edge.source,
                "target": edge.target,
                "kind": edge.kind.value,
                "metadata": edge.metadata,
            }
            for edge in sorted(
                self.edge_rows,
                key=lambda x: (x.kind.value, x.source, x.target),
            )
        ]
        return {
            "nodes": nodes,
            "edges": edges,
            "warnings": self.warnings,
            "stats": {
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        }


def make_node_id(kind: NodeKind, *parts: str) -> str:
    cleaned = [p.strip().replace(" ", "_") for p in parts if p and p.strip()]
    return f"{kind.value}:{':'.join(cleaned)}"
