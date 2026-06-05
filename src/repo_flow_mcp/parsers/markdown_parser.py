"""Markdown dependency extractor backed by ``markdown-it-py``.

Emits ``FILE`` nodes (the doc itself + each linked local file) and ``MODULE``
nodes for "tool" mentions, with ``DEPENDS_ON`` edges between them. Improvements
over the previous regex-based implementation:

* Links inside fenced code blocks no longer count as dependencies.
* Code spans inside fenced blocks no longer count as tool mentions.
* Reference-style links and autolinks are handled by the markdown parser.
* Setext headings and indented blocks do not confuse the link scanner.

The parser never silently swallows a markdown_it failure: a fatal parse error
propagates and is recorded by the graph builder.
"""

from __future__ import annotations

import re
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

from repo_flow_mcp.models import (
    EdgeKind,
    GraphDocument,
    GraphEdge,
    GraphNode,
    NodeKind,
    make_node_id,
)
from repo_flow_mcp.parsers.tree_sitter_helpers import ParserError


# Identifier shape: starts with letter/underscore, followed by word chars,
# dots or hyphens. Matches the legacy ``MD_TOOL_LIST_RE`` capture group.
_IDENT_RE = re.compile(r"^[A-Za-z_][\w.-]+$")

# Cached so we don't re-create the parser per file.
_MD = MarkdownIt("commonmark")


def _is_local_link(link: str) -> bool:
    lowered = link.lower()
    return not (
        lowered.startswith("http://")
        or lowered.startswith("https://")
        or lowered.startswith("mailto:")
        or lowered.startswith("ftp://")
        or lowered.startswith("//")
        or lowered.startswith("#")
    )


def _iter_tokens(tokens: list[Token]) -> list[Token]:
    """Flatten the token stream including children of ``inline`` tokens."""

    out: list[Token] = []
    for tok in tokens:
        out.append(tok)
        if tok.children:
            out.extend(tok.children)
    return out


def _list_item_identifier(
    list_item_open_idx: int, tokens: list[Token]
) -> str | None:
    """Return the bare identifier inside a single-line list item, if any.

    A "list-item-as-tool" must contain exactly one ``paragraph_open`` with an
    ``inline`` whose entire text content matches ``_IDENT_RE``. Items that
    have nested formatting, multiple words or extra punctuation are skipped
    so we never mis-identify a sentence as a tool reference.
    """

    depth = 1
    i = list_item_open_idx + 1
    body_tokens: list[Token] = []
    while i < len(tokens) and depth > 0:
        tok = tokens[i]
        if tok.type == "list_item_open":
            depth += 1
        elif tok.type == "list_item_close":
            depth -= 1
            if depth == 0:
                break
        body_tokens.append(tok)
        i += 1

    # Must be exactly: paragraph_open, inline, paragraph_close
    if len(body_tokens) != 3:
        return None
    if (
        body_tokens[0].type != "paragraph_open"
        or body_tokens[1].type != "inline"
        or body_tokens[2].type != "paragraph_close"
    ):
        return None
    inline = body_tokens[1]
    children = inline.children or []
    # Only allow a single text child (rules out emphasis, code, nested links).
    if len(children) != 1 or children[0].type != "text":
        return None
    text = (children[0].content or "").strip()
    if not _IDENT_RE.match(text):
        return None
    return text


def parse_markdown_dependencies(
    rel_path: str, graph: GraphDocument, text: str
) -> None:
    file_id = make_node_id(NodeKind.FILE, rel_path)
    graph.add_node(
        GraphNode(
            id=file_id,
            kind=NodeKind.FILE,
            label=Path(rel_path).name,
            path=rel_path,
        )
    )

    if not text.strip():
        return

    try:
        tokens = _MD.parse(text)
    except Exception as exc:
        raise ParserError("markdown", rel_path, f"markdown_it parse failed: {exc}") from exc

    # 1) Collect links from every inline/link_open token. markdown-it never
    #    surfaces links inside ``fence`` / ``code_block`` tokens because those
    #    are raw — that's exactly the behaviour we want.
    for tok in _iter_tokens(tokens):
        if tok.type != "link_open":
            continue
        href = tok.attrGet("href") if hasattr(tok, "attrGet") else None
        if href is None:
            # Older markdown-it-py exposes attrs as a list of [k, v] pairs.
            for k, v in (tok.attrs or {}).items() if isinstance(tok.attrs, dict) else (tok.attrs or []):
                if k == "href":
                    href = v
                    break
        if not href:
            continue
        clean = str(href).strip()
        if not _is_local_link(clean):
            continue
        target = clean.split("#", maxsplit=1)[0].strip()
        if not target:
            continue
        target_path = Path(rel_path).parent.joinpath(target).as_posix()
        target_id = make_node_id(NodeKind.FILE, target_path)
        graph.add_node(
            GraphNode(
                id=target_id,
                kind=NodeKind.FILE,
                label=Path(target_path).name,
                path=target_path,
            )
        )
        graph.add_edge(
            GraphEdge(source=file_id, target=target_id, kind=EdgeKind.DEPENDS_ON)
        )

    # 2) Collect tool mentions: single-identifier list items + inline code
    #    spans (>= 3 chars, no slash, no space). Fenced code blocks are not
    #    visited because they emit ``fence`` tokens with no children.
    tool_names: set[str] = set()
    for idx, tok in enumerate(tokens):
        if tok.type == "list_item_open":
            ident = _list_item_identifier(idx, tokens)
            if ident is not None:
                tool_names.add(ident)

    for tok in _iter_tokens(tokens):
        if tok.type != "code_inline":
            continue
        raw_content = tok.content if isinstance(tok.content, str) else ""
        content = raw_content.strip()
        if not content or "/" in content or " " in content:
            continue
        if len(content) < 3:
            continue
        # Match the legacy filter: must start with letter/underscore and only
        # contain word chars, dots or hyphens.
        if not _IDENT_RE.match(content):
            continue
        tool_names.add(content)

    for tool in sorted(tool_names):
        tool_id = make_node_id(NodeKind.MODULE, "tool", tool)
        graph.add_node(
            GraphNode(id=tool_id, kind=NodeKind.MODULE, label=f"tool:{tool}")
        )
        graph.add_edge(
            GraphEdge(source=file_id, target=tool_id, kind=EdgeKind.DEPENDS_ON)
        )
