from __future__ import annotations

import json
from pathlib import Path

from repo_flow_mcp.cli import main


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "repo_sample"


def test_cli_export(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "graph.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "repo-flow-mcp",
            "export",
            str(FIXTURE_ROOT),
            "--output",
            str(out),
        ],
    )
    rc = main()
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "nodes" in payload
    assert "edges" in payload
