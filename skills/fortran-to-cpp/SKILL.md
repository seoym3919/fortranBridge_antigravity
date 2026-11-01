---
name: fortran-to-cpp
description: Translates Fortran subroutines and functions into modern, high-performance C++ (C++17/20) keeping the Fortran equations and loop order, with std::vector / std::span and COMMON block structs.
disable-model-invocation: true
argument-hint: "<file.f> [--out FILE.cpp]"
---

# /fortran-to-cpp

Translates Fortran routines into idiomatic, zero-overhead C++17/20.

## Usage

```text
/fortran-to-cpp kernel.f --out kernel.cpp
```

Or via CLI:

```bash
python3 ~/.gemini/config/plugins/fortranbridge/scripts/fortran_bridge_cli.py kernel.f --target cpp --out kernel.cpp
```

## Features

- **0-Based Indexing**: Converts 1-based Fortran arrays `A(I)` to 0-based C++ `a[i]`.
- **Loop Order**: The Fortran loop order is kept, including during refinement. Interchanging loops changes the algorithm, even where the output happens to stay the same.
- **COMMON Block Encapsulation**: Shared state is bundled into clean C++ structs.
- **Pass-by-Reference**: `INTENT(IN)` arrays become `const std::vector<T>&`, modified scalars become `T&`. Refinement may switch arrays to `std::span<T>`.
- **Standard Math Headers**: Maps Fortran intrinsics (`DSQRT`, `DABS`, `DATAN2`) to `<cmath>` and `<algorithm>`.
