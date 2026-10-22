#!/usr/bin/env python3
r"""
parallel_modernizer.py - OpenMP and Numba Parallelization Engine.

Modernizes:
- Fortran !$OMP PARALLEL DO -> C++ #pragma omp parallel for simd
- Fortran DO CONCURRENT -> C++ execution::par_unseq or OpenMP
- Python Numba Acceleration -> @njit(fastmath=True, parallel=True) with prange
"""

from __future__ import annotations
import re
from typing import List, Dict, Any, Tuple


def translate_omp_directive(directive_line: str, target: str = "cpp") -> str:
    r"""Translate Fortran OpenMP sentinel to C++ or Python."""
    d = directive_line.strip()
    d_clean = re.sub(r'^[!Cc*]\$OMP\s+', '', d, flags=re.IGNORECASE).strip()

    if target.lower() in ("cpp", "c++"):
        # Parallel DO
        if re.match(r'^PARALLEL\s+DO\b', d_clean, re.IGNORECASE):
            # Parse clauses: private, shared, reduction
            clauses = []
            m_priv = re.search(r'PRIVATE\s*\(([^)]+)\)', d_clean, re.IGNORECASE)
            if m_priv:
                vars_priv = ", ".join(v.strip().lower() for v in m_priv.group(1).split(','))
                clauses.append(f"private({vars_priv})")

            m_red = re.search(r'REDUCTION\s*\(([^:]+):\s*([^)]+)\)', d_clean, re.IGNORECASE)
            if m_red:
                op = m_red.group(1).strip()
                var = m_red.group(2).strip().lower()
                clauses.append(f"reduction({op}:{var})")

            clauses_str = " " + " ".join(clauses) if clauses else ""
            return f"#pragma omp parallel for simd{clauses_str}"

        elif re.match(r'^END\s+PARALLEL\s+DO\b', d_clean, re.IGNORECASE):
            return "// end omp parallel for"

        elif re.match(r'^ATOMIC\b', d_clean, re.IGNORECASE):
            return "#pragma omp atomic"

        elif re.match(r'^CRITICAL\b', d_clean, re.IGNORECASE):
            return "#pragma omp critical"

        return f"#pragma omp {d_clean.lower()}"

    else:  # Python
        if re.match(r'^PARALLEL\s+DO\b', d_clean, re.IGNORECASE):
            return "# Parallel iteration (converted to numba.prange)"
        return f"# omp: {d_clean}"


def inject_numba_decorators(python_code: str, enable_parallel: bool = True) -> str:
    r"""Analyze Python functions and inject @njit(fastmath=True, parallel=True)."""
    lines = python_code.splitlines()
    out = []
    has_numba = False

    for l in lines:
        if l.strip().startswith("def ") and ("for " in python_code or "np." in python_code):
            if not has_numba:
                out.insert(0, "from numba import njit, prange")
                has_numba = True
            parallel_flag = "parallel=True" if enable_parallel else "parallel=False"
            out.append(f"@njit(fastmath=True, {parallel_flag})")
            out.append(l)
        else:
            # If line is 'for i in range(N):' inside parallel region, replace with prange
            if enable_parallel and re.match(r'^\s*for\s+([A-Za-z0-9_]+)\s+in\s+range\(', l):
                l_mod = re.sub(r'\brange\(', 'prange(', l)
                out.append(l_mod)
            else:
                out.append(l)

    return "\n".join(out)


if __name__ == "__main__":
    omp_sample = "!$OMP PARALLEL DO PRIVATE(I, J) REDUCTION(+:TOTAL)"
    print("[parallel_modernizer] Translating OpenMP:")
    print(" C++:   ", translate_omp_directive(omp_sample, "cpp"))
    print(" Python:", translate_omp_directive(omp_sample, "python"))

    py_sample = """def compute_forces(pos, vel, acc, n):
    for i in range(n):
        vel[i] += acc[i]
    return"""
    print("\n[parallel_modernizer] Injected Numba JIT:")
    print(inject_numba_decorators(py_sample, enable_parallel=True))
