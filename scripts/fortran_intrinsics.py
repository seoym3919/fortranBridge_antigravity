#!/usr/bin/env python3
r"""
fortran_intrinsics.py - Comprehensive Fortran-to-C++ and Fortran-to-Python Intrinsic Function Map.

Maps FORTRAN 66/77/90/95/2003 mathematical, type conversion, and array intrinsics
to equivalent modern C++ (<cmath>, <algorithm>, Eigen) and Python (math, numpy) expressions.
"""

from __future__ import annotations
from typing import Dict, Any, Tuple, Optional


# Intrinsic mapping database
# Format:
# "FORTRAN_INTRINSIC": {
#     "cpp": "C++ expression template (using {args})",
#     "python_scalar": "Python math template",
#     "python_vector": "NumPy vector template",
#     "header_cpp": "<cmath>" or "<algorithm>",
#     "category": "math" | "conversion" | "array" | "string"
# }

FORTRAN_INTRINSICS: Dict[str, Dict[str, Any]] = {
    # Square Root & Hypotenuse
    "SQRT": {"cpp": "std::sqrt({0})", "python_scalar": "math.sqrt({0})", "python_vector": "np.sqrt({0})", "header_cpp": "<cmath>", "category": "math"},
    "DSQRT": {"cpp": "std::sqrt({0})", "python_scalar": "math.sqrt({0})", "python_vector": "np.sqrt({0})", "header_cpp": "<cmath>", "category": "math"},
    "CSQRT": {"cpp": "std::sqrt({0})", "python_scalar": "cmath.sqrt({0})", "python_vector": "np.sqrt({0})", "header_cpp": "<complex>", "category": "math"},
    "HYPOT": {"cpp": "std::hypot({0}, {1})", "python_scalar": "math.hypot({0}, {1})", "python_vector": "np.hypot({0}, {1})", "header_cpp": "<cmath>", "category": "math"},

    # Absolute Value
    "ABS": {"cpp": "std::abs({0})", "python_scalar": "abs({0})", "python_vector": "np.abs({0})", "header_cpp": "<cmath>", "category": "math"},
    "IABS": {"cpp": "std::abs({0})", "python_scalar": "abs({0})", "python_vector": "np.abs({0})", "header_cpp": "<cmath>", "category": "math"},
    "DABS": {"cpp": "std::abs({0})", "python_scalar": "abs({0})", "python_vector": "np.abs({0})", "header_cpp": "<cmath>", "category": "math"},
    "CABS": {"cpp": "std::abs({0})", "python_scalar": "abs({0})", "python_vector": "np.abs({0})", "header_cpp": "<complex>", "category": "math"},

    # Trigonometric Functions
    "SIN": {"cpp": "std::sin({0})", "python_scalar": "math.sin({0})", "python_vector": "np.sin({0})", "header_cpp": "<cmath>", "category": "math"},
    "DSIN": {"cpp": "std::sin({0})", "python_scalar": "math.sin({0})", "python_vector": "np.sin({0})", "header_cpp": "<cmath>", "category": "math"},
    "COS": {"cpp": "std::cos({0})", "python_scalar": "math.cos({0})", "python_vector": "np.cos({0})", "header_cpp": "<cmath>", "category": "math"},
    "DCOS": {"cpp": "std::cos({0})", "python_scalar": "math.cos({0})", "python_vector": "np.cos({0})", "header_cpp": "<cmath>", "category": "math"},
    "TAN": {"cpp": "std::tan({0})", "python_scalar": "math.tan({0})", "python_vector": "np.tan({0})", "header_cpp": "<cmath>", "category": "math"},
    "DTAN": {"cpp": "std::tan({0})", "python_scalar": "math.tan({0})", "python_vector": "np.tan({0})", "header_cpp": "<cmath>", "category": "math"},

    # Inverse Trig
    "ASIN": {"cpp": "std::asin({0})", "python_scalar": "math.asin({0})", "python_vector": "np.arcsin({0})", "header_cpp": "<cmath>", "category": "math"},
    "DASIN": {"cpp": "std::asin({0})", "python_scalar": "math.asin({0})", "python_vector": "np.arcsin({0})", "header_cpp": "<cmath>", "category": "math"},
    "ACOS": {"cpp": "std::acos({0})", "python_scalar": "math.acos({0})", "python_vector": "np.arccos({0})", "header_cpp": "<cmath>", "category": "math"},
    "DACOS": {"cpp": "std::acos({0})", "python_scalar": "math.acos({0})", "python_vector": "np.arccos({0})", "header_cpp": "<cmath>", "category": "math"},
    "ATAN": {"cpp": "std::atan({0})", "python_scalar": "math.atan({0})", "python_vector": "np.arctan({0})", "header_cpp": "<cmath>", "category": "math"},
    "DATAN": {"cpp": "std::atan({0})", "python_scalar": "math.atan({0})", "python_vector": "np.arctan({0})", "header_cpp": "<cmath>", "category": "math"},
    "ATAN2": {"cpp": "std::atan2({0}, {1})", "python_scalar": "math.atan2({0}, {1})", "python_vector": "np.arctan2({0}, {1})", "header_cpp": "<cmath>", "category": "math"},
    "DATAN2": {"cpp": "std::atan2({0}, {1})", "python_scalar": "math.atan2({0}, {1})", "python_vector": "np.arctan2({0}, {1})", "header_cpp": "<cmath>", "category": "math"},

    # Hyperbolic Functions
    "SINH": {"cpp": "std::sinh({0})", "python_scalar": "math.sinh({0})", "python_vector": "np.sinh({0})", "header_cpp": "<cmath>", "category": "math"},
    "DSINH": {"cpp": "std::sinh({0})", "python_scalar": "math.sinh({0})", "python_vector": "np.sinh({0})", "header_cpp": "<cmath>", "category": "math"},
    "COSH": {"cpp": "std::cosh({0})", "python_scalar": "math.cosh({0})", "python_vector": "np.cosh({0})", "header_cpp": "<cmath>", "category": "math"},
    "DCOSH": {"cpp": "std::cosh({0})", "python_scalar": "math.cosh({0})", "python_vector": "np.cosh({0})", "header_cpp": "<cmath>", "category": "math"},
    "TANH": {"cpp": "std::tanh({0})", "python_scalar": "math.tanh({0})", "python_vector": "np.tanh({0})", "header_cpp": "<cmath>", "category": "math"},
    "DTANH": {"cpp": "std::tanh({0})", "python_scalar": "math.tanh({0})", "python_vector": "np.tanh({0})", "header_cpp": "<cmath>", "category": "math"},

    # Exponential and Logarithm
    "EXP": {"cpp": "std::exp({0})", "python_scalar": "math.exp({0})", "python_vector": "np.exp({0})", "header_cpp": "<cmath>", "category": "math"},
    "DEXP": {"cpp": "std::exp({0})", "python_scalar": "math.exp({0})", "python_vector": "np.exp({0})", "header_cpp": "<cmath>", "category": "math"},
    "LOG": {"cpp": "std::log({0})", "python_scalar": "math.log({0})", "python_vector": "np.log({0})", "header_cpp": "<cmath>", "category": "math"},
    "ALOG": {"cpp": "std::log({0})", "python_scalar": "math.log({0})", "python_vector": "np.log({0})", "header_cpp": "<cmath>", "category": "math"},
    "DLOG": {"cpp": "std::log({0})", "python_scalar": "math.log({0})", "python_vector": "np.log({0})", "header_cpp": "<cmath>", "category": "math"},
    "LOG10": {"cpp": "std::log10({0})", "python_scalar": "math.log10({0})", "python_vector": "np.log10({0})", "header_cpp": "<cmath>", "category": "math"},
    "ALOG10": {"cpp": "std::log10({0})", "python_scalar": "math.log10({0})", "python_vector": "np.log10({0})", "header_cpp": "<cmath>", "category": "math"},
    "DLOG10": {"cpp": "std::log10({0})", "python_scalar": "math.log10({0})", "python_vector": "np.log10({0})", "header_cpp": "<cmath>", "category": "math"},

    # Modulo & Remainder
    "MOD": {"cpp": "std::fmod({0}, {1})", "python_scalar": "math.fmod({0}, {1})", "python_vector": "np.fmod({0}, {1})", "header_cpp": "<cmath>", "category": "math"},
    "AMOD": {"cpp": "std::fmod({0}, {1})", "python_scalar": "math.fmod({0}, {1})", "python_vector": "np.fmod({0}, {1})", "header_cpp": "<cmath>", "category": "math"},
    "DMOD": {"cpp": "std::fmod({0}, {1})", "python_scalar": "math.fmod({0}, {1})", "python_vector": "np.fmod({0}, {1})", "header_cpp": "<cmath>", "category": "math"},
    "MODULO": {"cpp": "std::fmod({0}, {1})", "python_scalar": "{0} % {1}", "python_vector": "{0} % {1}", "header_cpp": "<cmath>", "category": "math"},

    # Sign Transfer & Positive Difference
    # Fortran SIGN(A, B): returns |A| * sgn(B). If B >= 0 -> |A|, else -|A|.
    "SIGN": {"cpp": "std::copysign({0}, {1})", "python_scalar": "math.copysign({0}, {1})", "python_vector": "np.copysign({0}, {1})", "header_cpp": "<cmath>", "category": "math"},
    "ISIGN": {"cpp": "(({1} >= 0) ? std::abs({0}) : -std::abs({0}))", "python_scalar": "(abs({0}) if {1} >= 0 else -abs({0}))", "python_vector": "np.where({1} >= 0, np.abs({0}), -np.abs({0}))", "header_cpp": "<cmath>", "category": "math"},
    "DSIGN": {"cpp": "std::copysign({0}, {1})", "python_scalar": "math.copysign({0}, {1})", "python_vector": "np.copysign({0}, {1})", "header_cpp": "<cmath>", "category": "math"},

    # Fortran DIM(A, B): returns max(0, A - B)
    "DIM": {"cpp": "std::max(0.0, {0} - {1})", "python_scalar": "max(0.0, {0} - {1})", "python_vector": "np.maximum(0.0, {0} - {1})", "header_cpp": "<algorithm>", "category": "math"},
    "IDIM": {"cpp": "std::max(0, {0} - {1})", "python_scalar": "max(0, {0} - {1})", "python_vector": "np.maximum(0, {0} - {1})", "header_cpp": "<algorithm>", "category": "math"},
    "DDIM": {"cpp": "std::max(0.0, {0} - {1})", "python_scalar": "max(0.0, {0} - {1})", "python_vector": "np.maximum(0.0, {0} - {1})", "header_cpp": "<algorithm>", "category": "math"},

    # Min / Max
    "MIN": {"cpp": "std::min({{{args}}})", "python_scalar": "min({args})", "python_vector": "np.minimum({args})", "header_cpp": "<algorithm>", "category": "math"},
    "MIN0": {"cpp": "std::min({{{args}}})", "python_scalar": "min({args})", "python_vector": "np.minimum({args})", "header_cpp": "<algorithm>", "category": "math"},
    "AMIN0": {"cpp": "static_cast<double>(std::min({{{args}}}))", "python_scalar": "float(min({args}))", "python_vector": "np.minimum({args}).astype(float)", "header_cpp": "<algorithm>", "category": "math"},
    "MIN1": {"cpp": "static_cast<int>(std::min({{{args}}}))", "python_scalar": "int(min({args}))", "python_vector": "np.minimum({args}).astype(int)", "header_cpp": "<algorithm>", "category": "math"},
    "AMIN1": {"cpp": "std::min({{{args}}})", "python_scalar": "min({args})", "python_vector": "np.minimum({args})", "header_cpp": "<algorithm>", "category": "math"},
    "DMIN1": {"cpp": "std::min({{{args}}})", "python_scalar": "min({args})", "python_vector": "np.minimum({args})", "header_cpp": "<algorithm>", "category": "math"},

    "MAX": {"cpp": "std::max({{{args}}})", "python_scalar": "max({args})", "python_vector": "np.maximum({args})", "header_cpp": "<algorithm>", "category": "math"},
    "MAX0": {"cpp": "std::max({{{args}}})", "python_scalar": "max({args})", "python_vector": "np.maximum({args})", "header_cpp": "<algorithm>", "category": "math"},
    "AMAX0": {"cpp": "static_cast<double>(std::max({{{args}}}))", "python_scalar": "float(max({args}))", "python_vector": "np.maximum({args}).astype(float)", "header_cpp": "<algorithm>", "category": "math"},
    "MAX1": {"cpp": "static_cast<int>(std::max({{{args}}}))", "python_scalar": "int(max({args}))", "python_vector": "np.maximum({args}).astype(int)", "header_cpp": "<algorithm>", "category": "math"},
    "AMAX1": {"cpp": "std::max({{{args}}})", "python_scalar": "max({args})", "python_vector": "np.maximum({args})", "header_cpp": "<algorithm>", "category": "math"},
    "DMAX1": {"cpp": "std::max({{{args}}})", "python_scalar": "max({args})", "python_vector": "np.maximum({args})", "header_cpp": "<algorithm>", "category": "math"},

    # Type Conversions
    "FLOAT": {"cpp": "static_cast<float>({0})", "python_scalar": "float({0})", "python_vector": "{0}.astype(np.float32)", "header_cpp": "", "category": "conversion"},
    "DFLOAT": {"cpp": "static_cast<double>({0})", "python_scalar": "float({0})", "python_vector": "{0}.astype(np.float64)", "header_cpp": "", "category": "conversion"},
    "DBLE": {"cpp": "static_cast<double>({0})", "python_scalar": "float({0})", "python_vector": "{0}.astype(np.float64)", "header_cpp": "", "category": "conversion"},
    "REAL": {"cpp": "static_cast<double>({0})", "python_scalar": "float({0})", "python_vector": "{0}.astype(np.float64)", "header_cpp": "", "category": "conversion"},
    "SNGL": {"cpp": "static_cast<float>({0})", "python_scalar": "float({0})", "python_vector": "{0}.astype(np.float32)", "header_cpp": "", "category": "conversion"},
    "INT": {"cpp": "static_cast<int>({0})", "python_scalar": "int({0})", "python_vector": "{0}.astype(int)", "header_cpp": "", "category": "conversion"},
    "IFIX": {"cpp": "static_cast<int>({0})", "python_scalar": "int({0})", "python_vector": "{0}.astype(int)", "header_cpp": "", "category": "conversion"},
    "IDINT": {"cpp": "static_cast<int>({0})", "python_scalar": "int({0})", "python_vector": "{0}.astype(int)", "header_cpp": "", "category": "conversion"},

    # Nearest Integer & Rounding
    "NINT": {"cpp": "static_cast<int>(std::round({0}))", "python_scalar": "round({0})", "python_vector": "np.round({0}).astype(int)", "header_cpp": "<cmath>", "category": "conversion"},
    "IDNINT": {"cpp": "static_cast<int>(std::round({0}))", "python_scalar": "round({0})", "python_vector": "np.round({0}).astype(int)", "header_cpp": "<cmath>", "category": "conversion"},
    "ANINT": {"cpp": "std::round({0})", "python_scalar": "float(round({0}))", "python_vector": "np.round({0})", "header_cpp": "<cmath>", "category": "conversion"},
    "DNINT": {"cpp": "std::round({0})", "python_scalar": "float(round({0}))", "python_vector": "np.round({0})", "header_cpp": "<cmath>", "category": "conversion"},

    # Array Reductions & Matrix Intrinsics (Fortran 90+)
    "SUM": {"cpp": "std::accumulate({0}.begin(), {0}.end(), 0.0)", "python_scalar": "sum({0})", "python_vector": "np.sum({0})", "header_cpp": "<numeric>", "category": "array"},
    "PRODUCT": {"cpp": "std::accumulate({0}.begin(), {0}.end(), 1.0, std::multiplies<>())", "python_scalar": "math.prod({0})", "python_vector": "np.prod({0})", "header_cpp": "<numeric>", "category": "array"},
    "DOT_PRODUCT": {"cpp": "std::inner_product({0}.begin(), {0}.end(), {1}.begin(), 0.0)", "python_scalar": "sum(x*y for x,y in zip({0},{1}))", "python_vector": "np.dot({0}, {1})", "header_cpp": "<numeric>", "category": "array"},
    "MATMUL": {"cpp": "({0} * {1})", "python_scalar": "None", "python_vector": "({0} @ {1})", "header_cpp": "", "category": "array"},
    "MAXVAL": {"cpp": "*std::max_element({0}.begin(), {0}.end())", "python_scalar": "max({0})", "python_vector": "np.max({0})", "header_cpp": "<algorithm>", "category": "array"},
    "MINVAL": {"cpp": "*std::min_element({0}.begin(), {0}.end())", "python_scalar": "min({0})", "python_vector": "np.min({0})", "header_cpp": "<algorithm>", "category": "array"},
    "TRANSPOSE": {"cpp": "{0}.transpose()", "python_scalar": "None", "python_vector": "{0}.T", "header_cpp": "", "category": "array"},
    "SIZE": {"cpp": "static_cast<int>({0}.size())", "python_scalar": "len({0})", "python_vector": "{0}.size", "header_cpp": "", "category": "array"},
    "SHAPE": {"cpp": "/* shape */", "python_scalar": "/* shape */", "python_vector": "{0}.shape", "header_cpp": "", "category": "array"},
}


def translate_intrinsic(name: str, args: list[str], target: str = "cpp", vectorized: bool = False) -> Tuple[str, Optional[str]]:
    r"""
    Translate a Fortran intrinsic call to target language (cpp or python).
    
    Returns:
        (translated_code, required_header_or_module)
    """
    key = name.upper()
    if key not in FORTRAN_INTRINSICS:
        # Fallback to generic function call
        args_str = ", ".join(args)
        return f"{name.lower()}({args_str})", None

    info = FORTRAN_INTRINSICS[key]

    if target.lower() in ("cpp", "c++"):
        tmpl = info["cpp"]
        header = info.get("header_cpp")
        if "{args}" in tmpl:
            res = tmpl.format(args=", ".join(args))
        elif "{0}" in tmpl:
            try:
                res = tmpl.format(*args)
            except IndexError:
                res = f"{key.lower()}({', '.join(args)})"
        else:
            res = tmpl
        return res, header

    elif target.lower() in ("python", "py"):
        tmpl = info["python_vector"] if vectorized else info["python_scalar"]
        if "{args}" in tmpl:
            res = tmpl.format(args=", ".join(args))
        elif "{0}" in tmpl:
            try:
                res = tmpl.format(*args)
            except IndexError:
                res = f"{key.lower()}({', '.join(args)})"
        else:
            res = tmpl
        module = "numpy as np" if ("np." in res or vectorized) else "math"
        return res, module

    raise ValueError(f"Unknown target language: {target}")


if __name__ == "__main__":
    print("[fortran_intrinsics] Testing intrinsic translation:")
    print("DSQRT(X) -> C++:", translate_intrinsic("DSQRT", ["x"], "cpp"))
    print("DSQRT(X) -> Python:", translate_intrinsic("DSQRT", ["x"], "python"))
    print("DATAN2(Y, X) -> C++:", translate_intrinsic("DATAN2", ["y", "x"], "cpp"))
    print("DATAN2(Y, X) -> NumPy:", translate_intrinsic("DATAN2", ["y", "x"], "python", vectorized=True))
    print("SIGN(A, B) -> C++:", translate_intrinsic("SIGN", ["a", "b"], "cpp"))
    print("MAX(A, B, C) -> C++:", translate_intrinsic("MAX", ["a", "b", "c"], "cpp"))
    print("DOT_PRODUCT(U, V) -> C++:", translate_intrinsic("DOT_PRODUCT", ["u", "v"], "cpp"))
    print("DOT_PRODUCT(U, V) -> Python:", translate_intrinsic("DOT_PRODUCT", ["u", "v"], "python", vectorized=True))
