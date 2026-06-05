"""GitLab CI + Jenkinsfile parsers.

GitLab CI uses ``pyyaml`` with strict shape validation; malformed YAML raises
:class:`~repo_flow_mcp.parsers.tree_sitter_helpers.ParserError`. Job scripts
are passed through :func:`extract_command_edges` (tree-sitter-bash).

Jenkinsfile parsing stays on the legacy regex scan: there is no robust
Groovy parser shipped in ``tree-sitter-language-pack`` and the Jenkinsfile
contribution to the graph is intentionally minimal.
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


# Top-level GitLab CI keys that are NOT jobs.
_GITLAB_RESERVED_KEYS = {
    "stages",
    "include",
    "default",
    "variables",
    "workflow",
    "image",
    "services",
    "cache",
    "before_script",
    "after_script",
    "pages",  # `pages` IS a job name in many setups, but the legacy parser
    # treated it as a job too because it's a dict — keep that.
}
# Refine: ``pages`` is actually a job in most CI files. Remove from reserved.
_GITLAB_RESERVED_KEYS.discard("pages")


def _load_yaml(rel_path: str, text: str, parser_name: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ParserError(parser_name, rel_path, f"invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ParserError(
            parser_name,
            rel_path,
            f"top-level YAML must be a mapping, got {type(data).__name__}",
        )
    return data


def parse_gitlab_ci(rel_path: str, graph: GraphDocument, text: str) -> None:
    data = _load_yaml(rel_path, text, parser_name="gitlab_ci")
    if not data:
        return

    workflow_name = Path(rel_path).name
    workflow_id = make_node_id(NodeKind.WORKFLOW, "gitlab", workflow_name)
    graph.add_node(
        GraphNode(
            id=workflow_id,
            kind=NodeKind.WORKFLOW,
            label=workflow_name,
            path=rel_path,
        )
    )

    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if key in _GITLAB_RESERVED_KEYS:
            continue

        job_id = make_node_id(
            NodeKind.WORKFLOW_JOB, "gitlab", workflow_name, str(key)
        )
        graph.add_node(
            GraphNode(
                id=job_id,
                kind=NodeKind.WORKFLOW_JOB,
                label=str(key),
                path=rel_path,
            )
        )
        graph.add_edge(
            GraphEdge(source=workflow_id, target=job_id, kind=EdgeKind.DEFINES)
        )

        needs = value.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        if isinstance(needs, list):
            for need in needs:
                need_name_raw = (
                    need.get("job") if isinstance(need, dict) else need
                )
                need_name = (
                    str(need_name_raw) if need_name_raw is not None else "<unknown>"
                )
                dep_id = make_node_id(
                    NodeKind.WORKFLOW_JOB, "gitlab", workflow_name, need_name
                )
                graph.add_node(
                    GraphNode(
                        id=dep_id,
                        kind=NodeKind.WORKFLOW_JOB,
                        label=need_name,
                        path=rel_path,
                    )
                )
                graph.add_edge(
                    GraphEdge(
                        source=job_id,
                        target=dep_id,
                        kind=EdgeKind.DEPENDS_ON,
                    )
                )

        script = value.get("script", [])
        if isinstance(script, str):
            script = [script]
        if not isinstance(script, list):
            continue
        for idx, cmd in enumerate(script):
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            step_id = make_node_id(
                NodeKind.WORKFLOW_STEP,
                "gitlab",
                workflow_name,
                str(key),
                str(idx),
            )
            graph.add_node(
                GraphNode(
                    id=step_id,
                    kind=NodeKind.WORKFLOW_STEP,
                    label=f"{key}-step-{idx}",
                    path=rel_path,
                )
            )
            graph.add_edge(
                GraphEdge(source=job_id, target=step_id, kind=EdgeKind.DEFINES)
            )
            try:
                extract_command_edges(graph, step_id, cmd, rel_path)
            except ParserError as exc:
                graph.warnings.append(str(exc))


def parse_jenkinsfile(rel_path: str, graph: GraphDocument, text: str) -> None:
    """Minimal Jenkinsfile (Groovy DSL) parser.

    We do not attempt to parse Groovy; we only pull out ``stage("name")`` and
    ``sh "cmd"`` patterns so that the graph at least connects stages to
    commands. Anything richer would need a real Groovy parser.
    """

    workflow_name = Path(rel_path).name
    workflow_id = make_node_id(NodeKind.WORKFLOW, "jenkins", workflow_name)
    graph.add_node(
        GraphNode(
            id=workflow_id,
            kind=NodeKind.WORKFLOW,
            label=workflow_name,
            path=rel_path,
        )
    )

    stage_name: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("stage("):
            quote = '"' if '"' in line else "'"
            if quote in line:
                stage_name = line.split(quote)[1]
                stage_id = make_node_id(
                    NodeKind.WORKFLOW_JOB,
                    "jenkins",
                    workflow_name,
                    stage_name,
                )
                graph.add_node(
                    GraphNode(
                        id=stage_id,
                        kind=NodeKind.WORKFLOW_JOB,
                        label=stage_name,
                        path=rel_path,
                    )
                )
                graph.add_edge(
                    GraphEdge(
                        source=workflow_id,
                        target=stage_id,
                        kind=EdgeKind.DEFINES,
                    )
                )
            continue

        if line.startswith("sh ") and stage_name:
            cmd = line[3:].strip().strip("\"'")
            step_id = make_node_id(
                NodeKind.WORKFLOW_STEP,
                "jenkins",
                workflow_name,
                stage_name,
                cmd[:30],
            )
            graph.add_node(
                GraphNode(
                    id=step_id,
                    kind=NodeKind.WORKFLOW_STEP,
                    label=cmd[:60],
                    path=rel_path,
                )
            )
            stage_id = make_node_id(
                NodeKind.WORKFLOW_JOB, "jenkins", workflow_name, stage_name
            )
            graph.add_edge(
                GraphEdge(
                    source=stage_id, target=step_id, kind=EdgeKind.DEFINES
                )
            )
            try:
                extract_command_edges(graph, step_id, cmd, rel_path)
            except ParserError as exc:
                graph.warnings.append(str(exc))
