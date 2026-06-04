from __future__ import annotations

import argparse
import json

from repo_flow_mcp.graph_builder import build_graph
from repo_flow_mcp.server import run_server


def _cmd_serve(_: argparse.Namespace) -> int:
    run_server()
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    graph = build_graph(args.path, include_hidden=args.include_hidden)
    payload = graph.to_dict()
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    else:
        print(json.dumps(payload, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="repo-flow-mcp",
        description="Unified code/script/dependency graph MCP server",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run stdio MCP server")
    p_serve.set_defaults(func=_cmd_serve)

    p_export = sub.add_parser("export", help="Export graph JSON locally")
    p_export.add_argument("path", help="Repository root path")
    p_export.add_argument("--output", help="Write JSON output to file")
    p_export.add_argument("--include-hidden", action="store_true", help="Include hidden paths")
    p_export.set_defaults(func=_cmd_export)

    args = parser.parse_args()
    func = getattr(args, "func", None)
    if not callable(func):
        return 2
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
