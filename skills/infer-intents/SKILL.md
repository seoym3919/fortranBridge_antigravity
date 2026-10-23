---
name: infer-intents
description: Static dataflow analysis engine that infers parameter intents (INTENT(IN), INTENT(OUT), INTENT(INOUT)) for legacy Fortran subroutines, ensuring C++ const-correctness and Python return tuples.
disable-model-invocation: true
---

# /infer-intents

Performs definition-use (def-use) static dataflow analysis on Fortran subroutine arguments to determine their intent and optimize modern signatures.

## Usage

```text
/infer-intents Analyze subroutine arguments in solver.f and infer const-correctness.
```

Or via CLI:

```bash
python3 scripts/intent_analyzer.py
```

## Intent Classifications

1. **`INTENT(IN)` (Read-Only)**:
   - Variable appears on right-hand side of assignments, in condition checks, or as input in calls.
   - **C++**: Emitted as `const T&` or `std::span<const T>` or pass-by-value scalar.
   - **Python**: Standard function parameter.

2. **`INTENT(OUT)` (Write-Only)**:
   - Variable appears exclusively on left-hand side of assignments or is populated by calls.
   - **C++**: Emitted as `T&` or `std::span<T>`.
   - **Python**: Removed from caller input parameters and emitted as a return value: `return out1, out2`.

3. **`INTENT(INOUT)` (Read & Written)**:
   - Variable is both read and updated.
   - **C++**: Emitted as mutable reference `T&`.
   - **Python**: Modified in-place or returned as updated state tuple.
