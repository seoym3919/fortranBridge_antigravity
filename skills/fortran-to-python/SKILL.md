---
name: fortran-to-python
description: Translates Fortran subroutines and functions into vectorized scientific Python (NumPy, SciPy, Numba) with clean multi-value returns and dataclass state models.
disable-model-invocation: true
argument-hint: "<file.f> [--out FILE.py]"
---

# /fortran-to-python

Translates Fortran routines into idiomatic scientific Python with vectorized NumPy expressions.

## Usage

```text
/fortran-to-python solver.f --out solver.py
```

Or via CLI:

```bash
python3 scripts/fortran_bridge_cli.py solver.f --target python --out solver.py
```

## Features

- **Vectorized Array Expressions**: Replaces scalar `DO` loops with contiguous NumPy array operations (`c = a + b`).
- **State Dataclasses**: Replaces `COMMON /NAME/` blocks with `@dataclass` state objects.
- **Pythonic Returns**: Replaces mutating output pointer arguments with multiple returned values: `return x_new, converged`.
- **Numba Ready**: Compatible with `@njit(fastmath=True)` for near-Fortran execution speed.
