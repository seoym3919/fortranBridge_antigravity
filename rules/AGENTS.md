# fortranBridge Antigravity Directives

1. **Subagent Routing**:
   - Master Fortran translation & decomposition: `invoke_subagent` (`TypeName`: `"fortran-translator"`, `Model`: `"pro"`).
   - High-performance C++ modernizer (C++17/20, cache efficiency, std::span): `invoke_subagent` (`TypeName`: `"cpp-modernizer"`, `Model`: `"flash"`).
   - Scientific Python vectorization (NumPy, SciPy, Numba): `invoke_subagent` (`TypeName`: `"python-vectorizer"`, `Model`: `"flash"`).
   - Rigorous mathematical indexing & offset auditor: `invoke_subagent` (`TypeName`: `"indexing-auditor"`, `Model`: `"pro"`).

2. **Translation & Porting Standards**:
   - **Directory-First & Codebase-Wide Scope**: `fortranBridge` operates on directories and whole codebases by default. It recursively discovers all Fortran files, constructs the dependency DAG (`MODULE`, `USE`, `COMMON`, `CALL`), topologically sorts compilation order, and outputs fully structured modern C++ projects (with `CMakeLists.txt`) or Python packages (with `__init__.py`).
   - **0-Based Indexing Conversion**: Every array subscript must be converted to 0-based indexing (`a[i]` for 0-indexed loops, `a[i - 1]` for 1-based literals). Never leave raw Fortran 1-based indexing in C++ or Python.
   - **Memory Layout Contiguity**: When translating multidimensional arrays to C++, transpose nested loops so the innermost loop iterates across the contiguous dimension (avoiding cache-line hopping).
   - **COMMON Block Encapsulation**: Never translate `COMMON` blocks into global mutable variables. Encapsulate them into structured C++ `struct` / `class` instances or Python `@dataclass` state objects passed as references.
   - **Pass-by-Reference Constness**: Identify parameters that are purely read-only and mark them `const T&` or `std::span<const T>` in C++.
   - **Intrinsic Mapping**: Standardize Fortran intrinsics (`DSQRT`, `DABS`, `DATAN2`, `SIGN`, `DIM`) to `<cmath>` / `<algorithm>` in C++ and `math` / `numpy` in Python.
   - **Closed-Loop Verification**: Always verify translated code with `scripts/translation_verifier.py` to confirm mathematical output equivalence ($L_2 < 10^{-12}$).
