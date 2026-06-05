"""GitHub Actions workflow parser.

Uses ``pyyaml`` (via ``safe_load``) and is defensive about every nested shape
that might appear in a real workflow file. ``run:`` blocks are forwarded to
:func:`extract_command_edges`, which itself parses them with tree-sitter-bash.

Raises :class:`~repo_flow_mcp.parsers.tree_sitter_helpers.ParserError` when
the YAML is invalid or the top-level shape is not a mapping. The graph
builder catches the error and records a warning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from repo_flow_mcp.models import (
    EdgeKind,
    GraphDocument,
    GraphEdge,
    GraphNode,
    NodeKind,
    make_node_id,
)
from repo_flow_mcp.parsers.shell_parser import extract_command_edges
from repo_flow_mcp.parsers.tree_sitter_helpers import ParserError


def _load_yaml(rel_path: str, text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ParserError("github_actions", rel_path, f"invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ParserError(
            "github_actions",
            rel_path,
            f"top-level YAML must be a mapping, got {type(data).__name__}",
        )
    return data


def parse_github_actions(
    root: Path, rel_path: str, graph: GraphDocument, text: str
) -> None:
    data = _load_yaml(rel_path, text)
    if not data:
        return

    workflow_name_raw = data.get("name") or Path(rel_path).stem
    workflow_name = str(workflow_name_raw)
    workflow_id = make_node_id(NodeKind.WORKFLOW, workflow_name)
    graph.add_node(
        GraphNode(
            id=workflow_id,
            kind=NodeKind.WORKFLOW,
            label=workflow_name,
            path=rel_path,
        )
    )

    jobs = data.get("jobs") or {}
    if not isinstance(jobs, dict):
        # Malformed workflow: `jobs:` should always be a mapping.
        graph.warnings.append(
            f"github_actions {rel_path}: `jobs` is {type(jobs).__name__}, expected mapping"
        )
        return

    for job_name, job_def in jobs.items():
        job_id = make_node_id(NodeKind.WORKFLOW_JOB, workflow_name, str(job_name))
        graph.add_node(
            GraphNode(
                id=job_id,
                kind=NodeKind.WORKFLOW_JOB,
                label=str(job_name),
                path=rel_path,
            )
        )
        graph.add_edge(
            GraphEdge(source=workflow_id, target=job_id, kind=EdgeKind.DEFINES)
        )

        if not isinstance(job_def, dict):
            continue

        needs = job_def.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        if not isinstance(needs, list):
            needs = []
        for need in needs:
            dep_id = make_node_id(
                NodeKind.WORKFLOW_JOB, workflow_name, str(need)
            )
            graph.add_node(
                GraphNode(
                    id=dep_id,
                    kind=NodeKind.WORKFLOW_JOB,
                    label=str(need),
                    path=rel_path,
                )
            )
            graph.add_edge(
                GraphEdge(
                    source=job_id, target=dep_id, kind=EdgeKind.DEPENDS_ON
                )
            )

        steps = job_def.get("steps", [])
        if not isinstance(steps, list):
            continue

        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            step_label = str(step.get("name") or step.get("id") or f"step_{idx}")
            step_id = make_node_id(
                NodeKind.WORKFLOW_STEP, workflow_name, str(job_name), step_label
            )
            graph.add_node(
                GraphNode(
                    id=step_id,
                    kind=NodeKind.WORKFLOW_STEP,
                    label=step_label,
                    path=rel_path,
                )
            )
            graph.add_edge(
                GraphEdge(source=job_id, target=step_id, kind=EdgeKind.DEFINES)
            )

            run_block = step.get("run")
            if isinstance(run_block, str) and run_block.strip():
                # Parse the entire run block as one shell snippet so multi-line
                # pipelines, ``&&`` chains and command substitution are all
                # decomposed by tree-sitter-bash rather than line-by-line.
                try:
                    extract_command_edges(graph, step_id, run_block, rel_path)
                except ParserError as exc:
                    graph.warnings.append(str(exc))

            uses_value = step.get("uses")
            if isinstance(uses_value, str) and uses_value.strip():
                action_id = make_node_id(NodeKind.MODULE, "gha", uses_value)
                graph.add_node(
                    GraphNode(
                        id=action_id, kind=NodeKind.MODULE, label=uses_value
                    )
                )
                # NOTE: legacy code used USES_ENV here; preserved for back-compat.
                graph.add_edge(
                    GraphEdge(
                        source=step_id,
                        target=action_id,
                        kind=EdgeKind.USES_ENV,
                    )
                )
