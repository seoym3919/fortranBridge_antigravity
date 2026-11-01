---
name: fortran-to-python
description: Translates Fortran subroutines and functions into scientific Python (NumPy) with identical equations and algorithm, clean multi-value returns and dataclass state models.
disable-model-invocation: true
argument-hint: "<file.f> [--out FILE.py]"
---

# /fortran-to-python

Translates Fortran routines into scientific Python with NumPy arrays, keeping the equations and the algorithm identical.

## Usage

```text
/fortran-to-python solver.f --out solver.py
```

Or via CLI:

```bash
python3 ~/.gemini/config/plugins/fortranbridge/scripts/fortran_bridge_cli.py solver.f --target python --out solver.py
```

## Features

- **Array Expressions**: A Fortran whole-array statement becomes a NumPy array expression (`c = a + b`). A `DO` loop stays a loop, in the same order: rewriting it as an array expression changes the algorithm.
- **State Dataclasses**: Replaces `COMMON /NAME/` blocks with `@dataclass` state objects.
- **Pythonic Returns**: Replaces mutating output pointer arguments with multiple returned values: `return x_new, converged`.
