---
name: translate-fortran
description: Modernizes Fortran codebases (FORTRAN 66–2003) into C++17/20 (CMake) or vectorized scientific Python (NumPy/SciPy/Numba).
argument-hint: "[directory|file] [--target cpp|python|both]"
---

# /fortranbridge:translate-fortran

Translates entire Fortran codebase directories, repositories, or individual source files into modern, high-performance C++ (C++17/20) or vectorized scientific Python (NumPy, SciPy, Numba) with automated 0-based indexing conversion, topological multi-file linking, COMMON block encapsulation, and numerical equivalence verification.
Bundled sub-skills live at `../<name>/SKILL.md` (`fortran-to-cpp`, `fortran-to-python`, `audit-indexing`, `decompile-format`, `encapsulate-common`, `infer-intents`, `link-project`, `restructure-goto`, `verify-translation`).

## Usage

Modernize an entire Fortran codebase directory to modern C++:
```text
/fortranbridge:translate-fortran [directory] --target cpp
```

Modernize an entire Fortran codebase directory to vectorized Python:
```text
/fortranbridge:translate-fortran [directory] --target python --out-dir modernized_py
```

Modernize a single Fortran file:
```text
/fortranbridge:translate-fortran orbit.f --target both --report audit.md
```

## Codebase Workflow

1. **Directory-Wide Ingestion & AST Lexing**:
   Recursively discovers all `.f`, `.f77`, `.for`, `.f90`, `.f95`, `.f03`, `.f08` files across the codebase directory.
2. **Multi-File Topological Dependency DAG**:
   Builds the Directed Acyclic Graph of `MODULE`, `USE`, `COMMON`, and `CALL` dependencies via `${CLAUDE_PLUGIN_ROOT}/scripts/project_linker.py`, ensuring base definitions and modules are translated before dependent numerical solvers.
3. **COMMON Block Encapsulation**:
   Converts shared memory blocks into typed C++ structs (`include/common_blocks.hpp`) or Python `@dataclass` state objects (`common_state.py`).
4. **Hybrid Static + Semantic LLM Code Synthesis**:
   - Run `${CLAUDE_PLUGIN_ROOT}/scripts/fortran_bridge_cli.py` to generate the topological project scaffold (`CMakeLists.txt` or `__init__.py`), `COMMON`/`EQUIVALENCE` structs, and inferred `INTENT(IN/OUT)` signatures.
   - Refine complex nested mathematical kernels semantically (via the `fortran-translator` subagent on Opus) rather than relying solely on regex line-by-line transpilation:
     - C++: Start from the generated `std::vector` scaffold; refine toward `std::span` views and row-major loop order where the verifier still passes.
     - Python: Emit vectorized NumPy/SciPy expressions or `@njit(parallel=True)` kernels.
5. **Codebase Translation Report & Closed-Loop Verification**:
   Emit `CODEBASE_TRANSLATION_REPORT.md`, audit 0-based offsets via `indexing-auditor`, and compare outputs with `${CLAUDE_PLUGIN_ROOT}/scripts/translation_verifier.py` (rules/directives.md item 3).
   Then run `${CLAUDE_PLUGIN_ROOT}/scripts/port_completeness.py --original ORIGINAL --translated TRANSLATED --target cpp|python --unit-tests TESTS --evidence-log .verification/log.jsonl`. The port is complete only when that command exits 0: every equation is translated, the regression record is bitwise identical, and every procedure other than a `PROGRAM` has a bitwise unit test (rules/directives.md item 4). If the values differ, investigate the translation instead of widening a tolerance.
