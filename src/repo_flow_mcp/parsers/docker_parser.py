"""Dockerfile + Compose parser.

Dockerfiles are parsed with ``tree-sitter-dockerfile``; compose files stay on
``pyyaml``. Both emit ``CONTAINER_IMAGE`` / ``SERVICE`` / ``ARTIFACT`` nodes
plus ``BUILDS_FROM`` / ``DEPENDS_ON`` / ``RUNS_IN`` / ``CONSUMES`` edges.

The Dockerfile branch handles features the old regex line scan missed:

* Multi-stage builds (``FROM base AS builder``); each stage's alias is also
  recorded as a ``CONTAINER_IMAGE`` so ``COPY --from=<alias>`` resolves.
* Line continuations in ``RUN`` instructions.
* ``COPY --from=<alias>`` (the ``--from`` parameter is skipped from the
  artifact list and turned into a ``DEPENDS_ON`` edge to the alias node).
* ``ARG`` in ``FROM`` (``FROM python:${PY}-slim``) — kept verbatim.

Raises :class:`~repo_flow_mcp.parsers.tree_sitter_helpers.ParserError` on
malformed Dockerfile or YAML input.
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
from repo_flow_mcp.parsers.tree_sitter_helpers import (
    ParserError,
    get_parser_or_raise,
    iter_children,
    node_text,
    parse_or_raise,
    walk,
)

Node = Any


def _image_spec_text(image_spec: Node, src: bytes) -> str:
    """Reconstruct the full image-spec text (e.g. ``python:3.11-slim``)."""

    return node_text(image_spec, src).strip()


def _alias_of(from_instr: Node, src: bytes) -> str | None:
    for child in iter_children(from_instr):
        if child.kind() == "image_alias":
            return node_text(child, src).strip()
    return None


def _emit_image(graph: GraphDocument, label: str) -> str:
    image_id = make_node_id(NodeKind.CONTAINER_IMAGE, label)
    graph.add_node(
        GraphNode(id=image_id, kind=NodeKind.CONTAINER_IMAGE, label=label)
    )
    return image_id


def _copy_paths_and_params(
    instr: Node, src: bytes
) -> tuple[list[str], dict[str, str]]:
    """Split a COPY/ADD instruction into ``([paths], {param_name: value})``."""

    paths: list[str] = []
    params: dict[str, str] = {}
    for child in iter_children(instr):
        kind = child.kind()
        if kind == "path":
            paths.append(node_text(child, src).strip())
        elif kind == "param":
            text = node_text(child, src).strip().lstrip("-")
            if "=" in text:
                name, value = text.split("=", 1)
                params[name.strip()] = value.strip()
    return paths, params


def _parse_dockerfile(rel_path: str, graph: GraphDocument, text: str) -> None:
    docker_id = make_node_id(NodeKind.FILE, rel_path)
    graph.add_node(
        GraphNode(
            id=docker_id,
            kind=NodeKind.FILE,
            label=Path(rel_path).name,
            path=rel_path,
        )
    )

    if not text.strip():
        return

    parser = get_parser_or_raise(
        "dockerfile", parser_name="dockerfile", path=rel_path
    )
    tree = parse_or_raise(
        parser, text, parser_name="dockerfile", path=rel_path
    )
    src = text.encode("utf-8", errors="replace")

    # Stage aliases (``FROM foo AS bar``) so subsequent ``COPY --from=bar``
    # can resolve to a known image node.
    aliases: dict[str, str] = {}

    for node in walk(tree.root_node()):
        kind = node.kind()

        if kind == "from_instruction":
            image_spec = None
            for child in iter_children(node):
                if child.kind() == "image_spec":
                    image_spec = child
                    break
            if image_spec is None:
                continue
            base_label = _image_spec_text(image_spec, src)
            base_id = _emit_image(graph, base_label)
            graph.add_edge(
                GraphEdge(
                    source=docker_id, target=base_id, kind=EdgeKind.BUILDS_FROM
                )
            )
            alias = _alias_of(node, src)
            if alias:
                # Treat the stage alias itself as a named image so later
                # ``COPY --from=<alias>`` can hook into the graph.
                alias_id = _emit_image(graph, alias)
                aliases[alias] = alias_id

        elif kind in {"copy_instruction", "add_instruction"}:
            paths, params = _copy_paths_and_params(node, src)
            # Last path is the destination; everything before is sources.
            sources = paths[:-1] if len(paths) >= 2 else paths
            from_alias = params.get("from")

            if from_alias:
                resolved_alias_id = aliases.get(from_alias)
                if resolved_alias_id is None:
                    # Reference a stage we haven't (yet) seen — register it.
                    resolved_alias_id = _emit_image(graph, from_alias)
                    aliases[from_alias] = resolved_alias_id
                graph.add_edge(
                    GraphEdge(
                        source=docker_id,
                        target=resolved_alias_id,
                        kind=EdgeKind.DEPENDS_ON,
                    )
                )
                continue

            for source_path in sources:
                if not source_path:
                    continue
                artifact_id = make_node_id(NodeKind.ARTIFACT, source_path)
                graph.add_node(
                    GraphNode(
                        id=artifact_id,
                        kind=NodeKind.ARTIFACT,
                        label=source_path,
                    )
                )
                graph.add_edge(
                    GraphEdge(
                        source=docker_id,
                        target=artifact_id,
                        kind=EdgeKind.CONSUMES,
                    )
                )


def _parse_compose(rel_path: str, graph: GraphDocument, text: str) -> None:
    if not text.strip():
        return
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ParserError("compose", rel_path, f"invalid YAML: {exc}") from exc
    if data is None:
        return
    if not isinstance(data, dict):
        raise ParserError(
            "compose", rel_path, f"top-level YAML must be a mapping, got {type(data).__name__}"
        )

    services_raw = data.get("services") or {}
    if not isinstance(services_raw, dict):
        raise ParserError(
            "compose",
            rel_path,
            f"`services` must be a mapping, got {type(services_raw).__name__}",
        )
    services: dict[str, Any] = services_raw

    for svc_name, svc in services.items():
        svc_id = make_node_id(NodeKind.SERVICE, svc_name)
        graph.add_node(
            GraphNode(
                id=svc_id,
                kind=NodeKind.SERVICE,
                label=str(svc_name),
                path=rel_path,
            )
        )

        if not isinstance(svc, dict):
            continue

        depends = svc.get("depends_on", []) or []
        if isinstance(depends, dict):
            depends = list(depends.keys())
        if not isinstance(depends, list):
            depends = []
        for dep in depends:
            dep_id = make_node_id(NodeKind.SERVICE, str(dep))
            graph.add_node(
                GraphNode(
                    id=dep_id,
                    kind=NodeKind.SERVICE,
                    label=str(dep),
                    path=rel_path,
                )
            )
            graph.add_edge(
                GraphEdge(
                    source=svc_id, target=dep_id, kind=EdgeKind.DEPENDS_ON
                )
            )

        image = svc.get("image")
        if image:
            image_id = make_node_id(NodeKind.CONTAINER_IMAGE, str(image))
            graph.add_node(
                GraphNode(
                    id=image_id,
                    kind=NodeKind.CONTAINER_IMAGE,
                    label=str(image),
                )
            )
            graph.add_edge(
                GraphEdge(
                    source=svc_id, target=image_id, kind=EdgeKind.RUNS_IN
                )
            )


def parse_docker_related(
    root: Path, rel_path: str, graph: GraphDocument, text: str
) -> None:
    name = Path(rel_path).name.lower()
    if name == "dockerfile" or name.endswith(".dockerfile"):
        _parse_dockerfile(rel_path, graph, text)
    if name in {
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    }:
        _parse_compose(rel_path, graph, text)
