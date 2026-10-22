---
name: translate-fortran
description: Master translation command to modernize legacy or modern Fortran codebases (FORTRAN 66/77/90/95/2003) into production C++17/20 or vectorized scientific Python.
---

# /translate-fortran

Translates entire Fortran source files or code routines into modern C++ or Python with automated 0-based indexing conversion, loop restructuring, COMMON block encapsulation, and equivalence verification.

## Usage

Translate to C++:
```text
/translate-fortran solver.f --target cpp
```

Translate to Python:
```text
/translate-fortran solver.f90 --target python
```

Translate to both with an audit report:
```text
/translate-fortran orbit.f --target both --report audit.md
```

## Workflow

1. **Format Lexing & Normalization**:
   Parses fixed-format card layouts (cols 1-72) or modern free-format Fortran via `scripts/fortran_lexer.py`.
2. **COMMON Block Analysis**:
   Converts shared memory blocks into typed C++ structs or Python `@dataclass` objects via `scripts/common_block_analyzer.py`.
3. **Control-Flow Modernization**:
   De-spaghettifies arithmetic IFs, computed GOTOs, and backward loops via `scripts/goto_restructurer.py`.
4. **Code Generation**:
   Generates idiomatic, typed C++17/20 or NumPy code with intrinsic math functions mapped (`DSQRT` -> `std::sqrt`).
5. **Numerical Verification**:
   Validates mathematical output equivalence using `scripts/translation_verifier.py`.
