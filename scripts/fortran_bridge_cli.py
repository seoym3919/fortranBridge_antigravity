#!/usr/bin/env python3
r"""
fortran_bridge_cli.py - Master CLI for Fortran-to-C++ and Fortran-to-Python Translation.

Usage:
    python3 scripts/fortran_bridge_cli.py input.f --target cpp --out output.cpp
    python3 scripts/fortran_bridge_cli.py input.f90 --target python --out output.py
    python3 scripts/fortran_bridge_cli.py input.f --target both
"""

from __future__ import annotations
import os
import sys
import argparse
from typing import Dict, Any

from fortran_lexer import lex_fortran_source, is_fixed_format
from common_block_analyzer import analyze_common_blocks
from fortran_to_cpp import translate_fortran_to_cpp
from fortran_to_python import translate_fortran_to_python


def generate_translation_report(filepath: str, source_text: str) -> str:
    r"""Generate Markdown translation summary and audit brief."""
    stmts = lex_fortran_source(source_text)
    common_blocks = analyze_common_blocks(stmts)
    fixed = is_fixed_format(source_text)

    # Count statement types
    type_counts: Dict[str, int] = {}
    for s in stmts:
        type_counts[s.stmt_type] = type_counts.get(s.stmt_type, 0) + 1

    report = []
    report.append(f"# Fortran Translation Audit Report: `{os.path.basename(filepath)}`\n")
    report.append(f"**Source Format**: {'Fixed-Format (FORTRAN 66/77)' if fixed else 'Free-Format (Fortran 90+)'}  ")
    report.append(f"**Total Logical Statements**: {len(stmts)}  ")
    report.append(f"**COMMON Blocks Encapsulated**: {len(common_blocks)}\n")

    report.append("## 1. Statement Classification Inventory\n")
    report.append("| Statement Type | Count | Translation Strategy |")
    report.append("|---|---|---|")
    strategies = {
        "SUBROUTINE": "Translated to free function or encapsulated class method",
        "FUNCTION": "Translated to typed function returning scalar/tensor",
        "DO": "Converted to 0-based for loop (C++: `for (int i=0; i<N; ++i)`, Python: `range(N)`)",
        "COMMON": "Encapsulated into typed C++ struct or Python @dataclass state object",
        "PARAMETER": "Converted to C++ `constexpr` or Python module-level constant",
        "IF_THEN": "Translated to structured `if (...) { ... }` block",
        "ARITHMETIC_IF": "Restructured into 3-way branching `if-else if-else` ladder",
        "COMPUTED_GOTO": "Restructured into `switch` / `match` statement",
        "GOTO": "Converted to structured loop exit or explicit labeled jump",
        "ASSIGNMENT": "Translated with 0-based indexing and intrinsic conversions",
    }
    for st, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
        strat = strategies.get(st, "Direct semantic translation")
        report.append(f"| `{st}` | {count} | {strat} |")
    report.append("")

    if common_blocks:
        report.append("## 2. COMMON Block State Encapsulations\n")
        report.append("| Block Name | Struct / Class Name | Encapsulated Members |")
        report.append("|---|---|---|")
        for b_name, b_obj in common_blocks.items():
            members_str = ", ".join(f"`{v.name}` ({v.f_type})" for v in b_obj.variables)
            report.append(f"| `/{b_name}/` | `{b_obj.struct_name}` | {members_str} |")
        report.append("")

    report.append("## 3. Recommended Modernization Best Practices\n")
    report.append("1. **Verify Index Offsets**: Ensure all 1-based Fortran arrays are accessed with 0-based indices in C++ / Python.")
    report.append("2. **Loop Memory Contiguity**: In C++, outer loops should index row coordinates and inner loops column coordinates for cache efficiency.")
    report.append("3. **Pass-by-Reference Constness**: Mark non-mutated parameters as `const` in C++ signatures.")
    report.append("4. **Equivalence Testing**: Execute `scripts/translation_verifier.py` to assert numerical output equivalence.")

    return "\n".join(report)


def main():
    parser = argparse.ArgumentParser(
        description="fortranBridge CLI - Translate legacy Fortran to modern C++ and Python."
    )
    parser.add_argument("file", help="Path to Fortran source file (.f, .f77, .f90, etc.)")
    parser.add_argument("--target", choices=["cpp", "python", "both"], default="cpp", help="Target language (default: cpp)")
    parser.add_argument("--out", help="Output file path (for single target)")
    parser.add_argument("--report", help="Save translation audit report markdown")

    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"Error: File '{args.file}' not found.")
        sys.exit(1)

    with open(args.file, "r", encoding="utf-8", errors="replace") as f:
        source_text = f.read()

    base_name, _ = os.path.splitext(args.file)

    if args.target in ("cpp", "both"):
        cpp_code = translate_fortran_to_cpp(source_text)
        cpp_out = args.out if (args.out and args.target == "cpp") else f"{base_name}.cpp"
        with open(cpp_out, "w", encoding="utf-8") as f:
            f.write(cpp_code)
        print(f"[fortranBridge] C++ code generated -> {cpp_out}")

    if args.target in ("python", "both"):
        py_code = translate_fortran_to_python(source_text)
        py_out = args.out if (args.out and args.target == "python") else f"{base_name}.py"
        with open(py_out, "w", encoding="utf-8") as f:
            f.write(py_code)
        print(f"[fortranBridge] Python code generated -> {py_out}")

    if args.report:
        report_md = generate_translation_report(args.file, source_text)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"[fortranBridge] Audit report generated -> {args.report}")


if __name__ == "__main__":
    main()
