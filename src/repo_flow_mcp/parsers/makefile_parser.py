"""Makefile parser backed by ``tree-sitter-make``.

Emits ``TARGET`` nodes plus ``DEPENDS_ON`` edges between targets and feeds
each recipe line through :func:`extract_command_edges` (which itself uses
tree-sitter-bash). Handles features the old line-based regex missed:

* Multi-target rules (``foo bar: baz``)
* Pattern rules (``%.o: %.c``)
* Line continuations inside a recipe (``\\`` + newline + tab)
* Conditional blocks (``ifeq`` / ``endif``) that contain nested rules

Raises :class:`~repo_flow_mcp.parsers.tree_sitter_helpers.ParserError` if
tree-sitter cannot produce a trustworthy parse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from repo_flow_mcp.models import (
    EdgeKind,
    GraphDocument,
    GraphEdge,
    GraphNode,
    NodeKind,
    make_node_id,
)
from repo_flow_mcp.parsers.shell_parser import extract_command_edges
from repo_flow_mcp.parsers.tree_sitter_helpers import (
    get_parser_or_raise,
    iter_children,
    node_text,
    parse_or_raise,
    walk,
)

Node = Any


# Recipe-line prefix chars that suppress echo / silence errors / force exec.
_RECIPE_PREFIXES = {"@", "-", "+"}


def _target_words(targets_node: Node, src: bytes) -> list[str]:
    out: list[str] = []
    for child in iter_children(targets_node):
        if child.kind() == "word":
            out.append(node_text(child, src).strip())
    return out


def _prereq_words(prereqs_node: Node | None, src: bytes) -> list[str]:
    """Words inside a ``prerequisites`` node.

    ``variable_reference`` children (``$(SRC)``) are emitted as their raw
    source text so they at least show up as a dependency edge — the legacy
    parser's ``str.split()`` would have done the same thing.
    """

    if prereqs_node is None:
        return []
    out: list[str] = []
    for child in iter_children(prereqs_node):
        kind = child.kind()
        if kind in {"word", "variable_reference"}:
            out.append(node_text(child, src).strip())
    return out


def _recipe_command(recipe_line: Node, src: bytes) -> str:
    """Rebuild a ``recipe_line`` as the equivalent single command string.

    tree-sitter-make splits a logical recipe line across multiple
    ``shell_text`` siblings when the user used ``\\``-newline continuations.
    We rebuild the line by stripping the leading prefix marker(s) and
    collapsing newline + leading-whitespace runs into a single space.
    """

    raw = node_text(recipe_line, src).rstrip("\n")
    i = 0
    while i < len(raw) and raw[i] in _RECIPE_PREFIXES:
        i += 1
    body = raw[i:]

    out_parts: list[str] = []
    pending_continuation = False
    for line in body.split("\n"):
        if pending_continuation:
            stripped = line.lstrip("\t ")
            if out_parts:
                out_parts[-1] = out_parts[-1] + " " + stripped
            else:
                out_parts.append(stripped)
            pending_continuation = False
        else:
            out_parts.append(line)
        if out_parts and out_parts[-1].endswith("\\"):
            out_parts[-1] = out_parts[-1][:-1].rstrip()
            pending_continuation = True
    return " ".join(p for p in out_parts if p).strip()


def _iter_rules(root_node: Node) -> list[Node]:
    return [n for n in walk(root_node) if n.kind() == "rule"]


def parse_makefile(
    root: Path, rel_path: str, graph: GraphDocument, text: str
) -> None:
    if not text.strip():
        return

    parser = get_parser_or_raise("make", parser_name="makefile", path=rel_path)
    tree = parse_or_raise(parser, text, parser_name="makefile", path=rel_path)
    src = text.encode("utf-8", errors="replace")

    for rule in _iter_rules(tree.root_node()):
        targets_node = None
        prereqs_node = None
        recipe_node = None
        for child in iter_children(rule):
            kind = child.kind()
            if kind == "targets" and targets_node is None:
                targets_node = child
            elif kind == "prerequisites":
                prereqs_node = child
            elif kind == "recipe":
                recipe_node = child

        if targets_node is None:
            continue

        target_names = _target_words(targets_node, src)
        prereq_names = _prereq_words(prereqs_node, src)

        target_ids: list[str] = []
        for target_name in target_names:
            if not target_name:
                continue
            target_id = make_node_id(NodeKind.TARGET, rel_path, target_name)
            graph.add_node(
                GraphNode(
                    id=target_id,
                    kind=NodeKind.TARGET,
                    label=target_name,
                    path=rel_path,
                )
            )
            target_ids.append(target_id)

            for prereq_name in prereq_names:
                if not prereq_name:
                    continue
                dep_id = make_node_id(NodeKind.TARGET, rel_path, prereq_name)
                graph.add_node(
                    GraphNode(
                        id=dep_id,
                        kind=NodeKind.TARGET,
                        label=prereq_name,
                        path=rel_path,
                    )
                )
                graph.add_edge(
                    GraphEdge(
                        source=target_id,
                        target=dep_id,
                        kind=EdgeKind.DEPENDS_ON,
                    )
                )

        if recipe_node is None or not target_ids:
            continue

        for recipe_line in iter_children(recipe_node):
            if recipe_line.kind() != "recipe_line":
                continue
            cmd = _recipe_command(recipe_line, src)
            if not cmd:
                continue
            for target_id in target_ids:
                extract_command_edges(graph, target_id, cmd, rel_path)
