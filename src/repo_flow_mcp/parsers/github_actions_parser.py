from __future__ import annotations

from pathlib import Path

import yaml

from repo_flow_mcp.models import EdgeKind, GraphDocument, GraphEdge, GraphNode, NodeKind, make_node_id
from repo_flow_mcp.parsers.shell_parser import extract_command_edges


def parse_github_actions(root: Path, rel_path: str, graph: GraphDocument, text: str) -> None:
    data = yaml.safe_load(text) or {}
    workflow_name = data.get("name") or Path(rel_path).stem
    workflow_id = make_node_id(NodeKind.WORKFLOW, workflow_name)
    graph.add_node(
        GraphNode(
            id=workflow_id,
            kind=NodeKind.WORKFLOW,
            label=workflow_name,
            path=rel_path,
        )
    )

    jobs = data.get("jobs", {})
    for job_name, job_def in jobs.items():
        job_id = make_node_id(NodeKind.WORKFLOW_JOB, workflow_name, job_name)
        graph.add_node(GraphNode(id=job_id, kind=NodeKind.WORKFLOW_JOB, label=job_name, path=rel_path))
        graph.add_edge(GraphEdge(source=workflow_id, target=job_id, kind=EdgeKind.DEFINES))

        needs = job_def.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        for need in needs:
            dep_id = make_node_id(NodeKind.WORKFLOW_JOB, workflow_name, str(need))
            graph.add_node(GraphNode(id=dep_id, kind=NodeKind.WORKFLOW_JOB, label=str(need), path=rel_path))
            graph.add_edge(GraphEdge(source=job_id, target=dep_id, kind=EdgeKind.DEPENDS_ON))

        steps = job_def.get("steps", [])
        for idx, step in enumerate(steps):
            step_label = str(step.get("name") or step.get("id") or f"step_{idx}")
            step_id = make_node_id(NodeKind.WORKFLOW_STEP, workflow_name, job_name, step_label)
            graph.add_node(GraphNode(id=step_id, kind=NodeKind.WORKFLOW_STEP, label=step_label, path=rel_path))
            graph.add_edge(GraphEdge(source=job_id, target=step_id, kind=EdgeKind.DEFINES))

            run_block = step.get("run")
            if isinstance(run_block, str):
                for run_line in run_block.splitlines():
                    cmd = run_line.strip()
                    if cmd:
                        extract_command_edges(graph, step_id, cmd, rel_path)

            uses_value = step.get("uses")
            if isinstance(uses_value, str):
                action_id = make_node_id(NodeKind.MODULE, "gha", uses_value)
                graph.add_node(GraphNode(id=action_id, kind=NodeKind.MODULE, label=uses_value))
                graph.add_edge(GraphEdge(source=step_id, target=action_id, kind=EdgeKind.USES_ENV))
