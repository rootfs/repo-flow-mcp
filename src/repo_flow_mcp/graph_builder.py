from __future__ import annotations

from collections import deque
from pathlib import Path

from repo_flow_mcp.models import EdgeKind, GraphDocument
from repo_flow_mcp.symbol_index import SymbolIndex
from repo_flow_mcp.parsers import (
    parse_bazel_build,
    parse_cmake,
    parse_code_file,
    parse_docker_related,
    parse_gitlab_ci,
    parse_github_actions,
    parse_jenkinsfile,
    parse_markdown_dependencies,
    parse_makefile,
    parse_shell_script,
)
from repo_flow_mcp.settings import ScanSettings, load_settings


def _should_skip(path: Path, settings: ScanSettings) -> bool:
    for part in path.parts:
        if part in settings.ignore_dirs:
            return True
        if not settings.include_hidden and part.startswith(".") and part not in {".github"}:
            return True
    return False


def _safe_read(file_path: Path, max_bytes: int) -> str:
    data = file_path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"file too large: {file_path}")
    return data.decode("utf-8", errors="replace")


def build_graph(repo_path: str, include_hidden: bool | None = None) -> GraphDocument:
    root = Path(repo_path).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"invalid repository path: {repo_path}")

    settings = load_settings()
    if include_hidden is not None:
        settings = ScanSettings(
            max_files=settings.max_files,
            max_file_size_bytes=settings.max_file_size_bytes,
            include_hidden=include_hidden,
            ignore_dirs=settings.ignore_dirs,
            scan_exts=settings.scan_exts,
        )

    graph = GraphDocument()
    files_seen = 0

    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue

        rel_path = file_path.relative_to(root)
        if _should_skip(rel_path, settings):
            continue

        files_seen += 1
        if files_seen > settings.max_files:
            graph.warnings.append(f"scan limit reached at {settings.max_files} files")
            break

        rel = rel_path.as_posix()
        name = file_path.name.lower()
        suffix = file_path.suffix.lower()

        try:
            text = _safe_read(file_path, settings.max_file_size_bytes)
        except Exception as exc:  # broad to isolate parser failures to file scope
            graph.warnings.append(f"read error {rel}: {exc}")
            continue

        try:
            if suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}:
                parse_code_file(root, rel, graph, text)

            if suffix in {".sh", ".bash", ".zsh"}:
                parse_shell_script(root, rel, graph, text)

            if suffix in {".md", ".markdown"}:
                parse_markdown_dependencies(rel, graph, text)

            if name in {"makefile", "gnumakefile"}:
                parse_makefile(root, rel, graph, text)

            if rel.startswith(".github/workflows/") and suffix in {".yml", ".yaml"}:
                parse_github_actions(root, rel, graph, text)

            if name == ".gitlab-ci.yml":
                parse_gitlab_ci(rel, graph, text)

            if name.lower() == "jenkinsfile":
                parse_jenkinsfile(rel, graph, text)

            if name == "cmakelists.txt":
                parse_cmake(rel, graph, text)

            if name in {"build", "build.bazel"}:
                parse_bazel_build(rel, graph, text)

            if name == "dockerfile" or name in {
                "docker-compose.yml",
                "docker-compose.yaml",
                "compose.yml",
                "compose.yaml",
            }:
                parse_docker_related(root, rel, graph, text)
        except Exception as exc:  # broad to keep scan resilient
            graph.warnings.append(f"parse error {rel}: {exc}")

    return graph


def neighborhood(graph: GraphDocument, node_id: str, depth: int = 2) -> dict[str, object]:
    outgoing: dict[str, list[tuple[str, str]]] = {}
    incoming: dict[str, list[tuple[str, str]]] = {}
    for edge in graph.edge_rows:
        outgoing.setdefault(edge.source, []).append((edge.target, edge.kind.value))
        incoming.setdefault(edge.target, []).append((edge.source, edge.kind.value))

    def bfs(seed: str, adjacency: dict[str, list[tuple[str, str]]]) -> list[dict[str, str]]:
        seen = {seed}
        queue: deque[tuple[str, int]] = deque([(seed, 0)])
        rows: list[dict[str, str]] = []
        while queue:
            current, d = queue.popleft()
            if d >= depth:
                continue
            for nxt, kind in adjacency.get(current, []):
                rows.append({"from": current, "to": nxt, "kind": kind})
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, d + 1))
        return rows

    return {
        "upstream": bfs(node_id, incoming),
        "downstream": bfs(node_id, outgoing),
    }


def artifact_lineage(graph: GraphDocument, artifact_label: str) -> dict[str, object]:
    targets = [node for node in graph.nodes.values() if node.label == artifact_label and node.kind.value == "artifact"]
    if not targets:
        return {"artifact": artifact_label, "producers": [], "consumers": []}

    artifact_id = targets[0].id
    producers: list[str] = []
    consumers: list[str] = []
    for edge in graph.edge_rows:
        if edge.target == artifact_id and edge.kind == EdgeKind.PRODUCES:
            producers.append(edge.source)
        if edge.target == artifact_id and edge.kind == EdgeKind.CONSUMES:
            consumers.append(edge.source)
    return {
        "artifact": artifact_label,
        "artifact_id": artifact_id,
        "producers": sorted(set(producers)),
        "consumers": sorted(set(consumers)),
    }


def broken_stage_contracts(graph: GraphDocument) -> dict[str, object]:
    produced: dict[str, set[str]] = {}
    consumed: dict[str, set[str]] = {}
    for edge in graph.edge_rows:
        if edge.kind == EdgeKind.PRODUCES:
            produced.setdefault(edge.target, set()).add(edge.source)
        if edge.kind == EdgeKind.CONSUMES:
            consumed.setdefault(edge.target, set()).add(edge.source)

    missing_producer: list[str] = []
    missing_consumer: list[str] = []

    for artifact_id in consumed:
        if artifact_id not in produced:
            missing_producer.append(artifact_id)
    for artifact_id in produced:
        if artifact_id not in consumed:
            missing_consumer.append(artifact_id)

    return {
        "missing_producer": sorted(missing_producer),
        "missing_consumer": sorted(missing_consumer),
    }


def shortest_path(graph: GraphDocument, start: str, end: str) -> dict[str, object]:
    adjacency: dict[str, list[str]] = {}
    for edge in graph.edge_rows:
        adjacency.setdefault(edge.source, []).append(edge.target)

    queue: deque[str] = deque([start])
    parent: dict[str, str | None] = {start: None}

    while queue:
        cur = queue.popleft()
        if cur == end:
            break
        for nxt in adjacency.get(cur, []):
            if nxt not in parent:
                parent[nxt] = cur
                queue.append(nxt)

    if end not in parent:
        return {"found": False, "path": []}

    rev: list[str] = []
    walk: str | None = end
    while walk is not None:
        rev.append(walk)
        walk = parent[walk]
    return {"found": True, "path": list(reversed(rev))}


def repo_overview(graph: GraphDocument, top_k: int = 20) -> dict[str, object]:
    counts: dict[str, int] = {}
    for node in graph.nodes.values():
        counts[node.kind.value] = counts.get(node.kind.value, 0) + 1

    hot_files: dict[str, int] = {}
    for edge in graph.edge_rows:
        if edge.source.startswith("file:"):
            path = edge.source[5:]
            hot_files[path] = hot_files.get(path, 0) + 1

    ranked_files = sorted(hot_files.items(), key=lambda x: x[1], reverse=True)[:top_k]
    return {
        "node_kinds": counts,
        "top_files_by_edges": [{"path": p, "edge_count": c} for p, c in ranked_files],
        "warnings": graph.warnings,
        "stats": {
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edge_rows),
        },
    }


def repo_entrypoints(graph: GraphDocument, limit: int = 50) -> dict[str, object]:
    scripts: list[dict[str, str]] = []
    targets: list[dict[str, str]] = []
    workflows: list[dict[str, str]] = []

    for node in graph.nodes.values():
        row = {"id": node.id, "label": node.label, "path": node.path or ""}
        if node.kind.value == "script":
            scripts.append(row)
        elif node.kind.value == "target":
            targets.append(row)
        elif node.kind.value in {"workflow", "workflow_job"}:
            workflows.append(row)

    scripts.sort(key=lambda x: (x["path"], x["label"]))
    targets.sort(key=lambda x: (x["path"], x["label"]))
    workflows.sort(key=lambda x: (x["path"], x["label"]))
    return {
        "scripts": scripts[:limit],
        "targets": targets[:limit],
        "workflows": workflows[:limit],
    }


def function_to_script_chains(
    graph: GraphDocument,
    function_query: str,
    limit: int = 10,
    symbol_index: SymbolIndex | None = None,
) -> dict[str, object]:
    query = function_query.strip().lower()
    if not query:
        return {"query": function_query, "matches": []}

    node_by_id = graph.nodes
    defines_file_for_symbol: dict[str, str] = {}
    invocations: list[tuple[str, str, str]] = []
    # Per-node neighborhood for non-code matches (workflow jobs, Make
    # targets, scripts, modules). Built lazily only when we have at
    # least one such candidate so the common code-symbol query path
    # stays cheap.
    incoming_by_id: dict[str, list[tuple[str, str, str]]] | None = None
    outgoing_by_id: dict[str, list[tuple[str, str, str]]] | None = None

    for edge in graph.edge_rows:
        if edge.kind.value == "defines" and edge.source.startswith("file:") and edge.target.startswith("code_symbol:"):
            defines_file_for_symbol[edge.target] = edge.source[5:]
        if edge.kind.value == "invokes" and edge.target.startswith("script:"):
            invocations.append((edge.source, edge.target, edge.metadata.get("command", "")))

    # Pick candidates using the FTS5 index when available; fall back
    # to a full scan if the index is missing or returns nothing. The
    # index now holds CODE_SYMBOL plus runner kinds (workflow / job /
    # step / target / script / module), so a single search returns
    # both the symbol-chain candidates and the CI/build-runner
    # candidates we'll process below.
    candidate_pairs: list[tuple[str, str]] = []
    if symbol_index is not None:
        candidate_pairs = symbol_index.search_with_kinds(
            function_query, limit=max(limit * 4, 64)
        )
    if not candidate_pairs:
        # Legacy substring fallback over CODE_SYMBOL nodes only; this
        # preserves prior behaviour when the index is empty.
        candidate_pairs = [
            (sid, "code_symbol")
            for sid in defines_file_for_symbol
            if (
                query in (node_by_id[sid].label.lower() if sid in node_by_id else "")
                or query in sid.lower()
            )
        ]

    code_candidate_ids = [
        sid for sid, kind in candidate_pairs
        if kind == "code_symbol" and sid in defines_file_for_symbol
    ]
    runner_candidate_ids = [
        sid for sid, kind in candidate_pairs
        if kind != "code_symbol" and sid in node_by_id
    ]

    results: list[dict[str, object]] = []

    # --- Code-symbol -> script chain (legacy shape, kept verbatim) ---
    for symbol_id in code_candidate_ids:
        file_path = defines_file_for_symbol.get(symbol_id)
        if file_path is None:
            continue
        symbol = node_by_id.get(symbol_id)
        if not symbol:
            continue

        basename = Path(file_path).name
        for src_id, target_script_id, command in invocations:
            target_node = node_by_id.get(target_script_id)
            if not target_node:
                continue
            target_label = target_node.label
            target_path = target_node.path or ""
            src_node = node_by_id.get(src_id)
            src_label = src_node.label if src_node is not None else src_id

            is_match = (
                target_label == basename
                or target_path.endswith(file_path)
                or target_path.endswith(basename)
                or file_path.endswith(target_label)
            )
            if not is_match:
                continue

            results.append(
                {
                    "match_kind": "code_symbol_chain",
                    "node": {
                        "id": symbol_id,
                        "label": symbol.label,
                        "kind": symbol.kind.value,
                        "path": file_path,
                    },
                    "function": {
                        "id": symbol_id,
                        "label": symbol.label,
                        "file": file_path,
                    },
                    "script_source": {
                        "id": src_id,
                        "label": src_label,
                    },
                    "invoked_script": {
                        "id": target_script_id,
                        "label": target_label,
                        "path": target_path,
                    },
                    "bridge": {
                        "file_node": f"file:{file_path}",
                        "type": "script-label-to-file-basename",
                    },
                    "command": command,
                }
            )
            if len(results) >= limit:
                return {"query": function_query, "matches": results}

    # --- Runner matches (workflow / target / script / module) -------
    # Same `matches[]` array, different shape: each entry exposes the
    # matched node plus its 1-hop neighborhood, so the agent can
    # answer "what runs this Make target?" or "what does this job
    # invoke?" without a second tool call.
    if runner_candidate_ids and (incoming_by_id is None or outgoing_by_id is None):
        incoming_by_id = {}
        outgoing_by_id = {}
        for edge in graph.edge_rows:
            outgoing_by_id.setdefault(edge.source, []).append(
                (edge.target, edge.kind.value, edge.metadata.get("command", ""))
            )
            incoming_by_id.setdefault(edge.target, []).append(
                (edge.source, edge.kind.value, edge.metadata.get("command", ""))
            )

    for runner_id in runner_candidate_ids:
        if len(results) >= limit:
            break
        runner = node_by_id.get(runner_id)
        if runner is None:
            continue

        def _resolve(other_id: str) -> dict[str, str]:
            other = node_by_id.get(other_id)
            if other is None:
                return {"id": other_id, "label": other_id, "kind": "", "path": ""}
            return {
                "id": other.id,
                "label": other.label,
                "kind": other.kind.value,
                "path": other.path or "",
            }

        incoming_rows: list[dict[str, object]] = []
        for src, edge_kind, cmd in (incoming_by_id or {}).get(runner_id, [])[:20]:
            row: dict[str, object] = {"edge_kind": edge_kind, **_resolve(src)}
            if cmd:
                row["command"] = cmd
            incoming_rows.append(row)

        outgoing_rows: list[dict[str, object]] = []
        for dst, edge_kind, cmd in (outgoing_by_id or {}).get(runner_id, [])[:20]:
            row = {"edge_kind": edge_kind, **_resolve(dst)}
            if cmd:
                row["command"] = cmd
            outgoing_rows.append(row)

        # match_kind classifies the runner so the agent can route
        # straight to the right block (CI vs build-target vs module).
        if runner.kind.value in {"workflow", "workflow_job", "workflow_step"}:
            match_kind = "ci_runner"
        elif runner.kind.value == "target":
            match_kind = "build_target"
        elif runner.kind.value == "script":
            match_kind = "script"
        else:
            match_kind = "module"

        results.append(
            {
                "match_kind": match_kind,
                "node": {
                    "id": runner.id,
                    "label": runner.label,
                    "kind": runner.kind.value,
                    "path": runner.path or "",
                },
                "incoming": incoming_rows,
                "outgoing": outgoing_rows,
            }
        )

    return {"query": function_query, "matches": results}
