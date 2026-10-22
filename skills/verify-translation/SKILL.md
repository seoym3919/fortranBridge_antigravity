---
name: verify-translation
description: Compares numerical outputs of translated C++ or Python code against the original Fortran reference across parameter sweeps to prove mathematical identity (L_2 < 10^{-12}).
---

# /verify-translation

Runs closed-loop mathematical verification to prove that translated C++ or Python implementations produce identical floating-point results to the original Fortran algorithms.

## Usage

```bash
python3 scripts/translation_verifier.py --self-test
```

## Metrics Checked

- Maximum Absolute Error: $L_\infty = \max |y_{\text{ref}} - y_{\text{test}}|$
- Relative $L_2$ Error: $\frac{\|y_{\text{ref}} - y_{\text{test}}\|_2}{\|y_{\text{ref}}\|_2 + \varepsilon} \le 10^{-12}$
- Conservation of physical invariants (energy, mass, momentum) to machine tolerance.
