---
name: restructure-goto
description: Restructures spaghetti Fortran control flow (Arithmetic IFs, Computed GOTOs, backward jump loops) into clean while/for loops, switches, and guard clauses.
---

# /restructure-goto

De-spaghettifies legacy Fortran jumps into modern structured control flow:
- **Arithmetic IF**: `IF (EXPR) 10, 20, 30` $\to$ `if (expr < 0) ... else if (expr == 0) ... else ...`
- **Computed GOTO**: `GOTO (10, 20, 30) I` $\to$ `switch (i) { case 1: ... case 2: ... case 3: ... }`
- **Backward GOTO Loops**: `100 I = I + 1 ... IF (I .LE. N) GO TO 100` $\to$ `while (i <= n) { ... }`
- **Error Traps**: `IF (ERR .NE. 0) GO TO 999` $\to$ early return or exception throw.

## Usage

```text
/restructure-goto De-spaghettify subroutines in legacy_solver.f.
```
