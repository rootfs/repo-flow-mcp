from __future__ import annotations

from pathlib import Path

import yaml

from repo_flow_mcp.models import EdgeKind, GraphDocument, GraphEdge, GraphNode, NodeKind, make_node_id
from repo_flow_mcp.parsers.shell_parser import extract_command_edges


def parse_gitlab_ci(rel_path: str, graph: GraphDocument, text: str) -> None:
    data = yaml.safe_load(text) or {}
    workflow_name = Path(rel_path).name
    workflow_id = make_node_id(NodeKind.WORKFLOW, "gitlab", workflow_name)
    graph.add_node(GraphNode(id=workflow_id, kind=NodeKind.WORKFLOW, label=workflow_name, path=rel_path))

    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if key in {"stages", "include", "default", "variables", "workflow"}:
            continue

        job_id = make_node_id(NodeKind.WORKFLOW_JOB, "gitlab", workflow_name, key)
        graph.add_node(GraphNode(id=job_id, kind=NodeKind.WORKFLOW_JOB, label=key, path=rel_path))
        graph.add_edge(GraphEdge(source=workflow_id, target=job_id, kind=EdgeKind.DEFINES))

        needs = value.get("needs", [])
        if isinstance(needs, list):
            for need in needs:
                need_name_raw = need.get("job") if isinstance(need, dict) else need
                need_name = str(need_name_raw) if need_name_raw is not None else "<unknown>"
                dep_id = make_node_id(NodeKind.WORKFLOW_JOB, "gitlab", workflow_name, need_name)
                graph.add_node(GraphNode(id=dep_id, kind=NodeKind.WORKFLOW_JOB, label=need_name, path=rel_path))
                graph.add_edge(GraphEdge(source=job_id, target=dep_id, kind=EdgeKind.DEPENDS_ON))

        script = value.get("script", [])
        if isinstance(script, str):
            script = [script]
        if isinstance(script, list):
            for idx, cmd in enumerate(script):
                if not isinstance(cmd, str):
                    continue
                step_id = make_node_id(NodeKind.WORKFLOW_STEP, "gitlab", workflow_name, key, str(idx))
                graph.add_node(
                    GraphNode(id=step_id, kind=NodeKind.WORKFLOW_STEP, label=f"{key}-step-{idx}", path=rel_path)
                )
                graph.add_edge(GraphEdge(source=job_id, target=step_id, kind=EdgeKind.DEFINES))
                extract_command_edges(graph, step_id, cmd, rel_path)


def parse_jenkinsfile(rel_path: str, graph: GraphDocument, text: str) -> None:
    workflow_name = Path(rel_path).name
    workflow_id = make_node_id(NodeKind.WORKFLOW, "jenkins", workflow_name)
    graph.add_node(GraphNode(id=workflow_id, kind=NodeKind.WORKFLOW, label=workflow_name, path=rel_path))

    stage_name: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("stage("):
            quote = "\"" if "\"" in line else "'"
            if quote in line:
                stage_name = line.split(quote)[1]
                stage_id = make_node_id(NodeKind.WORKFLOW_JOB, "jenkins", workflow_name, stage_name)
                graph.add_node(GraphNode(id=stage_id, kind=NodeKind.WORKFLOW_JOB, label=stage_name, path=rel_path))
                graph.add_edge(GraphEdge(source=workflow_id, target=stage_id, kind=EdgeKind.DEFINES))
            continue

        if line.startswith("sh ") and stage_name:
            cmd = line[3:].strip().strip("\"'")
            step_id = make_node_id(NodeKind.WORKFLOW_STEP, "jenkins", workflow_name, stage_name, cmd[:30])
            graph.add_node(GraphNode(id=step_id, kind=NodeKind.WORKFLOW_STEP, label=cmd[:60], path=rel_path))
            stage_id = make_node_id(NodeKind.WORKFLOW_JOB, "jenkins", workflow_name, stage_name)
            graph.add_edge(GraphEdge(source=stage_id, target=step_id, kind=EdgeKind.DEFINES))
            extract_command_edges(graph, step_id, cmd, rel_path)
