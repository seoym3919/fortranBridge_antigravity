---
name: translate-fortran
description: Modernizes Fortran codebases (FORTRAN 66–2003) into C++17/20 (CMake) or scientific Python (NumPy) with identical equations and algorithm.
argument-hint: "[directory|file] [--target cpp|python|both]"
---

# /translate-fortran

Translates entire Fortran codebase directories, repositories, or individual source files into modern, high-performance C++ (C++17/20) or scientific Python (NumPy) with identical equations and algorithm, automated 0-based indexing conversion, topological multi-file linking, COMMON block encapsulation, and numerical equivalence verification.
Bundled sub-skills live at `../<name>/SKILL.md` (`fortran-to-cpp`, `fortran-to-python`, `audit-indexing`, `decompile-format`, `encapsulate-common`, `infer-intents`, `link-project`, `restructure-goto`, `verify-translation`).

## Usage

Modernize an entire Fortran codebase directory to modern C++:
```text
/translate-fortran [directory] --target cpp
```

Modernize an entire Fortran codebase directory to Python:
```text
/translate-fortran [directory] --target python --out-dir modernized_py
```

Modernize a single Fortran file:
```text
/translate-fortran orbit.f --target both --report audit.md
```

## Codebase Workflow

**Follow `rules/directives.md` item 0, in order, for every codebase conversion:**

1. Plan: run `python3 ~/.gemini/config/plugins/fortranbridge/scripts/conversion_plan.py <directory> --target cpp|python`. It writes `FORTRAN_FUNCTION_LIST.md` and `PLAN.md`. Fill in the golden data table in `PLAN.md`, show it to the user, and stop. Conversion begins only when the user types `APPROVE PLAN <code>`.
2. Record golden data with `regression_test.py record`, then convert one function or module at a time in the plan's order. After each function run `unit_compare.py` and `regression_test.py run --after NAME`.
3. Finish with `regression_test.py run --final`: more than 10 cases, byte identical, or within machine precision only where the plan marks a Fortran-specific library.

`fortran_bridge_cli.py` needs `--plan .verification/conversion_plan.json` and an approved `PLAN.md` to convert a directory.

1. **Directory-Wide Ingestion & AST Lexing**:
   Recursively discovers all `.f`, `.f77`, `.for`, `.f90`, `.f95`, `.f03`, `.f08` files across the codebase directory.
2. **Multi-File Topological Dependency DAG**:
   Builds the Directed Acyclic Graph of `MODULE`, `USE`, `COMMON`, and `CALL` dependencies via `python3 ~/.gemini/config/plugins/fortranbridge/scripts/project_linker.py`, ensuring base definitions and modules are translated before dependent numerical solvers.
3. **COMMON Block Encapsulation**:
   Converts shared memory blocks into typed C++ structs (`include/common_blocks.hpp`) or Python `@dataclass` state objects (`common_state.py`).
4. **Hybrid Static + Semantic LLM Code Synthesis**:
   - Run `python3 ~/.gemini/config/plugins/fortranbridge/scripts/fortran_bridge_cli.py` to generate the topological project scaffold (`CMakeLists.txt` or `__init__.py`), `COMMON`/`EQUIVALENCE` structs, and inferred `INTENT(IN/OUT)` signatures.
   - Refine complex nested mathematical kernels semantically (via the `fortran-translator` subagent on `Model: "pro"`) rather than relying solely on regex line-by-line transpilation:
     - C++: Start from the generated `std::vector` scaffold; `std::span` views are fine, but keep the Fortran loop order.
     - Python: Keep every `DO` loop as a loop, and map only Fortran whole-array statements to NumPy array expressions.
     - Both: Keep the equations and the algorithm identical to the Fortran: same statements in the same order, same loop nesting, order, bounds and steps, same operator order and parentheses, same precision. Do not interchange or merge loops, turn a `DO` loop into a whole-array expression, reorder or reassociate arithmetic, substitute a library routine for hand-written numerics, or add parallelism or `fastmath`.
5. **Codebase Translation Report & Closed-Loop Verification**:
   Emit `CODEBASE_TRANSLATION_REPORT.md`, audit 0-based offsets via `indexing-auditor`, and compare outputs with `python3 ~/.gemini/config/plugins/fortranbridge/scripts/translation_verifier.py` (`rules/directives.md` item 3).
   Then run `python3 ~/.gemini/config/plugins/fortranbridge/scripts/port_completeness.py --original ORIGINAL --translated TRANSLATED --target cpp|python --unit-tests TESTS --evidence-log .verification/log.jsonl`. The port is complete only when that command exits 0: every equation is translated, the regression record is bitwise identical, and every procedure other than a `PROGRAM` has a bitwise unit test (`rules/directives.md` item 4). If the values differ, investigate the translation instead of widening a tolerance.
