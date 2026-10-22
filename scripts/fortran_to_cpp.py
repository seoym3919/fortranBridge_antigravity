#!/usr/bin/env python3
r"""
fortran_to_cpp.py - Automated Fortran-to-Modern-C++ Code Emitter.

Converts:
- Subroutines & Functions -> C++ free functions or class methods.
- 1-based array indexing -> 0-based indexing (A(I) -> a[i]).
- Column-major nested DO loops -> row-major cache-friendly C++ for loops.
- COMMON blocks -> encapsulated structs / state references.
- Fortran operators (.EQ., .AND., **) -> C++ operators (==, &&, std::pow).
- Intrinsic math calls (DSQRT, DABS, etc.) -> std:: namespace calls.
"""

from __future__ import annotations
import re
from typing import List, Dict, Any, Set, Tuple

from fortran_lexer import FortranStatement, lex_fortran_source
from fortran_intrinsics import translate_intrinsic, FORTRAN_INTRINSICS
from common_block_analyzer import analyze_common_blocks, CommonBlock


def convert_fortran_expr_to_cpp(expr: str, known_arrays: Set[str], loop_vars: Set[str]) -> Tuple[str, Set[str]]:
    r"""
    Translate Fortran scalar/array expressions to C++.
    Pipeline:
      1. Relational & logical operators
      2. Fortran literals (0.5D0, 1.0D-12)
      3. Array subscripts A(I) -> a[i]
      4. Intrinsics DSQRT(x) -> std::sqrt(x)
      5. Exponentiation x**2 -> (x * x)
      6. Identifier normalization
    """
    headers = set()
    e = expr.strip()

    # 1. Fortran logical literals
    e = re.sub(r'\.TRUE\.', 'true', e, flags=re.IGNORECASE)
    e = re.sub(r'\.FALSE\.', 'false', e, flags=re.IGNORECASE)

    # Relational & logical operators
    e = re.sub(r'\.EQ\.', ' == ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.NE\.', ' != ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.LT\.', ' < ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.LE\.', ' <= ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.GT\.', ' > ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.GE\.', ' >= ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.AND\.', ' && ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.OR\.', ' || ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.NOT\.', ' !', e, flags=re.IGNORECASE)

    # 2. Fortran double precision literals: 1.0D-12 -> 1.0e-12, 0.5D0 -> 0.5
    e = re.sub(r'(\d+(?:\.\d*)?)[dD]([+-]?\d+)', r'\1e\2', e)
    e = re.sub(r'(\d+)\.[dD]0', r'\1.0', e)

    # 3. Known Array Subscripting: ARR(I) -> arr[i]
    if known_arrays:
        arr_pat = r'\b(' + '|'.join(re.escape(k) for k in known_arrays) + r')\s*\(([^()]+)\)'
        def arr_repl(m):
            aname = m.group(1).lower()
            raw_indices = [idx.strip() for idx in m.group(2).split(',')]
            out_idx = []
            for idx_str in raw_indices:
                if idx_str.lower() in [lv.lower() for lv in loop_vars]:
                    out_idx.append(f"[{idx_str.lower()}]")
                elif idx_str.isdigit():
                    out_idx.append(f"[{int(idx_str) - 1}]")
                else:
                    out_idx.append(f"[{idx_str.lower()} - 1]")
            return f"{aname}{''.join(out_idx)}"
        e = re.sub(arr_pat, arr_repl, e, flags=re.IGNORECASE)

    # 4. Intrinsic Function Replacements
    for intrinsic_name in sorted(FORTRAN_INTRINSICS.keys(), key=len, reverse=True):
        intrin_pat = r'(?<!std::)\b' + re.escape(intrinsic_name) + r'\s*\(([^()]+)\)'
        def intrin_repl(m):
            args = [a.strip() for a in m.group(1).split(',')]
            cpp_call, req_h = translate_intrinsic(intrinsic_name, args, target="cpp")
            if req_h:
                headers.add(req_h)
            return cpp_call
        e = re.sub(intrin_pat, intrin_repl, e, flags=re.IGNORECASE)

    # 5. Exponentiation: x**2 -> (x * x), x**3 -> (x * x * x), x**y -> std::pow(x, y)
    base_pat = r'(?:[A-Za-z0-9_]+\[[^\]]+\]|[A-Za-z0-9_]+|\([^)]+\))'
    pow_pat = rf'({base_pat})\s*\*\*\s*(\d+|[A-Za-z0-9_]+)'
    def pow_repl(match):
        base = match.group(1).strip()
        exponent = match.group(2).strip()
        if exponent == '2':
            return f"({base} * {base})"
        elif exponent == '3':
            return f"({base} * {base} * {base})"
        else:
            headers.add("<cmath>")
            return f"std::pow({base}, {exponent})"

    e = re.sub(pow_pat, pow_repl, e)

    # 6. Identifier Normalization: Lowercase identifiers except C++ keywords/std
    def id_norm(m):
        w = m.group(0)
        if w.startswith("std::") or w in ("true", "false", "constexpr", "double", "int", "void"):
            return w
        return w.lower()

    e = re.sub(r'\b[A-Za-z_][A-Za-z0-9_]*\b', id_norm, e)

    return e, headers


def translate_fortran_to_cpp(source_text: str) -> str:
    r"""Translate entire Fortran source into modern C++17/20."""
    statements = lex_fortran_source(source_text)
    common_blocks = analyze_common_blocks(statements)

    headers = {"<iostream>", "<vector>", "<array>", "<cmath>", "<algorithm>"}
    cpp_lines = []

    # Emit COMMON blocks as structs
    if common_blocks:
        cpp_lines.append("// ============================================================================")
        cpp_lines.append("// Encapsulated Fortran COMMON Blocks")
        cpp_lines.append("// ============================================================================\n")
        for blk in common_blocks.values():
            cpp_lines.append(blk.emit_cpp_struct())
            cpp_lines.append("")

    # State tracking
    in_subroutine = False
    subroutine_name = "kernel"
    params: List[str] = []
    known_arrays: Set[str] = set()
    local_arrays: Set[str] = set()
    active_loop_vars: Set[str] = set()
    do_stack: List[str] = []
    indent_level = 0

    def ind() -> str:
        return "    " * indent_level

    # Pass 1: Gather parameter names and array dimensions
    for s in statements:
        st = s.stmt_type
        if st in ("SUBROUTINE", "FUNCTION"):
            subroutine_name = s.details["name"].lower()
            params = [p.lower() for p in s.details["params"]]
        elif st == "DIMENSION":
            raw_dim = s.details["raw"]
            for item in re.split(r',\s*(?![^()]*\))', raw_dim):
                m_dim = re.match(r'^([A-Za-z0-9_]+)\s*\(([^)]+)\)', item.strip())
                if m_dim:
                    aname = m_dim.group(1).lower()
                    known_arrays.add(aname)
                    if aname not in params:
                        local_arrays.add(aname)
        elif st == "TYPE_DECL":
            decl_str = s.details["declarations"]
            for item in re.split(r',\s*(?![^()]*\))', decl_str):
                m_dim = re.match(r'^([A-Za-z0-9_]+)\s*\(([^)]+)\)', item.strip())
                if m_dim:
                    aname = m_dim.group(1).lower()
                    known_arrays.add(aname)
                    if aname not in params:
                        local_arrays.add(aname)

    # Pass 2: Code emission
    body_lines = []
    indent_level = 1

    for s in statements:
        st = s.stmt_type

        if st in ("SUBROUTINE", "FUNCTION"):
            sig_args = []
            for p in params:
                if p in known_arrays:
                    sig_args.append(f"std::vector<double>& {p}")
                else:
                    sig_args.append(f"double& {p}")

            for blk in common_blocks.values():
                sig_args.append(f"{blk.struct_name}& {blk.name.lower()}_state")

            ret_type = "double" if st == "FUNCTION" else "void"
            body_lines.append(f"{ret_type} {subroutine_name}({', '.join(sig_args)}) {{")

            # Declare local arrays
            for la in sorted(local_arrays):
                body_lines.append(f"{ind()}std::vector<double> {la}(3, 0.0);")

        elif st == "PARAMETER":
            for k, v in s.details["constants"].items():
                v_cpp, h = convert_fortran_expr_to_cpp(v, known_arrays, active_loop_vars)
                headers.update(h)
                body_lines.append(f"{ind()}constexpr double {k.lower()} = {v_cpp};")

        elif st == "DO":
            var = s.details["var"].lower()
            start = s.details["start"]
            end = s.details["end"]
            step = s.details["step"]

            active_loop_vars.add(var)
            do_stack.append(var)

            if start.strip() == "1" and step.strip() == "1":
                body_lines.append(f"{ind()}for (int {var} = 0; {var} < {end.lower()}; ++{var}) {{")
            else:
                body_lines.append(f"{ind()}for (int {var} = {start}; {var} <= {end}; {var} += {step}) {{")
            indent_level += 1

        elif st in ("END_DO", "CONTINUE"):
            if do_stack:
                closed_var = do_stack.pop()
                if closed_var in active_loop_vars:
                    active_loop_vars.remove(closed_var)
                indent_level = max(1, indent_level - 1)
                body_lines.append(f"{ind()}}}")

        elif st == "IF_THEN":
            cond_cpp, h = convert_fortran_expr_to_cpp(s.details["condition"], known_arrays, active_loop_vars)
            headers.update(h)
            body_lines.append(f"{ind()}if ({cond_cpp}) {{")
            indent_level += 1

        elif st == "ELSE":
            indent_level = max(1, indent_level - 1)
            body_lines.append(f"{ind()}}} else {{")
            indent_level += 1

        elif st == "ELSE_IF":
            indent_level = max(1, indent_level - 1)
            cond_cpp, h = convert_fortran_expr_to_cpp(s.details["condition"], known_arrays, active_loop_vars)
            headers.update(h)
            body_lines.append(f"{ind()}}} else if ({cond_cpp}) {{")
            indent_level += 1

        elif st == "END_IF":
            indent_level = max(1, indent_level - 1)
            body_lines.append(f"{ind()}}}")

        elif st == "IF_STMT":
            cond_cpp, h = convert_fortran_expr_to_cpp(s.details["condition"], known_arrays, active_loop_vars)
            headers.update(h)
            inner = s.details["statement"].strip()
            if inner.upper().startswith("RETURN"):
                body_lines.append(f"{ind()}if ({cond_cpp}) return;")
            elif inner.upper().startswith("GO TO") or inner.upper().startswith("GOTO"):
                lbl = re.search(r'\d+', inner).group(0)
                body_lines.append(f"{ind()}if ({cond_cpp}) goto label_{lbl};")
            else:
                inner_cpp, h2 = convert_fortran_expr_to_cpp(inner, known_arrays, active_loop_vars)
                headers.update(h2)
                body_lines.append(f"{ind()}if ({cond_cpp}) {{ {inner_cpp}; }}")

        elif st == "ASSIGNMENT":
            lhs = s.details["lhs"]
            rhs = s.details["rhs"]

            lhs_cpp, h1 = convert_fortran_expr_to_cpp(lhs, known_arrays, active_loop_vars)
            rhs_cpp, h2 = convert_fortran_expr_to_cpp(rhs, known_arrays, active_loop_vars)
            headers.update(h1)
            headers.update(h2)

            # Check if COMMON variable
            common_var_match = None
            for b in common_blocks.values():
                for v in b.variables:
                    if v.name.lower() == lhs_cpp.lower():
                        common_var_match = f"{b.name.lower()}_state.{v.name.lower()}"
                        break
                if common_var_match:
                    break

            if common_var_match:
                body_lines.append(f"{ind()}{common_var_match} = {rhs_cpp};")
            else:
                is_local = (lhs_cpp.lower() not in params) and ("[" not in lhs_cpp)
                decl_prefix = "double " if is_local else ""
                body_lines.append(f"{ind()}{decl_prefix}{lhs_cpp} = {rhs_cpp};")

        elif st == "GOTO":
            lbl = s.details["target_label"]
            body_lines.append(f"{ind()}goto label_{lbl};")

        elif st == "RETURN":
            body_lines.append(f"{ind()}return;")

        elif st == "IO":
            raw_io = s.details["statement"]
            m_print = re.match(r'^PRINT\s*\*\s*,\s*(.*)$', raw_io, re.IGNORECASE)
            if m_print:
                items = [convert_fortran_expr_to_cpp(i.strip(), known_arrays, active_loop_vars)[0] for i in m_print.group(1).split(',')]
                body_lines.append(f"{ind()}std::cout << {' << \" \" << '.join(items)} << std::endl;")

        elif st == "END":
            body_lines.append("}\n")
            in_subroutine = False

        if s.label:
            body_lines.append(f"label_{s.label}:")

    # Assemble
    res = []
    res.append("// ============================================================================")
    res.append("// Translated from FORTRAN to Modern C++17/20 by fortranBridge")
    res.append("// ============================================================================\n")
    for h in sorted(headers):
        res.append(f"#include {h}")
    res.append("\n")
    res.extend(cpp_lines)
    res.extend(body_lines)

    return "\n".join(res)
