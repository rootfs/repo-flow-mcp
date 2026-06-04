from __future__ import annotations

import re
import shlex
from pathlib import Path

from repo_flow_mcp.models import EdgeKind, GraphDocument, GraphEdge, GraphNode, NodeKind, make_node_id

ASSIGNMENT_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.+)$")
ARTIFACT_HINT_RE = re.compile(r"([\w./-]+\.(?:pt|pth|ckpt|bin|jsonl?|csv|parquet|tar(?:\.gz)?|zip))")


def _split_command(line: str) -> list[str]:
    try:
        return shlex.split(line, comments=True, posix=True)
    except ValueError:
        return line.split()


def extract_command_edges(graph: GraphDocument, source_node_id: str, cmd: str, rel_path: str) -> None:
    parts = _split_command(cmd)
    if not parts:
        return

    for part in parts:
        env_match = ASSIGNMENT_RE.match(part)
        if env_match:
            env_name = env_match.group(1)
            env_id = make_node_id(NodeKind.ENV_VAR, env_name)
            graph.add_node(GraphNode(id=env_id, kind=NodeKind.ENV_VAR, label=env_name))
            graph.add_edge(GraphEdge(source=source_node_id, target=env_id, kind=EdgeKind.SETS_ENV))

    head = parts[0]
    invoke_targets: list[tuple[NodeKind, str]] = []

    if head in {"bash", "sh", "zsh"} and len(parts) >= 2:
        invoke_targets.append((NodeKind.SCRIPT, parts[1]))
    elif head in {"source", "."} and len(parts) >= 2:
        invoke_targets.append((NodeKind.SCRIPT, parts[1]))
    elif head.startswith("./"):
        invoke_targets.append((NodeKind.SCRIPT, head))
    elif head == "make" and len(parts) >= 2:
        invoke_targets.append((NodeKind.TARGET, parts[1]))
    elif head in {"python", "python3"} and len(parts) >= 2:
        invoke_targets.append((NodeKind.SCRIPT, parts[1]))
    elif head in {"npm", "pnpm", "yarn"} and len(parts) >= 3 and parts[1] in {"run", "exec"}:
        invoke_targets.append((NodeKind.TARGET, f"pkg:{parts[2]}"))
    elif head in {"docker", "docker-compose", "compose"}:
        invoke_targets.append((NodeKind.SERVICE, "container-runtime"))

    for kind, raw_target in invoke_targets:
        target = str(Path(rel_path).parent.joinpath(raw_target).as_posix()) if "/" in raw_target else raw_target
        target_id = make_node_id(kind, target)
        graph.add_node(GraphNode(id=target_id, kind=kind, label=raw_target, path=target if "/" in target else None))
        graph.add_edge(
            GraphEdge(
                source=source_node_id,
                target=target_id,
                kind=EdgeKind.INVOKES,
                metadata={"command": cmd},
            )
        )

    for match in ARTIFACT_HINT_RE.findall(cmd):
        artifact_id = make_node_id(NodeKind.ARTIFACT, match)
        graph.add_node(GraphNode(id=artifact_id, kind=NodeKind.ARTIFACT, label=match))
        if any(flag in cmd for flag in ("--output", "-o", "save", "write", "export")):
            graph.add_edge(GraphEdge(source=source_node_id, target=artifact_id, kind=EdgeKind.PRODUCES))
        else:
            graph.add_edge(GraphEdge(source=source_node_id, target=artifact_id, kind=EdgeKind.CONSUMES))


def parse_shell_script(root: Path, rel_path: str, graph: GraphDocument, text: str) -> None:
    script_id = make_node_id(NodeKind.SCRIPT, rel_path)
    graph.add_node(
        GraphNode(
            id=script_id,
            kind=NodeKind.SCRIPT,
            label=Path(rel_path).name,
            path=rel_path,
        )
    )

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        extract_command_edges(graph, script_id, line, rel_path)
