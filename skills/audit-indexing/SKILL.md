---
name: audit-indexing
description: Rigorous mathematical audit of translated code for 1-based vs. 0-based offset errors, array bounds, slice ranges, and memory layout order.
disable-model-invocation: true
argument-hint: "<original.f> <translated_file>"
---

# /audit-indexing

Scans translated C++ and Python code against the original Fortran source to detect:
1. **Off-by-One Errors**: Unconverted 1-based subscripts (`a[1]` instead of `a[0]`).
2. **Loop Bounds Inconsistencies**: Loops running up to `<= n` instead of `< n`.
3. **Non-Standard Fortran Bounds**: Arrays declared with negative or custom lower bounds (`DIMENSION A(-5:5)`).
4. **Strided Memory Access**: Multidimensional loops accessing memory across cache strides rather than contiguously.

## Usage

```text
/audit-indexing Compare original orbit.f with translated orbit.cpp and audit all array indices.
```
