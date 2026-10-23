---
name: translate-fortran
description: Modernizes Fortran codebases (FORTRAN 66–2003) into C++17/20 (CMake) or vectorized scientific Python (NumPy/SciPy/Numba).
---

# /translate-fortran

Translates entire Fortran codebase directories, repositories, or individual source files into modern, high-performance C++ (C++17/20) or vectorized scientific Python (NumPy, SciPy, Numba) with automated 0-based indexing conversion, topological multi-file linking, COMMON block encapsulation, and numerical equivalence verification.
Bundled sub-skills live at `../<name>/SKILL.md` (`fortran-to-cpp`, `fortran-to-python`, `audit-indexing`, `decompile-format`, `encapsulate-common`, `infer-intents`, `link-project`, `restructure-goto`, `verify-translation`).

## Usage

Modernize an entire Fortran codebase directory to modern C++:
```text
/translate-fortran [directory] --target cpp
```

Modernize an entire Fortran codebase directory to vectorized Python:
```text
/translate-fortran [directory] --target python --out-dir modernized_py
```

Modernize a single Fortran file:
```text
/translate-fortran orbit.f --target both --report audit.md
```

## Codebase Workflow

1. **Directory-Wide Ingestion & AST Lexing**:
   Recursively discovers all `.f`, `.f77`, `.for`, `.f90`, `.f95`, `.f03`, `.f08` files across the codebase directory.
2. **Multi-File Topological Dependency DAG**:
   Builds the Directed Acyclic Graph of `MODULE`, `USE`, `COMMON`, and `CALL` dependencies via `scripts/project_linker.py`, ensuring base definitions and modules are translated before dependent numerical solvers.
3. **COMMON Block Encapsulation**:
   Converts shared memory blocks into typed C++ structs (`include/common_blocks.hpp`) or Python `@dataclass` state objects (`common_state.py`).
4. **Hybrid Static + Semantic LLM Code Synthesis**:
   - Run `scripts/fortran_bridge_cli.py` to generate the topological project scaffold (`CMakeLists.txt` or `__init__.py`), `COMMON`/`EQUIVALENCE` structs, and inferred `INTENT(IN/OUT)` signatures.
   - Refine complex nested mathematical kernels semantically (via `fortran-translator` on Opus 5.5 / Sonnet 5.5 / Gemini 4) rather than relying solely on regex line-by-line transpilation:
     - C++: Emit idiomatic C++17/20 with `std::vector`, `std::span`, and row-major loop transposition.
     - Python: Emit vectorized NumPy/SciPy expressions or `@njit(parallel=True)` kernels.
5. **Codebase Translation Report & Closed-Loop Verification**:
   Emit `CODEBASE_TRANSLATION_REPORT.md`, audit 0-based offsets via `indexing-auditor`, and verify $L_2 < 10^{-12}$ numerical parity via `scripts/translation_verifier.py` and `scripts/f2py_harness.py`.
