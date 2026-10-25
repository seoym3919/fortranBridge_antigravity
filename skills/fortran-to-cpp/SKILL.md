---
name: fortran-to-cpp
description: Translates Fortran subroutines and functions into modern, high-performance C++ (C++17/20) with optimal cache contiguity, loop transposition, std::span, and Eigen integration.
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
python3 scripts/fortran_bridge_cli.py kernel.f --target cpp --out kernel.cpp
```

## Features

- **0-Based Indexing**: Converts 1-based Fortran arrays `A(I)` to 0-based C++ `a[i]`.
- **Cache-Optimized Loops**: Transposes nested loops to traverse row-major C++ memory contiguously without cache stalls.
- **COMMON Block Encapsulation**: Shared state is bundled into clean C++ structs.
- **Pass-by-Reference**: Uses `const T&` for read-only parameters and `T&` / `std::span<T>` for mutable tensors.
- **Standard Math Headers**: Maps Fortran intrinsics (`DSQRT`, `DABS`, `DATAN2`) to `<cmath>` and `<algorithm>`.
