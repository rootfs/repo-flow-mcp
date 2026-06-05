"""Adversarial tests for the tree-sitter / markdown-it backed parsers.

Each parser is exercised with both happy-path and pathological inputs to make
sure:

* The expected graph nodes and edges are emitted.
* Malformed inputs raise :class:`ParserError` (instead of silently producing
  an empty/partial graph).
* Edge cases that the legacy regex parsers got wrong (line continuations,
  fenced code blocks, multi-stage builds, pipelines, ``&&`` lists …) now
  produce correct edges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_flow_mcp.models import EdgeKind, GraphDocument, NodeKind
from repo_flow_mcp.parsers.ci_parser import parse_gitlab_ci
from repo_flow_mcp.parsers.docker_parser import (
    _parse_compose,
    _parse_dockerfile,
)
from repo_flow_mcp.parsers.github_actions_parser import parse_github_actions
from repo_flow_mcp.parsers.makefile_parser import parse_makefile
from repo_flow_mcp.parsers.markdown_parser import parse_markdown_dependencies
from repo_flow_mcp.parsers.shell_parser import (
    extract_command_edges,
    parse_shell_script,
)
from repo_flow_mcp.parsers.tree_sitter_helpers import ParserError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kinds(doc: GraphDocument) -> set[str]:
    return {n.kind.value for n in doc.nodes.values()}


def _has_edge(
    doc: GraphDocument,
    kind: EdgeKind,
    source_substr: str | None = None,
    target_substr: str | None = None,
) -> bool:
    for e in doc.edge_rows:
        if e.kind != kind:
            continue
        if source_substr is not None and source_substr not in e.source:
            continue
        if target_substr is not None and target_substr not in e.target:
            continue
        return True
    return False


def _edge_kinds(doc: GraphDocument) -> set[str]:
    return {e.kind.value for e in doc.edge_rows}


def _node_labels_of_kind(doc: GraphDocument, kind: NodeKind) -> set[str]:
    return {n.label for n in doc.nodes.values() if n.kind == kind}


# ---------------------------------------------------------------------------
# Shell parser
# ---------------------------------------------------------------------------


class TestShellParser:
    def test_pipeline_and_and_list_each_command_is_seen(self, tmp_path: Path) -> None:
        """``cd src && bash inside.sh`` must register an INVOKE for inside.sh.

        The legacy regex parser only looked at the first word of the line
        (``cd``) and missed ``bash inside.sh``.
        """

        g = GraphDocument()
        script = "cd src && bash inside.sh\npython publish.py | tee log.txt\n"
        parse_shell_script(tmp_path, "scripts/run.sh", g, script)
        assert _has_edge(
            g, EdgeKind.INVOKES, source_substr="scripts/run.sh", target_substr="inside.sh"
        )
        assert _has_edge(
            g, EdgeKind.INVOKES, source_substr="scripts/run.sh", target_substr="publish.py"
        )

    def test_env_prefix_then_command_extracts_both(self, tmp_path: Path) -> None:
        g = GraphDocument()
        parse_shell_script(
            tmp_path, "scripts/x.sh", g, "PYTHONPATH=src python scripts/foo.py\n"
        )
        labels = _node_labels_of_kind(g, NodeKind.ENV_VAR)
        assert "PYTHONPATH" in labels
        assert _has_edge(
            g, EdgeKind.INVOKES, source_substr="scripts/x.sh", target_substr="foo.py"
        )

    def test_bare_variable_assignment_keeps_artifact_extraction(
        self, tmp_path: Path
    ) -> None:
        g = GraphDocument()
        parse_shell_script(
            tmp_path, "scripts/y.sh", g, "MODEL_OUT=models/model.ckpt\n"
        )
        labels = _node_labels_of_kind(g, NodeKind.ARTIFACT)
        assert "models/model.ckpt" in labels
        assert _has_edge(g, EdgeKind.CONSUMES, target_substr="models/model.ckpt")

    def test_heredoc_body_is_not_parsed_as_commands(self, tmp_path: Path) -> None:
        """Heredoc bodies should not produce stray INVOKE edges."""

        g = GraphDocument()
        script = (
            "cat <<EOF > /tmp/note\n"
            "bash this_is_text.sh\n"
            "EOF\n"
            "bash real_script.sh\n"
        )
        parse_shell_script(tmp_path, "scripts/h.sh", g, script)
        scripts = _node_labels_of_kind(g, NodeKind.SCRIPT)
        assert "real_script.sh" in scripts
        # The text inside the heredoc must not become a SCRIPT node.
        assert "this_is_text.sh" not in scripts

    def test_for_loop_inner_command_is_parsed(self, tmp_path: Path) -> None:
        g = GraphDocument()
        script = 'for f in a b c; do bash "scripts/$f.sh"; done\n'
        parse_shell_script(tmp_path, "scripts/loop.sh", g, script)
        # The loop body's ``bash`` invocation should be seen, even though the
        # argument is a quoted expansion.
        assert _has_edge(g, EdgeKind.INVOKES, source_substr="scripts/loop.sh")

    def test_extract_command_edges_handles_multi_line(self, tmp_path: Path) -> None:
        g = GraphDocument()
        from repo_flow_mcp.models import GraphNode, make_node_id

        source_id = make_node_id(NodeKind.SCRIPT, "fake")
        g.add_node(GraphNode(id=source_id, kind=NodeKind.SCRIPT, label="fake"))
        # A multi-line ``run:`` block from a GHA workflow.
        block = "set -e\npython scripts/a.py --output dist/out.tar.gz\nmake test\n"
        extract_command_edges(g, source_id, block, "workflow")
        assert _has_edge(g, EdgeKind.INVOKES, target_substr="a.py")
        assert _has_edge(g, EdgeKind.INVOKES, target_substr="test")
        assert _has_edge(g, EdgeKind.PRODUCES, target_substr="dist/out.tar.gz")


# ---------------------------------------------------------------------------
# Makefile parser
# ---------------------------------------------------------------------------


class TestMakefileParser:
    def test_recipe_line_continuation_joins_to_single_command(
        self, tmp_path: Path
    ) -> None:
        g = GraphDocument()
        src = (
            "build:\n"
            "\tpython scripts/build.py \\\n"
            "\t\t--output dist/app.tar.gz\n"
        )
        parse_makefile(tmp_path, "Makefile", g, src)
        # The continuation must reassemble before the artifact regex runs;
        # otherwise we'd miss ``dist/app.tar.gz`` (it lives on the second
        # physical line).
        assert _has_edge(g, EdgeKind.PRODUCES, target_substr="dist/app.tar.gz")

    def test_multi_target_rule_emits_all_targets(self, tmp_path: Path) -> None:
        g = GraphDocument()
        parse_makefile(tmp_path, "Makefile", g, "foo bar: baz\n\techo hi\n")
        targets = _node_labels_of_kind(g, NodeKind.TARGET)
        assert {"foo", "bar", "baz"}.issubset(targets)

    def test_pattern_rules_are_captured(self, tmp_path: Path) -> None:
        g = GraphDocument()
        parse_makefile(tmp_path, "Makefile", g, "%.o: %.c\n\tgcc -c -o $@ $<\n")
        targets = _node_labels_of_kind(g, NodeKind.TARGET)
        assert "%.o" in targets

    def test_conditional_nested_rule_is_captured(self, tmp_path: Path) -> None:
        g = GraphDocument()
        src = (
            "ifeq ($(OS),Darwin)\n"
            "mac:\n"
            "\techo mac\n"
            "endif\n"
        )
        parse_makefile(tmp_path, "Makefile", g, src)
        assert "mac" in _node_labels_of_kind(g, NodeKind.TARGET)

    def test_recipe_silence_prefix_is_stripped(self, tmp_path: Path) -> None:
        """``@`` / ``-`` / ``+`` recipe prefixes must not break command parsing."""

        g = GraphDocument()
        src = "all:\n\t@bash scripts/x.sh\n\t-rm -rf build/\n"
        parse_makefile(tmp_path, "Makefile", g, src)
        # The `@`-silenced bash invocation must still register as INVOKES.
        scripts = _node_labels_of_kind(g, NodeKind.SCRIPT)
        assert "scripts/x.sh" in scripts


# ---------------------------------------------------------------------------
# Dockerfile parser
# ---------------------------------------------------------------------------


class TestDockerfileParser:
    def test_multistage_alias_registered_as_image(self) -> None:
        g = GraphDocument()
        _parse_dockerfile(
            "Dockerfile",
            g,
            "FROM python:3.11-slim AS base\nFROM base AS runtime\n",
        )
        images = _node_labels_of_kind(g, NodeKind.CONTAINER_IMAGE)
        # Both the literal base image and the stage aliases must be present.
        assert "python:3.11-slim" in images
        assert "base" in images
        assert "runtime" in images

    def test_copy_from_alias_emits_depends_on(self) -> None:
        g = GraphDocument()
        src = (
            "FROM base AS builder\n"
            "COPY src /app/src\n"
            "FROM base AS runtime\n"
            "COPY --from=builder /usr/local/lib /usr/local/lib\n"
        )
        _parse_dockerfile("Dockerfile", g, src)
        # The COPY --from=builder must NOT create an ARTIFACT node for
        # ``/usr/local/lib`` (those are paths inside the alias, not local).
        assert "/usr/local/lib" not in _node_labels_of_kind(g, NodeKind.ARTIFACT)
        # Instead, the COPY must depend on the alias image node.
        assert _has_edge(g, EdgeKind.DEPENDS_ON, target_substr="container_image:builder")

    def test_arg_in_from_is_preserved_verbatim(self) -> None:
        g = GraphDocument()
        _parse_dockerfile(
            "Dockerfile", g, "ARG PY=3.11\nFROM python:${PY}-slim\n"
        )
        images = _node_labels_of_kind(g, NodeKind.CONTAINER_IMAGE)
        assert "python:${PY}-slim" in images

    def test_invalid_compose_yaml_raises_parser_error(self) -> None:
        g = GraphDocument()
        with pytest.raises(ParserError):
            _parse_compose("compose.yml", g, "services: : :\n  - bad\n")

    def test_compose_top_level_must_be_mapping(self) -> None:
        g = GraphDocument()
        with pytest.raises(ParserError):
            _parse_compose("compose.yml", g, "- just\n- a list\n")


# ---------------------------------------------------------------------------
# Markdown parser
# ---------------------------------------------------------------------------


class TestMarkdownParser:
    def test_links_inside_fenced_code_blocks_are_ignored(self) -> None:
        g = GraphDocument()
        text = (
            "# Title\n"
            "[real](docs/real.md)\n"
            "\n"
            "```\n"
            "[fake](docs/fake.md)\n"
            "```\n"
        )
        parse_markdown_dependencies("doc.md", g, text)
        files = _node_labels_of_kind(g, NodeKind.FILE)
        assert "real.md" in files
        assert "fake.md" not in files

    def test_external_links_are_skipped(self) -> None:
        g = GraphDocument()
        parse_markdown_dependencies(
            "doc.md", g, "[ext](https://example.com)\n[local](./a.md)\n"
        )
        files = _node_labels_of_kind(g, NodeKind.FILE)
        assert "a.md" in files
        assert all("example.com" not in f for f in files)

    def test_tool_list_only_captures_single_identifier_items(self) -> None:
        g = GraphDocument()
        text = (
            "Tools:\n"
            "- grep\n"
            "- multi word item\n"
            "- a\n"  # too short — legacy regex required 2+ chars
            "- terminal\n"
        )
        parse_markdown_dependencies("doc.md", g, text)
        tools = {
            n.label.removeprefix("tool:")
            for n in g.nodes.values()
            if n.kind == NodeKind.MODULE and n.label.startswith("tool:")
        }
        assert "grep" in tools
        assert "terminal" in tools
        assert "multi" not in tools  # multi-word list items are not tools
        assert "a" not in tools

    def test_inline_code_tool_filtering(self) -> None:
        g = GraphDocument()
        text = (
            "Use `pytest` and `python_tool` but skip `with space` "
            "and `path/to/file`.\n"
        )
        parse_markdown_dependencies("doc.md", g, text)
        tools = {
            n.label.removeprefix("tool:")
            for n in g.nodes.values()
            if n.kind == NodeKind.MODULE and n.label.startswith("tool:")
        }
        # Match the legacy filter: >=3 chars, no slash, no space.
        assert "python_tool" in tools
        assert "pytest" in tools
        assert "with space" not in tools
        assert "path/to/file" not in tools


# ---------------------------------------------------------------------------
# GitHub Actions parser
# ---------------------------------------------------------------------------


class TestGitHubActionsParser:
    def test_malformed_yaml_raises_parser_error(self, tmp_path: Path) -> None:
        g = GraphDocument()
        with pytest.raises(ParserError):
            parse_github_actions(
                tmp_path, ".github/workflows/x.yml", g, "name: x\n  : : :\n"
            )

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        g = GraphDocument()
        with pytest.raises(ParserError):
            parse_github_actions(
                tmp_path, ".github/workflows/x.yml", g, "- foo\n- bar\n"
            )

    def test_needs_as_single_string_works(self, tmp_path: Path) -> None:
        g = GraphDocument()
        wf = (
            "name: t\n"
            "jobs:\n"
            "  build:\n"
            "    steps: []\n"
            "  publish:\n"
            "    needs: build\n"
            "    steps: []\n"
        )
        parse_github_actions(tmp_path, ".github/workflows/t.yml", g, wf)
        assert _has_edge(g, EdgeKind.DEPENDS_ON, source_substr="publish", target_substr="build")

    def test_multi_line_run_block_parses_each_command(
        self, tmp_path: Path
    ) -> None:
        g = GraphDocument()
        wf = (
            "name: t\n"
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      - name: many\n"
            "        run: |\n"
            "          python scripts/a.py\n"
            "          bash scripts/b.sh\n"
        )
        parse_github_actions(tmp_path, ".github/workflows/t.yml", g, wf)
        scripts = _node_labels_of_kind(g, NodeKind.SCRIPT)
        assert "scripts/a.py" in scripts
        assert "scripts/b.sh" in scripts


# ---------------------------------------------------------------------------
# GitLab CI parser
# ---------------------------------------------------------------------------


class TestGitLabCIParser:
    def test_malformed_yaml_raises(self) -> None:
        g = GraphDocument()
        with pytest.raises(ParserError):
            parse_gitlab_ci(".gitlab-ci.yml", g, "build:\n  script: ['a', 'b'\n")

    def test_script_as_string_is_treated_as_one_step(self) -> None:
        g = GraphDocument()
        parse_gitlab_ci(
            ".gitlab-ci.yml",
            g,
            "build:\n  script: bash scripts/a.sh\n",
        )
        assert _has_edge(g, EdgeKind.INVOKES, target_substr="scripts/a.sh")

    def test_reserved_top_level_keys_are_not_jobs(self) -> None:
        g = GraphDocument()
        src = (
            "stages:\n"
            "  - build\n"
            "variables:\n"
            "  FOO: bar\n"
            "build:\n"
            "  script: echo hi\n"
        )
        parse_gitlab_ci(".gitlab-ci.yml", g, src)
        job_labels = _node_labels_of_kind(g, NodeKind.WORKFLOW_JOB)
        assert "build" in job_labels
        assert "stages" not in job_labels
        assert "variables" not in job_labels
