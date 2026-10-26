#!/usr/bin/env python3
r"""
translation_verifier.py - Closed-Loop Numerical Equivalence Verifier for Fortran Translations.

Compares numerical outputs of translated C++ or Python implementations
against legacy Fortran benchmarks across parameter sweeps to prove mathematical equivalence:
    \| y_{\text{fortran}} - y_{\text{translated}} \|_{L_2} \le 10^{-12}
"""

from __future__ import annotations
import math
import os
import sys
import argparse
from typing import Callable, List, Tuple, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verification_evidence as ve  # noqa: E402


def compute_error_norms(y_ref: List[float], y_test: List[float], eps: float = 1e-15) -> Dict[str, float]:
    r"""Compute relative and absolute error norms between reference and translated outputs."""
    if len(y_ref) != len(y_test):
        raise ValueError(f"Length mismatch: {len(y_ref)} != {len(y_test)}")

    n = len(y_ref)
    sum_diff_sq = 0.0
    sum_ref_sq = 0.0
    max_diff = 0.0
    max_ref = 0.0

    for i in range(n):
        diff = abs(y_ref[i] - y_test[i])
        ref = abs(y_ref[i])
        sum_diff_sq += diff * diff
        sum_ref_sq += ref * ref
        if diff > max_diff:
            max_diff = diff
        if ref > max_ref:
            max_ref = ref

    l2_abs = math.sqrt(sum_diff_sq / n)
    ref_l2 = math.sqrt(sum_ref_sq / n)
    rel_l2 = l2_abs / (ref_l2 + eps)
    rel_linf = max_diff / (max_ref + eps)

    return {
        "n_points": n,
        "l2_abs": l2_abs,
        "linf_abs": max_diff,
        "rel_l2": rel_l2,
        "rel_linf": rel_linf,
    }


# ---------------------------------------------------------------------------
# Comparing real output files
# ---------------------------------------------------------------------------

class InputError(Exception):
    """An output file cannot be read as numbers (exit status 2: nothing was compared)."""


def parse_number(token: str) -> float:
    r"""float() that also accepts Fortran exponents: 1.0D-3, 2.5d+02."""
    return float(token.replace("D", "E").replace("d", "e"))


def load_values(path: str) -> List[float]:
    r"""Read a reference/test output file as a flat list of floats.

    `.npy` needs numpy. Anything else is text: numbers separated by whitespace or commas, Fortran `D`
    exponents allowed, blank lines and lines starting with `#` or `!` ignored.
    """
    if path.endswith(".npy"):
        try:
            import numpy as np
        except ImportError:
            raise InputError(f"{path}: reading .npy files needs numpy; write the output as text instead")
        try:
            return [float(x) for x in np.load(path).flatten()]
        except (OSError, ValueError) as exc:
            raise InputError(f"{path}: {exc}")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise InputError(f"{path}: {exc.strerror or exc}")
    values: List[float] = []
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line[0] in "#!":
            continue
        for token in line.replace(",", " ").split():
            try:
                values.append(parse_number(token))
            except ValueError:
                raise InputError(f"{path}, line {number}: {token!r} is not a number")
    return values


def compare_outputs(y_ref: List[float], y_test: List[float], tol: float = 1e-12) -> Dict[str, Any]:
    r"""Verdict for two output vectors. Pass requires Rel L_2 <= tol AND Rel L_inf <= tol.

    An empty comparison, a different number of values, or any NaN/Inf fails (with the reason in "error").
    """
    result: Dict[str, Any] = {"tolerance": tol, "n_ref": len(y_ref), "n_test": len(y_test), "passed": False}
    if not y_ref and not y_test:
        result["error"] = "Both files contain no numbers: nothing was compared"
    elif len(y_ref) != len(y_test):
        result["error"] = f"Length mismatch: reference has {len(y_ref)} values, translation has {len(y_test)}"
    elif not all(math.isfinite(v) for v in y_ref) or not all(math.isfinite(v) for v in y_test):
        result["error"] = "Non-finite values (NaN or Inf) in the reference or the translation"
    else:
        norms = compute_error_norms(y_ref, y_test)
        result.update(norms)
        result["passed"] = norms["rel_l2"] <= tol and norms["rel_linf"] <= tol
    return result


def format_report(result: Dict[str, Any], ref_path: str, test_path: str) -> str:
    lines = ["# fortranBridge Translation Verification", "",
             f"- Reference (Fortran): `{ref_path}`", f"- Translation: `{test_path}`",
             f"- Tolerance: {result['tolerance']:.1e} on relative L2 and relative Linf",
             f"- Status: **{'PASSED' if result['passed'] else 'FAILED'}**"]
    if "error" in result:
        lines.append(f"- Reason: {result['error']}")
    else:
        lines += [f"- Points compared: {result['n_points']}", f"- Relative L2: {result['rel_l2']:.3e}",
                  f"- Relative Linf: {result['rel_linf']:.3e}", f"- Max absolute difference: {result['linf_abs']:.3e}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Self-Test Reference Implementations
# ---------------------------------------------------------------------------

def _fortran_kepler_oracle(r0, v0, dt, gm):
    r"""Exact Fortran 77 semantic simulation (1-based semantics simulated in Python)."""
    # R0 = [x, y, z], V0 = [vx, vy, vz]
    r = math.sqrt(r0[0]**2 + r0[1]**2 + r0[2]**2)
    rf = [0.0, 0.0, 0.0]
    vf = [0.0, 0.0, 0.0]
    fac = -gm / (r**3)
    for i in range(3):
        rf[i] = r0[i] + dt * v0[i] + 0.5 * dt**2 * fac * r0[i]
        vf[i] = v0[i] + dt * fac * r0[i]
    return rf + vf


def _translated_python_kepler(r0, v0, dt, gm):
    r"""Output of fortranBridge translation to modern Python."""
    r = math.sqrt(r0[0]**2 + r0[1]**2 + r0[2]**2)
    fac = -gm / (r**3)
    rf = [r0[i] + dt * v0[i] + 0.5 * dt**2 * fac * r0[i] for i in range(3)]
    vf = [v0[i] + dt * fac * r0[i] for i in range(3)]
    return rf + vf


def run_self_tests() -> bool:
    r"""Execute equivalence tests across test parameter suites."""
    print("[translation_verifier] Running Fortran -> Modern Python Equivalence Tests...")

    import random
    rng = random.Random(12345)
    all_ref = []
    all_test = []

    for _ in range(100):
        r0 = [rng.uniform(-10.0, 10.0) for _ in range(3)]
        v0 = [rng.uniform(-2.0, 2.0) for _ in range(3)]
        dt = rng.uniform(0.001, 0.1)
        gm = rng.uniform(1.0, 100.0)

        out_ref = _fortran_kepler_oracle(r0, v0, dt, gm)
        out_test = _translated_python_kepler(r0, v0, dt, gm)

        all_ref.extend(out_ref)
        all_test.extend(out_test)

    norms = compute_error_norms(all_ref, all_test)
    print(f"Total evaluation points: {norms['n_points']}")
    print(f"Relative L_2 error:      {norms['rel_l2']:.3e}")
    print(f"Relative L_infinity:     {norms['rel_linf']:.3e}")

    passed = (norms['rel_l2'] < 1e-12) and (norms['rel_linf'] < 1e-12)

    # Negative controls: the verifier must reject a wrong translation, a missing value and a NaN.
    controls = {
        "perturbed by 1e-9": compare_outputs(all_ref, [v * (1.0 + 1e-9) for v in all_test]),
        "one value missing": compare_outputs(all_ref, all_test[:-1]),
        "NaN in output": compare_outputs(all_ref, all_test[:-1] + [float("nan")]),
        "one local error in a long vector": compare_outputs([1.0] * 1_000_000, [1.0] * 999_999 + [1.0 + 5e-10]),
    }
    for label, outcome in controls.items():
        if outcome["passed"]:
            print(f"Negative control not rejected: {label}")
            passed = False
    if not compare_outputs(all_ref, list(all_test))["passed"]:
        print("Positive control failed: identical outputs were rejected")
        passed = False
    if passed:
        print("\n✔ SELF-TEST PASSED: the built-in reference and translated functions agree within machine tolerance.")
    else:
        print("\n❌ SELF-TEST FAILED: Discrepancy exceeds tolerance.")
    print("\nNote: this self-test checks the verifier's own arithmetic on two built-in Kepler functions. "
          "It does NOT verify your translation; use --ref and --test for that.")
    return passed


def main():
    parser = argparse.ArgumentParser(
        description="translation_verifier - verify that a translated program's output matches the Fortran reference. "
                    "Exit status: 0 compared and equal, 1 compared and different, 2 nothing was compared.")
    parser.add_argument("--ref", help="Fortran reference output (text, Fortran D exponents allowed, or .npy)")
    parser.add_argument("--test", help="Translated program's output for the same inputs (text or .npy)")
    parser.add_argument("--tol", type=float, default=1e-12, help="relative L2 and Linf tolerance (default 1e-12)")
    parser.add_argument("--report", help="write a Markdown report to this file")
    parser.add_argument("--self-test", action="store_true", help="check the verifier itself (does not verify a translation)")
    ve.add_cli_options(parser)
    args = parser.parse_args()

    if args.self_test:
        sys.exit(0 if run_self_tests() else 1)

    if not args.ref or not args.test:
        parser.print_help(sys.stderr)
        print("\ntranslation_verifier: give both --ref and --test (nothing was compared).", file=sys.stderr)
        sys.exit(2)
    if not args.tol > 0:
        print("translation_verifier: --tol must be positive", file=sys.stderr)
        sys.exit(2)
    try:
        y_ref, y_test = load_values(args.ref), load_values(args.test)
    except InputError as exc:
        print(f"translation_verifier: {exc}", file=sys.stderr)
        sys.exit(2)

    result = compare_outputs(y_ref, y_test, args.tol)
    report = format_report(result, args.ref, args.test)
    base = ve.log_path(args.evidence_dir).parent.parent
    evidence = ve.record_cli(
        args, tool="translation_verifier", plugin="fortranbridge", kind="compare", name="fortran_vs_translation",
        key="compare:" + "|".join(ve.subject(p, base)["path"] for p in (args.ref, args.test)), passed=result["passed"],
        tolerance={"tol": args.tol}, subject_paths=[args.ref, args.test],
        detail={"n_ref": result["n_ref"], "n_test": result["n_test"], "rel_l2": result.get("rel_l2"),
                "rel_linf": result.get("rel_linf")})
    if evidence and not evidence.get("error"):
        report += f"- Evidence: appended to `{evidence['log']}` (exit status {evidence['exit_status']})\n"
        if "tolerance_loosened_after_failure" in evidence["flags"]:
            report += "- The tolerance is looser than in the previous failed run of this comparison\n"
        if evidence["withheld"]:
            report += "- **Verdict withheld**: loosened after a failure on unchanged files; add --justify \"reason\"\n"
    print(report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(report)
    sys.exit(ve.finish("translation_verifier", result["passed"], evidence))


if __name__ == "__main__":
    main()
