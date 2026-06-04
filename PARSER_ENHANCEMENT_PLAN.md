# Parser Enhancement Plan

## Objective

Increase parser coverage for code and script ecosystems while preserving deterministic, safe graph extraction.

## Enhancements

1. Code parser expansion
- Add lightweight import/dependency extraction for Go, Rust, Java, and C/C++.
- Keep Python AST parsing as primary high-fidelity path.
- Keep JS/TS import parsing and improve regex handling where needed.

2. Script and command parser expansion
- Improve invocation detection for direct script execution (`./script.sh`), sourced scripts (`source` / `.`), and package manager workflows (`npm`, `pnpm`, `yarn`).
- Improve artifact extraction in command lines for common build outputs.

3. CI parser coverage
- Add parser support for Jenkinsfile pipeline stages and shell steps.
- Add parser support for `.gitlab-ci.yml` jobs, dependencies, and script blocks.

4. Build system parser coverage
- Add parser support for `CMakeLists.txt` targets and dependencies.
- Add parser support for `BUILD`/`BUILD.bazel` dependencies.

5. Scanner integration
- Route newly supported files through corresponding parsers in `graph_builder`.
- Keep parsing best-effort and non-fatal on per-file failures.

6. Validation and tests
- Add fixtures for new language and script/build/CI inputs.
- Add tests to verify presence of expected node/edge kinds.

## Non-goals

- Full semantic call graphs for every language.
- Complete shell/control-flow execution simulation.
- Full parser-complete handling for every CI/build DSL edge case.

## Success Criteria

- Expanded parser coverage is reflected by tests.
- `make check` passes (ruff, mypy, pytest).
- Existing behavior remains stable while adding new extraction capability.
