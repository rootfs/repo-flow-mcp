"""Tests for the tree-sitter backed code parser.

These exercise the property the legacy regex parsers were missing for
Go/Rust/Java/C/C++: CODE_SYMBOL definitions and CALLS edges originating from
inside a function body.
"""

from __future__ import annotations

from repo_flow_mcp.models import GraphDocument
from repo_flow_mcp.parsers.tree_sitter_parser import parse_with_tree_sitter


def _payload(rel_path: str, text: str) -> dict:
    graph = GraphDocument()
    ok = parse_with_tree_sitter(rel_path, graph, text)
    assert ok, f"tree-sitter parse failed for {rel_path}"
    return graph.to_dict()


def _symbols(payload: dict) -> set[str]:
    return {n["label"] for n in payload["nodes"] if n["kind"] == "code_symbol"}


def _calls(payload: dict) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in payload["edges"] if e["kind"] == "calls"}


def _modules(payload: dict) -> set[str]:
    return {n["label"] for n in payload["nodes"] if n["kind"] == "module"}


def test_go_function_call_and_method_extraction() -> None:
    src = """package foo
import (
  "context"
  "fmt"
)

func DoThing(ctx context.Context) error {
    Helper()
    fmt.Println("hi")
    return nil
}

type Bar struct{}

func (b *Bar) Method() {
    DoThing(nil)
}
"""
    payload = _payload("svc/foo.go", src)
    symbols = _symbols(payload)
    assert "DoThing" in symbols
    assert "Method" in symbols

    callers = {
        e["source"].rsplit(":", 1)[-1]
        for e in payload["edges"]
        if e["kind"] == "calls" and e["target"].endswith(":Helper")
    }
    assert "DoThing" in callers
    callers = {
        e["source"].rsplit(":", 1)[-1]
        for e in payload["edges"]
        if e["kind"] == "calls" and e["target"].endswith(":DoThing")
    }
    assert "Method" in callers

    assert {"context", "fmt"}.issubset(_modules(payload))


def test_rust_function_and_macro_calls() -> None:
    src = """use std::collections::HashMap;

fn driver() {
    helper();
    println!("hi");
}

fn helper() {}
"""
    payload = _payload("src/lib.rs", src)
    symbols = _symbols(payload)
    assert "driver" in symbols
    assert "helper" in symbols
    callees = {e["target"].rsplit(":", 1)[-1] for e in payload["edges"] if e["kind"] == "calls"}
    assert "helper" in callees
    assert "println!" in callees
    assert "std::collections::HashMap" in _modules(payload)


def test_java_method_invocation() -> None:
    src = """package p;
import java.util.List;

public class K {
    public void run() {
        helper();
        other.thing();
    }

    private void helper() {}
}
"""
    payload = _payload("src/K.java", src)
    symbols = _symbols(payload)
    assert "run" in symbols
    assert "helper" in symbols
    assert "K" in symbols
    callees = {e["target"].rsplit(":", 1)[-1] for e in payload["edges"] if e["kind"] == "calls"}
    assert "helper" in callees
    assert "thing" in callees
    assert "java.util.List" in _modules(payload)


def test_cpp_function_call_extraction() -> None:
    src = """#include <vector>
#include "mylib/core.h"

int helper() { return 1; }

int driver() {
    return helper();
}
"""
    payload = _payload("src/main.cpp", src)
    symbols = _symbols(payload)
    assert "helper" in symbols
    assert "driver" in symbols
    callees = {e["target"].rsplit(":", 1)[-1] for e in payload["edges"] if e["kind"] == "calls"}
    assert "helper" in callees
    assert {"vector", "mylib/core.h"}.issubset(_modules(payload))


def test_python_call_attribution_via_tree_sitter() -> None:
    src = """import os
from a.b import c

def fn(x):
    return c(x)

class K:
    def m(self):
        fn(1)
"""
    payload = _payload("pkg/mod.py", src)
    symbols = _symbols(payload)
    assert {"fn", "K", "m"}.issubset(symbols)
    callees = {e["target"].rsplit(":", 1)[-1] for e in payload["edges"] if e["kind"] == "calls"}
    assert "fn" in callees
    assert "c" in callees
    assert {"os", "a.b"}.issubset(_modules(payload))


def test_typescript_require_treated_as_import() -> None:
    src = """import { join } from "path";
const helper = require("fs");

export function bootstrap(): void {
    helper.readFileSync("x");
    join("a", "b");
}
"""
    payload = _payload("src/web.ts", src)
    modules = _modules(payload)
    assert "path" in modules
    assert "fs" in modules
    symbols = _symbols(payload)
    assert "bootstrap" in symbols
