"""Shell script parser backed by ``tree-sitter-bash``.

Public surface (preserved for back-compat with the previous regex parser):

* :func:`parse_shell_script` — parse a whole shell script and emit ``SCRIPT``
  / ``ENV_VAR`` / ``TARGET`` / ``ARTIFACT`` / ``SERVICE`` nodes plus
  ``SETS_ENV`` / ``INVOKES`` / ``PRODUCES`` / ``CONSUMES`` edges.
* :func:`extract_command_edges` — parse a single inline command string
  (e.g. one ``RUN`` line in a workflow ``run:`` block or one Makefile recipe
  line). Used by the Makefile, GitHub Actions and GitLab CI parsers.

Both functions raise :class:`~repo_flow_mcp.parsers.tree_sitter_helpers.ParserError`
when tree-sitter is unavailable or the input cannot be parsed within the
error budget. The graph builder catches the error and records a warning so
one bad shell snippet does not contaminate the rest of the scan.
"""

from __future__ import annotations

import re
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
from repo_flow_mcp.parsers.tree_sitter_helpers import (
    ParserError,
    get_parser_or_raise,
    iter_children,
    node_text,
    parse_or_raise,
    walk,
)

Node = Any

ARTIFACT_HINT_RE = re.compile(
    r"([\w./-]+\.(?:pt|pth|ckpt|bin|jsonl?|csv|parquet|tar(?:\.gz)?|zip))"
)

_SCRIPT_HEADS = {"bash", "sh", "zsh"}
_SOURCE_HEADS = {"source", "."}
_PY_HEADS = {"python", "python3"}
_JS_RUNNERS = {"npm", "pnpm", "yarn"}
_CONTAINER_HEADS = {"docker", "docker-compose", "compose", "podman"}

# Match the previous parser's environment-name shape: uppercase + digits + _.
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _is_command_like(node: Node) -> bool:
    return bool(node.kind() == "command")


def _decode_word(node: Node, src: bytes) -> str:
    text = node_text(node, src).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text


def _arg_words(command_node: Node, src: bytes) -> list[str]:
    """Positional arguments of a ``command`` node, in source order.

    The bash grammar lays out a command as::

        command
          variable_assignment*      (env prefixes)
          command_name              (the head; wraps a word)
          word|string|raw_string|concatenation|... (arguments)

    Redirections, heredoc bodies and similar non-argument children are
    skipped: they're not semantic arguments to the command.
    """

    args: list[str] = []
    for child in iter_children(command_node):
        kind = child.kind()
        if kind in {"variable_assignment", "command_name"}:
            continue
        if kind in {
            "word",
            "string",
            "raw_string",
            "ansi_c_string",
            "concatenation",
            "simple_expansion",
            "expansion",
            "command_substitution",
            "process_substitution",
            "number",
        }:
            args.append(_decode_word(child, src))
    return args


def _head_of(command_node: Node, src: bytes) -> str | None:
    for child in iter_children(command_node):
        if child.kind() == "command_name":
            inner = child.child(0) if child.child_count() > 0 else None
            if inner is None:
                return None
            return _decode_word(inner, src) or None
    return None


def _record_env_assignment(
    graph: GraphDocument, source_node_id: str, name: str
) -> None:
    if not name or not _ENV_NAME_RE.match(name):
        return
    env_id = make_node_id(NodeKind.ENV_VAR, name)
    graph.add_node(GraphNode(id=env_id, kind=NodeKind.ENV_VAR, label=name))
    graph.add_edge(
        GraphEdge(source=source_node_id, target=env_id, kind=EdgeKind.SETS_ENV)
    )


def _record_artifacts(
    graph: GraphDocument, source_node_id: str, cmd_text: str
) -> None:
    """Match the legacy artifact-hint heuristic on the full command text."""

    if not cmd_text:
        return
    for match in ARTIFACT_HINT_RE.findall(cmd_text):
        artifact_id = make_node_id(NodeKind.ARTIFACT, match)
        graph.add_node(
            GraphNode(id=artifact_id, kind=NodeKind.ARTIFACT, label=match)
        )
        if any(
            flag in cmd_text
            for flag in ("--output", "-o", "save", "write", "export")
        ):
            graph.add_edge(
                GraphEdge(
                    source=source_node_id,
                    target=artifact_id,
                    kind=EdgeKind.PRODUCES,
                )
            )
        else:
            graph.add_edge(
                GraphEdge(
                    source=source_node_id,
                    target=artifact_id,
                    kind=EdgeKind.CONSUMES,
                )
            )


def _emit_invoke(
    graph: GraphDocument,
    source_node_id: str,
    kind: NodeKind,
    raw_target: str,
    rel_path: str,
    cmd_text: str,
) -> None:
    if not raw_target:
        return
    target = (
        str(Path(rel_path).parent.joinpath(raw_target).as_posix())
        if "/" in raw_target
        else raw_target
    )
    target_id = make_node_id(kind, target)
    graph.add_node(
        GraphNode(
            id=target_id,
            kind=kind,
            label=raw_target,
            path=target if "/" in target else None,
        )
    )
    graph.add_edge(
        GraphEdge(
            source=source_node_id,
            target=target_id,
            kind=EdgeKind.INVOKES,
            metadata={"command": cmd_text},
        )
    )


def _extract_from_command_node(
    graph: GraphDocument,
    source_node_id: str,
    command_node: Node,
    src: bytes,
    rel_path: str,
) -> None:
    cmd_text = node_text(command_node, src).strip()

    # Env-var prefixes carried by the command (e.g. ``FOO=bar python ...``).
    for child in iter_children(command_node):
        if child.kind() == "variable_assignment":
            name_node = (
                child.child_by_field_name("name")
                if hasattr(child, "child_by_field_name")
                else None
            )
            name = (
                node_text(name_node, src).strip()
                if name_node is not None
                else ""
            )
            _record_env_assignment(graph, source_node_id, name)

    head = _head_of(command_node, src)
    if not head:
        _record_artifacts(graph, source_node_id, cmd_text)
        return

    args = _arg_words(command_node, src)

    if head in _SCRIPT_HEADS and args:
        _emit_invoke(
            graph, source_node_id, NodeKind.SCRIPT, args[0], rel_path, cmd_text
        )
    elif head in _SOURCE_HEADS and args:
        _emit_invoke(
            graph, source_node_id, NodeKind.SCRIPT, args[0], rel_path, cmd_text
        )
    elif head.startswith("./"):
        _emit_invoke(
            graph, source_node_id, NodeKind.SCRIPT, head, rel_path, cmd_text
        )
    elif head == "make" and args:
        _emit_invoke(
            graph, source_node_id, NodeKind.TARGET, args[0], rel_path, cmd_text
        )
    elif head in _PY_HEADS and args:
        _emit_invoke(
            graph, source_node_id, NodeKind.SCRIPT, args[0], rel_path, cmd_text
        )
    elif head in _JS_RUNNERS and len(args) >= 2 and args[0] in {"run", "exec"}:
        _emit_invoke(
            graph,
            source_node_id,
            NodeKind.TARGET,
            f"pkg:{args[1]}",
            rel_path,
            cmd_text,
        )
    elif head in _CONTAINER_HEADS:
        _emit_invoke(
            graph,
            source_node_id,
            NodeKind.SERVICE,
            "container-runtime",
            rel_path,
            cmd_text,
        )

    _record_artifacts(graph, source_node_id, cmd_text)


def _walk_commands(root_node: Node) -> list[Node]:
    return [n for n in walk(root_node) if _is_command_like(n)]


def _record_top_level_assignments(
    graph: GraphDocument,
    source_node_id: str,
    root_node: Node,
    src: bytes,
) -> None:
    """SETS_ENV edges for bare ``VAR=value`` statements (not command prefixes).

    The grammar models a bare assignment as a top-level
    ``variable_assignment`` statement; an in-command assignment lives as a
    child of a ``command`` node. We dedupe the latter so we don't double-emit.
    """

    inside_command: set[int] = set()
    for node in walk(root_node):
        if node.kind() == "command":
            for child in iter_children(node):
                if child.kind() == "variable_assignment":
                    inside_command.add(child.start_byte())

    for node in walk(root_node):
        if node.kind() != "variable_assignment":
            continue
        if node.start_byte() in inside_command:
            continue
        name_node = (
            node.child_by_field_name("name")
            if hasattr(node, "child_by_field_name")
            else None
        )
        name = (
            node_text(name_node, src).strip() if name_node is not None else ""
        )
        _record_env_assignment(graph, source_node_id, name)
        # The legacy parser also ran the artifact-hint regex against bare
        # ``VAR=path/to/file.ckpt`` lines; preserve that here.
        _record_artifacts(graph, source_node_id, node_text(node, src))


def extract_command_edges(
    graph: GraphDocument, source_node_id: str, cmd: str, rel_path: str
) -> None:
    """Parse a single inline command string and emit graph edges.

    Kept for back-compat with the older line-by-line callers (Makefile, GHA,
    GitLab CI). Internally we re-parse the snippet with tree-sitter-bash so
    pipelines, ``&&`` lists, command substitution etc. are handled correctly.
    """

    if not cmd or not cmd.strip():
        return
    parser = get_parser_or_raise("bash", parser_name="shell", path=rel_path)
    tree = parse_or_raise(parser, cmd, parser_name="shell", path=rel_path)
    src = cmd.encode("utf-8", errors="replace")
    for command_node in _walk_commands(tree.root_node()):
        _extract_from_command_node(
            graph, source_node_id, command_node, src, rel_path
        )
    _record_top_level_assignments(graph, source_node_id, tree.root_node(), src)


def parse_shell_script(
    root: Path, rel_path: str, graph: GraphDocument, text: str
) -> None:
    """Parse a whole shell script and emit a ``SCRIPT`` node plus all
    derived edges.
    """

    script_id = make_node_id(NodeKind.SCRIPT, rel_path)
    graph.add_node(
        GraphNode(
            id=script_id,
            kind=NodeKind.SCRIPT,
            label=Path(rel_path).name,
            path=rel_path,
        )
    )

    if not text.strip():
        return

    parser = get_parser_or_raise("bash", parser_name="shell", path=rel_path)
    tree = parse_or_raise(parser, text, parser_name="shell", path=rel_path)
    src = text.encode("utf-8", errors="replace")

    for command_node in _walk_commands(tree.root_node()):
        _extract_from_command_node(graph, script_id, command_node, src, rel_path)

    _record_top_level_assignments(graph, script_id, tree.root_node(), src)


__all__ = ["extract_command_edges", "parse_shell_script", "ParserError"]
