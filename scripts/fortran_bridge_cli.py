#!/usr/bin/env python3
r"""
fortran_bridge_cli.py - Master CLI for Fortran-to-C++ and Fortran-to-Python Translation.
Codebase-Wide & Directory-First Multi-File Modernization Engine.

Usage:
    # 1. Modernize entire Fortran codebase directory:
    python3 scripts/fortran_bridge_cli.py /path/to/fortran_repo --target cpp
    python3 scripts/fortran_bridge_cli.py /path/to/fortran_repo --target python --out-dir modernized_py

    # 2. Modernize a single Fortran file:
    python3 scripts/fortran_bridge_cli.py input.f --target cpp --out output.cpp
    python3 scripts/fortran_bridge_cli.py input.f90 --target python --out output.py
"""

from __future__ import annotations
import ast
import os
import re
import sys
import argparse
from pathlib import Path
from typing import Dict, List, Any, Optional

from fortran_lexer import lex_fortran_source, is_fixed_format
from common_block_analyzer import analyze_common_blocks
from fortran_to_cpp import translate_fortran_to_cpp
from fortran_to_python import translate_fortran_to_python
from project_linker import ProjectGraph, FortranFileInfo
import translation_verifier


SUPPORTED_FORTRAN_EXTS = {".f", ".f77", ".for", ".f90", ".f95", ".f03", ".f08"}
IGNORED_DIRS = {".git", ".svn", "__pycache__", ".pytest_cache", "build", "dist", ".venv", "venv"}


def check_translation(code: str, lang: str) -> Dict[str, Any]:
    r"""Sanity-check generated code. Returns {'error': str|None, 'untranslated': int}.

    Python output must parse; C++ output must have balanced braces/parentheses (no compiler is
    invoked). `untranslated` counts constructs the translator refused rather than guessed at.
    """
    if lang == "python":
        marker = len(re.findall(r"NotImplementedError\('fortranBridge", code)) + code.count("_fortran_untranslated(")
        try:
            ast.parse(code)
            return {"error": None, "untranslated": marker}
        except SyntaxError as exc:
            return {"error": f"line {exc.lineno}: {exc.msg}", "untranslated": marker}
    marker = code.count('logic_error("fortranBridge') + code.count("fortran_untranslated(")
    stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
    for open_c, close_c in (("{", "}"), ("(", ")"), ("[", "]")):
        if stripped.count(open_c) != stripped.count(close_c):
            return {"error": f"unbalanced '{open_c}{close_c}'", "untranslated": marker}
    return {"error": None, "untranslated": marker}


def report_check(name: str, result: Dict[str, Any]) -> int:
    r"""Print the outcome of check_translation; returns 1 if the output is syntactically invalid."""
    if result["error"]:
        print(f"WARNING: generated code for {name} is not valid: {result['error']}", file=sys.stderr)
        return 1
    if result["untranslated"]:
        print(f"NOTE: {name}: {result['untranslated']} construct(s) were not translated and fail loudly at "
              f"runtime (search for 'fortranBridge:' in the output).", file=sys.stderr)
    return 0


def generate_single_file_report(filepath: str, source_text: str) -> str:
    r"""Generate Markdown translation summary and audit brief for a single file."""
    stmts = lex_fortran_source(source_text)
    common_blocks = analyze_common_blocks(stmts)
    fixed = is_fixed_format(source_text)

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
        "DO": "Counted loop; Fortran loop-variable value preserved, subscripts shifted to 0-based (C++ `for`, Python `range`)",
        "COMMON": "Encapsulated into typed C++ struct or Python @dataclass state object",
        "PARAMETER": "Converted to C++ `constexpr` or Python module-level constant",
        "IF_THEN": "Translated to structured `if (...) { ... }` block",
        "ARITHMETIC_IF": "C++: `goto` ladder. Python: NOT translated (raises NotImplementedError; use restructure-goto)",
        "COMPUTED_GOTO": "C++: `switch` + `goto`. Python: NOT translated (raises NotImplementedError)",
        "GOTO": "C++: `goto`. Python: NOT translated (raises NotImplementedError; use restructure-goto)",
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
    report.append("5. **Untranslated Constructs**: Statements outside the supported subset (GO TO in Python, DATA, EQUIVALENCE, formatted I/O, array sections in C++) are emitted as `raise NotImplementedError` / `throw std::logic_error`; resolve each before use.\n")

    return "\n".join(report)


def modernize_fortran_codebase(
    target_dir: str | Path,
    target_lang: str = "cpp",
    out_dir: Optional[str | Path] = None,
    report_path: Optional[str | Path] = None
) -> Dict[str, Any]:
    r"""
    Modernize an entire Fortran codebase directory:
    - Recursively discovers all Fortran sources
    - Computes topological compile/translation order via ProjectGraph
    - Translates each module into modern C++ or Python
    - Encapsulates COMMON blocks across modules
    - Generates CMakeLists.txt (C++) or __init__.py (Python)
    - Emits a comprehensive CODEBASE_TRANSLATION_REPORT.md
    """
    root = Path(target_dir).resolve()
    print("================================================================================")
    print(f" [fortranBridge] Scanning Fortran Codebase Directory: {root}")
    print("================================================================================")

    # 1. Build Project Graph and Topological Order
    graph = ProjectGraph(str(root))
    if not graph.files:
        print("No Fortran source files (.f, .f90, .for, etc.) found in directory.")
        return {"files": 0}

    compile_order = graph.topological_sort()
    print(f"Discovered Fortran source files: {len(graph.files)}")
    print(f"Topological dependency translation order:")
    for idx, fname in enumerate(compile_order, 1):
        print(f"  {idx}. {fname}")

    # Output paths
    destination = Path(out_dir) if out_dir else (root / f"modernized_{target_lang}")
    destination.mkdir(parents=True, exist_ok=True)

    translated_files: List[Tuple[str, Path]] = []
    invalid = 0
    total_statements = 0
    all_common_blocks: Dict[str, Any] = {}

    for fname in compile_order:
        info = graph.files[fname]
        src_path = Path(info.filepath)
        text = src_path.read_text(encoding="utf-8", errors="replace")

        stmts = lex_fortran_source(text)
        total_statements += len(stmts)
        c_blocks = analyze_common_blocks(stmts)
        all_common_blocks.update(c_blocks)

        stem = src_path.stem

        if target_lang in ("cpp", "both"):
            cpp_code = translate_fortran_to_cpp(text)
            out_cpp = destination / f"{stem}.cpp"
            out_cpp.write_text(cpp_code, encoding="utf-8")
            translated_files.append((fname, out_cpp))
            invalid += report_check(out_cpp.name, check_translation(cpp_code, "cpp"))

        if target_lang in ("python", "both"):
            py_code = translate_fortran_to_python(text)
            out_py = destination / f"{stem}.py"
            out_py.write_text(py_code, encoding="utf-8")
            translated_files.append((fname, out_py))
            invalid += report_check(out_py.name, check_translation(py_code, "python"))

    # Multi-file packaging
    if target_lang in ("cpp", "both"):
        cmake_content = graph.generate_cmakelists(project_name=root.name)
        cmake_path = destination / "CMakeLists.txt"
        cmake_path.write_text(cmake_content, encoding="utf-8")
        print(f"Generated build system: {cmake_path}")

    if target_lang in ("python", "both"):
        init_lines = [
            f"# Auto-generated package for {root.name} by fortranBridge",
            "from __future__ import annotations",
            ""
        ]
        for fname in compile_order:
            stem = Path(fname).stem
            init_lines.append(f"from .{stem} import *")
        init_lines.append("\n__all__ = [")
        for fname in compile_order:
            stem = Path(fname).stem
            init_lines.append(f'    "{stem}",')
        init_lines.append("]\n")

        init_path = destination / "__init__.py"
        init_path.write_text("\n".join(init_lines), encoding="utf-8")
        print(f"Generated Python package descriptor: {init_path}")

    # Generate Codebase Translation Report
    rep_lines = [
        f"# Codebase Fortran Translation Report: `{root.name}`",
        "",
        "## 1. Executive Summary",
        f"- **Codebase Root**: `{root}`",
        f"- **Fortran Source Files Modernized**: {len(graph.files)}",
        f"- **Total Logical Statements Transpiled**: {total_statements}",
        f"- **Target Language**: {target_lang.upper()}",
        f"- **Destination Directory**: `{destination}`",
        f"- **Encapsulated COMMON Blocks**: {len(all_common_blocks)}",
        "",
        "## 2. Topological Dependency Build Sequence",
        "Files were translated in topological order resolving all `MODULE`, `USE`, and `CALL` dependencies:",
        ""
    ]
    for idx, fname in enumerate(compile_order, 1):
        rep_lines.append(f"{idx}. `{fname}`")

    if all_common_blocks:
        rep_lines.extend([
            "",
            "## 3. Global COMMON Block Encapsulation",
            "Legacy global `COMMON` blocks were de-aliased into structured state containers:",
            "",
            "| Block Name | Struct / Class Name | Encapsulated Members |",
            "|---|---|---|"
        ])
        for b_name, b_obj in all_common_blocks.items():
            members_str = ", ".join(f"`{v.name}` ({v.f_type})" for v in b_obj.variables)
            rep_lines.append(f"| `/{b_name}/` | `{b_obj.struct_name}` | {members_str} |")

    rep_lines.extend([
        "",
        "## 4. Verification and Numerical Equivalence",
        "> [!IMPORTANT]",
        "> Run the closed-loop verification engine to assert relative $L_2$ error $< 10^{-12}$:",
        "> ```bash",
        "> python3 scripts/translation_verifier.py --self-test",
        "> ```"
    ])

    full_report = "\n".join(rep_lines)
    rep_out = Path(report_path) if report_path else (root / "CODEBASE_TRANSLATION_REPORT.md")
    rep_out.write_text(full_report, encoding="utf-8")
    print(f"Codebase translation report written to: {rep_out}")
    print("================================================================================")

    return {
        "files": len(graph.files),
        "statements": total_statements,
        "destination": destination,
        "report": rep_out,
        "invalid_outputs": invalid,
    }


def main():
    parser = argparse.ArgumentParser(
        description="fortranBridge CLI - Translate legacy Fortran to modern C++ and Python (Codebase-Wide & Directory-First)."
    )
    parser.add_argument("target", nargs="?", default=".", help="Path to Fortran codebase directory (default: '.') or source file (.f, .f90, etc.)")
    parser.add_argument("--target", dest="target_lang", choices=["cpp", "python", "both"], default="cpp", help="Target language (default: cpp)")
    parser.add_argument("--out", help="Output file path (for single file translation)")
    parser.add_argument("--out-dir", help="Output directory path (for codebase directory translation)")
    parser.add_argument("--report", help="Save translation audit report markdown")
    parser.add_argument("--self-test", action="store_true", help="Run self-test suite")

    args = parser.parse_args()

    if args.self_test:
        success = translation_verifier.run_self_tests()
        sys.exit(0 if success else 1)

    target_path = Path(args.target)
    if not target_path.exists():
        print(f"Error: Target path '{args.target}' not found.", file=sys.stderr)
        sys.exit(1)

    # DIRECTORY / CODEBASE MODE (PRIMARY MODE)
    if target_path.is_dir():
        summary = modernize_fortran_codebase(
            target_path,
            target_lang=args.target_lang,
            out_dir=args.out_dir,
            report_path=args.report
        )
        sys.exit(3 if summary.get("invalid_outputs") else 0)

    # SINGLE FILE MODE (FALLBACK)
    with open(target_path, "r", encoding="utf-8", errors="replace") as f:
        source_text = f.read()

    base_name = target_path.stem
    invalid = 0

    if args.target_lang in ("cpp", "both"):
        cpp_code = translate_fortran_to_cpp(source_text)
        cpp_out = args.out if (args.out and args.target_lang == "cpp") else f"{base_name}.cpp"
        with open(cpp_out, "w", encoding="utf-8") as f:
            f.write(cpp_code)
        print(f"Translated C++ code saved to: {cpp_out}")
        invalid += report_check(cpp_out, check_translation(cpp_code, "cpp"))

    if args.target_lang in ("python", "both"):
        py_code = translate_fortran_to_python(source_text)
        py_out = args.out if (args.out and args.target_lang == "python") else f"{base_name}.py"
        with open(py_out, "w", encoding="utf-8") as f:
            f.write(py_code)
        print(f"Translated Python code saved to: {py_out}")
        invalid += report_check(py_out, check_translation(py_code, "python"))

    if args.report:
        report_md = generate_single_file_report(str(target_path), source_text)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report_md)
        print(f"Translation audit report saved to: {args.report}")

    sys.exit(3 if invalid else 0)


if __name__ == "__main__":
    main()
