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
from typing import Callable, List, Tuple, Dict, Any, Optional

CHECKER_VERSION = "2"

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


def load_numeric(path: str) -> Dict[str, Any]:
    r"""Logical array: C-order values, shape, dtype (float32 or float64) and memory order.

    `.npy` keeps its shape and float width. Text is one float64 vector (shape of the parsed values, order C):
    numbers separated by whitespace or commas, Fortran `D` exponents allowed, blank lines and lines starting
    with `#` or `!` ignored.
    """
    if path.endswith(".npy"):
        try:
            import numpy as np
        except ImportError:
            raise InputError(f"{path}: reading .npy files needs numpy; write the output as text instead")
        try:
            arr = np.load(path)
        except (OSError, ValueError) as exc:
            raise InputError(f"{path}: {exc}")
        if not np.issubdtype(arr.dtype, np.floating):
            raise InputError(f"{path}: expected a floating-point array, got {arr.dtype}")
        if arr.dtype == np.float32:
            dtype = "float32"
        elif arr.dtype == np.float64:
            dtype = "float64"
        else:
            raise InputError(f"{path}: only float32 and float64 arrays are compared, got {arr.dtype}")
        order = "F" if arr.flags.f_contiguous and not arr.flags.c_contiguous else "C"
        logical = np.ascontiguousarray(arr)
        return {"values": [float(x) for x in logical.reshape(-1)], "shape": list(arr.shape),
                "dtype": dtype, "order": order}
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
    return {"values": values, "shape": [len(values)], "dtype": "float64", "order": "C"}


def load_values(path: str) -> List[float]:
    r"""Flat values from an output file. Prefer load_numeric when shape and dtype matter."""
    return load_numeric(path)["values"]


def compare_outputs(y_ref: List[float], y_test: List[float], tol: Optional[float] = None,
                    atol: Optional[float] = None, max_ulp: Optional[int] = None,
                    ref_meta: Optional[Dict[str, Any]] = None,
                    test_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    r"""Elementwise |test - ref| <= atol + rtol |ref|, plus a dtype ULP budget and a +0/-0 sign-bit check.

    rtol is `tol` (float64 default 1e-12). Pooled relative L2 and Linf are reported and are not the verdict.
    An empty comparison, a shape or dtype mismatch, or any NaN/Inf fails (the reason is in "error").
    """
    if ref_meta is None:
        ref_meta = {"values": list(y_ref), "shape": [len(y_ref)], "dtype": "float64", "order": "C"}
    if test_meta is None:
        test_meta = {"values": list(y_test), "shape": [len(y_test)], "dtype": "float64", "order": "C"}
    ref_list, test_list = ref_meta["values"], test_meta["values"]
    shown_tol = 1e-12 if tol is None else tol
    result: Dict[str, Any] = {"tolerance": shown_tol, "n_ref": len(ref_list), "n_test": len(test_list),
                              "passed": False, "shape": list(ref_meta["shape"]), "dtype": ref_meta["dtype"],
                              "ref_order": ref_meta["order"], "test_order": test_meta["order"]}
    if ref_meta["shape"] != test_meta["shape"] or len(ref_list) != len(test_list):
        result["error"] = (f"Length mismatch: reference has {len(ref_list)} values, translation has {len(test_list)}"
                           if len(ref_meta["shape"]) == 1 and len(test_meta["shape"]) == 1
                           else f"Shape mismatch: reference {tuple(ref_meta['shape'])} vs test {tuple(test_meta['shape'])}")
        return result
    if ref_meta["dtype"] != test_meta["dtype"]:
        result["error"] = f"dtype mismatch: reference {ref_meta['dtype']} vs test {test_meta['dtype']}"
        return result
    if not ref_list and not test_list:
        result["error"] = "Both files contain no numbers: nothing was compared"
        return result
    if not all(math.isfinite(v) for v in ref_list) or not all(math.isfinite(v) for v in test_list):
        result["error"] = "Non-finite values (NaN or Inf) in the reference or the translation"
        return result
    try:
        lim = ve.limits(ref_meta["dtype"], rtol=tol, atol=atol, max_ulp=max_ulp)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    compared = ve.compare_vectors(ref_list, test_list, rtol=lim["rtol"], atol=lim["atol"],
                                  max_ulp=int(lim["max_ulp"]), bits=int(lim["bits"]))
    norms = compute_error_norms(ref_list, test_list)
    result.update(norms)
    result.update(passed=bool(compared["passed"]), atol=lim["atol"], max_ulp=int(lim["max_ulp"]),
                  max_ulp_seen=compared["max_ulp_seen"], signbit_mismatches=compared["signbit_mismatches"],
                  points_outside_tolerance=compared["points_outside_tolerance"], limits=lim,
                  tolerance=lim["rtol"])
    return result


def format_report(result: Dict[str, Any], ref_path: str, test_path: str) -> str:
    lines = ["# fortranBridge Translation Verification", "",
             f"- Reference (Fortran): `{ref_path}`", f"- Translation: `{test_path}`",
             f"- Tolerance: rtol {result['tolerance']:.1e}, atol {result.get('atol', 0.0):.1e}, "
             f"max ULP {result.get('max_ulp', 'default')}",
             "- Pass rule: elementwise |test-ref| <= atol + rtol*|ref|, ULP budget, and zero sign bit",
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


# Dyadic kick step. r0=(1,0,0), v0=(0, 1/2, 0), dt=1/2, gm=1.
# r = 1, fac = -1. Position: r0 + dt*v0 + 0.5*dt^2*fac*r0 = (1 - 0.125, 0.25, 0).
# Velocity: v0 + dt*fac*r0 = (-0.5, 0.5, 0). These literals are the closed form, not a second copy of the loop.
EXPECTED_KICK = [0.875, 0.25, 0.0, -0.5, 0.5, 0.0]
# Column-major vs row-major sentinel: a[i, j] = i + 1000*j for i=0..1, j=0..2.
LAYOUT_C = [0.0, 1000.0, 2000.0, 1.0, 1001.0, 2001.0]
LAYOUT_F = [0.0, 1.0, 1000.0, 1001.0, 2000.0, 2001.0]


def run_self_tests() -> bool:
    r"""Check the verifier against a closed-form kick step and a memory-order sentinel."""
    print("[translation_verifier] Running Fortran -> Modern Python Equivalence Tests...")
    passed = True
    r0, v0, dt, gm = [1.0, 0.0, 0.0], [0.0, 0.5, 0.0], 0.5, 1.0
    for label, fn in (("fortran oracle", _fortran_kepler_oracle), ("translated python", _translated_python_kepler)):
        got = fn(r0, v0, dt, gm)
        outcome = compare_outputs(EXPECTED_KICK, got, tol=1e-12, atol=0.0, max_ulp=0)
        print(f"{label}: {got}")
        if not outcome["passed"] or got != EXPECTED_KICK:
            print(f"Closed form rejected {label}: expected {EXPECTED_KICK}")
            passed = False
    shifted = list(EXPECTED_KICK)
    shifted[1] = 0.25 + 1.0e-6
    if compare_outputs(EXPECTED_KICK, shifted)["passed"]:
        print("Negative control not rejected: one component shifted")
        passed = False

    if compare_outputs(LAYOUT_C, LAYOUT_F)["passed"]:
        print("Layout sentinel not rejected: C-order flat matched F-order flat")
        passed = False
    if not compare_outputs(LAYOUT_C, list(LAYOUT_C))["passed"] or not compare_outputs(LAYOUT_F, list(LAYOUT_F))["passed"]:
        print("Layout sentinel rejected two copies of the same order")
        passed = False
    try:
        import numpy as np
        matrix = np.array([[0.0, 1000.0, 2000.0], [1.0, 1001.0, 2001.0]])
        order_c = np.array(matrix, order="C")
        order_f = np.array(matrix, order="F")
        same = compare_outputs([], [], ref_meta=_array_meta(order_c), test_meta=_array_meta(order_f))
        if not same["passed"]:
            print("Same logical matrix in C and F memory order was rejected")
            passed = False
    except ImportError:
        print("numpy is not installed; the in-memory C/F array check was skipped")

    controls = {
        "one value missing": compare_outputs(EXPECTED_KICK, EXPECTED_KICK[:-1]),
        "NaN in output": compare_outputs(EXPECTED_KICK, EXPECTED_KICK[:-1] + [float("nan")]),
        "one local error in a long vector": compare_outputs([1.0] * 1_000_000, [1.0] * 999_999 + [1.0 + 5e-10]),
        "negative zero": compare_outputs([0.0, 0.0], [0.0, -0.0]),
    }
    for label, outcome in controls.items():
        if outcome["passed"]:
            print(f"Negative control not rejected: {label}")
            passed = False
    if not compare_outputs(EXPECTED_KICK, list(EXPECTED_KICK))["passed"]:
        print("Positive control failed: identical outputs were rejected")
        passed = False
    if passed:
        print("\n✔ SELF-TEST PASSED: the kick step matches the closed form and the layout sentinel holds.")
    else:
        print("\n❌ SELF-TEST FAILED: Discrepancy exceeds tolerance.")
    print("\nNote: this self-test checks the verifier's own arithmetic on two built-in Kepler functions. "
          "It does NOT verify your translation; use --ref and --test for that.")
    return passed


def _array_meta(arr) -> Dict[str, Any]:
    import numpy as np
    dtype = "float32" if arr.dtype == np.float32 else "float64"
    order = "F" if arr.flags.f_contiguous and not arr.flags.c_contiguous else "C"
    logical = np.ascontiguousarray(arr)
    return {"values": [float(x) for x in logical.reshape(-1)], "shape": list(arr.shape),
            "dtype": dtype, "order": order}


def main():
    parser = argparse.ArgumentParser(
        description="translation_verifier - verify that a translated program's output matches the Fortran reference. "
                    "Exit status: 0 compared and equal, 1 compared and different, 2 nothing was compared.")
    parser.add_argument("--ref", help="Fortran reference output (text, Fortran D exponents allowed, or .npy)")
    parser.add_argument("--test", help="Translated program's output for the same inputs (text or .npy)")
    parser.add_argument("--tol", type=float, default=None, help="relative tolerance (float64 default 1e-12; "
                        "float32 default 8 * 2^-23 when omitted)")
    parser.add_argument("--atol", type=float, default=None, help="absolute tolerance (default 0)")
    parser.add_argument("--max-ulp", type=int, default=None, help="ULP budget (default: ceil(rtol / machine epsilon))")
    parser.add_argument("--ref-source", help="Fortran source the --ref dump came from")
    parser.add_argument("--test-source", help="translated source the --test dump came from")
    parser.add_argument("--inputs", help="input deck both programs ran on")
    parser.add_argument("--report", help="write a Markdown report to this file")
    parser.add_argument("--self-test", action="store_true", help="check the verifier itself (does not verify a translation)")
    ve.add_cli_options(parser)
    args = parser.parse_args()
    ve.pin_runtime_env()

    if args.self_test:
        sys.exit(0 if run_self_tests() else 1)

    if not args.ref or not args.test:
        parser.print_help(sys.stderr)
        print("\ntranslation_verifier: give both --ref and --test (nothing was compared).", file=sys.stderr)
        sys.exit(2)
    if args.tol is not None and not args.tol > 0:
        print("translation_verifier: --tol must be positive", file=sys.stderr)
        sys.exit(2)
    try:
        ref_meta, test_meta = load_numeric(args.ref), load_numeric(args.test)
    except InputError as exc:
        print(f"translation_verifier: {exc}", file=sys.stderr)
        sys.exit(2)

    result = compare_outputs(ref_meta["values"], test_meta["values"], args.tol, args.atol, args.max_ulp,
                             ref_meta, test_meta)
    if result.get("error") and "must be" in result["error"]:
        print(f"translation_verifier: {result['error']}", file=sys.stderr)
        sys.exit(2)
    report = format_report(result, args.ref, args.test)
    missing = [flag for flag, value in (("--ref-source", args.ref_source), ("--test-source", args.test_source),
                                         ("--inputs", args.inputs)) if not value]
    for flag, value in (("--ref-source", args.ref_source), ("--test-source", args.test_source), ("--inputs", args.inputs)):
        if value and not os.path.isfile(value):
            print(f"translation_verifier: {flag} is not a file: {value}", file=sys.stderr)
            sys.exit(2)
    if missing and result["passed"]:
        print(report)
        print("translation_verifier: a pass must name --ref-source, --test-source and --inputs "
              f"(missing {', '.join(missing)}). Nothing was accepted.", file=sys.stderr)
        sys.exit(2)
    bound = [args.ref, args.test] + [p for p in (args.ref_source, args.test_source, args.inputs) if p]
    base = ve.log_path(args.evidence_dir).parent.parent
    lim = result.get("limits") or {}
    evidence = ve.record_cli(
        args, tool="translation_verifier", plugin="fortranbridge", kind="compare", name="fortran_vs_translation",
        key="compare:" + "|".join(ve.subject(p, base)["path"] for p in (args.ref, args.test)), passed=result["passed"],
        tolerance={"rtol": lim.get("rtol", args.tol), "atol": lim.get("atol", 0.0), "max_ulp": lim.get("max_ulp", 0)},
        subject_paths=bound,
        detail={"checker_version": CHECKER_VERSION, "n_ref": result["n_ref"], "n_test": result["n_test"],
                "shape": result.get("shape"), "dtype": result.get("dtype"),
                "ref_order": result.get("ref_order"), "test_order": result.get("test_order"),
                "rel_l2": result.get("rel_l2"), "rel_linf": result.get("rel_linf"),
                "max_ulp_seen": result.get("max_ulp_seen"), "signbit_mismatches": result.get("signbit_mismatches"),
                "runtime": ve.pin_runtime_env()})
    if evidence and not evidence.get("error"):
        report += f"- Evidence: appended to `{evidence['log']}` (exit status {evidence['exit_status']})\n"
        if "tolerance_loosened_after_failure" in evidence["flags"]:
            report += "- The tolerance is looser than in the previous failed run of this comparison\n"
        if evidence["withheld"]:
            report += ("- **Verdict withheld**: loosened after a failure on unchanged files. "
                       "A --justify sentence does not release it; add a tolerance-approvals.json entry.\n")
    print(report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(report)
    sys.exit(ve.finish("translation_verifier", result["passed"], evidence))


if __name__ == "__main__":
    main()
