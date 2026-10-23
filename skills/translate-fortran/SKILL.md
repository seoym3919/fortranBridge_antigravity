---
name: translate-fortran
description: Master translation command to modernize legacy or modern Fortran codebases (FORTRAN 66/77/90/95/2003) into production C++17/20 or vectorized scientific Python.
---

# /translate-fortran

Translates entire Fortran codebase directories, repositories, or individual source files into modern, high-performance C++ (C++17/20) or vectorized scientific Python (NumPy, SciPy, Numba) with automated 0-based indexing conversion, topological multi-file linking, COMMON block encapsulation, and numerical equivalence verification.

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
4. **Code Generation & Project Packaging**:
   - C++: Emits C++17/20 with `std::vector`, `std::span`, and generates a root `CMakeLists.txt`.
   - Python: Emits vectorized NumPy modules and packages them with `__init__.py`.
5. **Codebase Translation Report & Verification**:
   Emits `CODEBASE_TRANSLATION_REPORT.md` and runs closed-loop verification via `scripts/translation_verifier.py`.
