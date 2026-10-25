---
name: verify-translation
description: Compare the output of translated C++/Python against the original Fortran program's output (relative L2 and Linf <= 1e-12). Use after any Fortran translation, or when asked to prove it is equivalent.
argument-hint: "--ref FORTRAN_OUT --test TRANSLATED_OUT [--tol 1e-12] [--report FILE]"
---

# /verify-translation

Runs closed-loop numerical verification: the translated program must reproduce the original Fortran program's output.
The script decides; Claude and Antigravity must not judge equivalence by reading numbers themselves.

## Usage

1. Run the original Fortran program and the translation on the **same inputs** (a parameter sweep, fixed seeds)
   and write each program's numbers to a file: text with numbers separated by spaces, commas or lines (Fortran
   `D` exponents such as `1.0D-3` are accepted; lines starting with `#` or `!` are ignored), or a `.npy` array.
2. Compare:

```bash
python3 scripts/translation_verifier.py --ref fortran.out --test translated.out --tol 1e-12 --report verification.md
```

`--self-test` checks the verifier itself on built-in functions (including negative controls that must be
rejected). **It does not verify a translation.**

## Verdict, exit status and what to report

- **Pass rule:** relative $L_2 \le$ `--tol` **and** relative $L_\infty \le$ `--tol`.
- **Always a failure:** a different number of values, no values at all, any NaN or Inf.
- **Exit status:** 0 compared and equal; 1 compared and different; 2 nothing compared (missing arguments,
  unreadable or non-numeric files).
- **Report to the user:** say "equivalent" only if the exit status was 0, and quote the status, the number of points and the
  norms. Never describe a `--self-test` result as evidence about a translation.

## Metrics Checked

- Maximum Absolute Error: $L_\infty = \max |y_{\text{ref}} - y_{\text{test}}|$
- Relative $L_2$ Error: $\frac{\|y_{\text{ref}} - y_{\text{test}}\|_2}{\|y_{\text{ref}}\|_2 + \varepsilon} \le 10^{-12}$
- Relative $L_\infty$ Error: $\frac{\max |y_{\text{ref}} - y_{\text{test}}|}{\max |y_{\text{ref}}| + \varepsilon} \le 10^{-12}$

Conservation of physical invariants (energy, mass, momentum) is not checked by this script; if it matters, write the
invariant as an output column of both programs so that it is compared too.
