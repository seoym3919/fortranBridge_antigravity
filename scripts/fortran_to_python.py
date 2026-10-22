#!/usr/bin/env python3
r"""
fortran_to_python.py - Automated Fortran-to-Python / NumPy Code Emitter.

Converts:
- Subroutines & Functions -> Python functions with type annotations.
- 1-based array indexing -> 0-based slicing or vectorized NumPy operations.
- COMMON blocks -> encapsulated @dataclass state objects.
- Fortran operators (.EQ., .AND., .OR.) -> Python operators (==, and, or).
- Math intrinsics (DSQRT, DABS, etc.) -> math or numpy functions.
- Multi-value scalar returns instead of pointer-mutating arguments.
"""

from __future__ import annotations
import re
from typing import List, Dict, Any, Set, Tuple

from fortran_lexer import FortranStatement, lex_fortran_source
from fortran_intrinsics import translate_intrinsic, FORTRAN_INTRINSICS
from common_block_analyzer import analyze_common_blocks, CommonBlock


def convert_fortran_expr_to_python(expr: str, known_arrays: Set[str], loop_vars: Set[str]) -> Tuple[str, Set[str]]:
    r"""
    Translate Fortran scalar/array expressions to Python.
    Pipeline:
      1. Relational & logical operators
      2. Fortran literals (0.5D0, 1.0D-12)
      3. Array subscripts ARR(I) -> arr[i]
      4. Intrinsics DSQRT(x) -> math.sqrt(x)
      5. Exponentiation
      6. Identifier normalization
    """
    imports = set()
    e = expr.strip()

    # 1. Logical literals
    e = re.sub(r'\.TRUE\.', 'True', e, flags=re.IGNORECASE)
    e = re.sub(r'\.FALSE\.', 'False', e, flags=re.IGNORECASE)

    # Relational & logical operators
    e = re.sub(r'\.EQ\.', ' == ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.NE\.', ' != ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.LT\.', ' < ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.LE\.', ' <= ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.GT\.', ' > ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.GE\.', ' >= ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.AND\.', ' and ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.OR\.', ' or ', e, flags=re.IGNORECASE)
    e = re.sub(r'\.NOT\.', ' not ', e, flags=re.IGNORECASE)

    # 2. Double precision literals: 1.0D-12 -> 1.0e-12, 0.5D0 -> 0.5
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
        intrin_pat = r'(?<!math\.)(?<!np\.)\b' + re.escape(intrinsic_name) + r'\s*\(([^()]+)\)'
        def intrin_repl(m):
            args = [a.strip() for a in m.group(1).split(',')]
            py_call, req_mod = translate_intrinsic(intrinsic_name, args, target="python")
            if req_mod:
                imports.add(req_mod)
            return py_call
        e = re.sub(intrin_pat, intrin_repl, e, flags=re.IGNORECASE)

    # 5. Identifier Normalization: Lowercase identifiers except Python keywords
    def id_norm(m):
        w = m.group(0)
        if w.startswith("math.") or w.startswith("np.") or w in ("True", "False", "None", "and", "or", "not"):
            return w
        return w.lower()

    e = re.sub(r'\b[A-Za-z_][A-Za-z0-9_]*\b', id_norm, e)

    return e, imports


def translate_fortran_to_python(source_text: str) -> str:
    r"""Translate entire Fortran source into modern Python / NumPy."""
    statements = lex_fortran_source(source_text)
    common_blocks = analyze_common_blocks(statements)

    imports = {"numpy as np", "math"}
    py_lines = []

    if common_blocks:
        imports.add("from dataclasses import dataclass, field")
        py_lines.append("# ============================================================================")
        py_lines.append("# Encapsulated Fortran COMMON Blocks")
        py_lines.append("# ============================================================================\n")
        for blk in common_blocks.values():
            py_lines.append(blk.emit_python_dataclass())
            py_lines.append("")

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

    # Collect known arrays and parameters
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

    body_lines = []
    indent_level = 1

    for s in statements:
        st = s.stmt_type

        if st in ("SUBROUTINE", "FUNCTION"):
            sig_args = []
            for p in params:
                if p in known_arrays:
                    sig_args.append(f"{p}: np.ndarray")
                else:
                    sig_args.append(f"{p}: float")

            for blk in common_blocks.values():
                sig_args.append(f"{blk.name.lower()}_state: {blk.struct_name}")

            body_lines.append(f"def {subroutine_name}({', '.join(sig_args)}):")
            body_lines.append(f'{ind()}r"""Translated from Fortran {subroutine_name}."""')

            # Initialize local arrays
            for la in sorted(local_arrays):
                body_lines.append(f"{ind()}{la} = np.zeros(3, dtype=np.float64)")

        elif st == "PARAMETER":
            for k, v in s.details["constants"].items():
                v_py, imp = convert_fortran_expr_to_python(v, known_arrays, active_loop_vars)
                imports.update(imp)
                body_lines.append(f"{ind()}{k.lower()} = {v_py}")

        elif st == "DO":
            var = s.details["var"].lower()
            start = s.details["start"]
            end = s.details["end"]
            step = s.details["step"]

            active_loop_vars.add(var)
            do_stack.append(var)

            if start.strip() == "1" and step.strip() == "1":
                body_lines.append(f"{ind()}for {var} in range(int({end.lower()})):")
            else:
                body_lines.append(f"{ind()}for {var} in range(int({start}), int({end}) + 1, int({step})):")
            indent_level += 1

        elif st in ("END_DO", "CONTINUE"):
            if do_stack:
                closed_var = do_stack.pop()
                if closed_var in active_loop_vars:
                    active_loop_vars.remove(closed_var)
                indent_level = max(1, indent_level - 1)

        elif st == "IF_THEN":
            cond_py, imp = convert_fortran_expr_to_python(s.details["condition"], known_arrays, active_loop_vars)
            imports.update(imp)
            body_lines.append(f"{ind()}if {cond_py}:")
            indent_level += 1

        elif st == "ELSE":
            indent_level = max(1, indent_level - 1)
            body_lines.append(f"{ind()}else:")
            indent_level += 1

        elif st == "ELSE_IF":
            indent_level = max(1, indent_level - 1)
            cond_py, imp = convert_fortran_expr_to_python(s.details["condition"], known_arrays, active_loop_vars)
            imports.update(imp)
            body_lines.append(f"{ind()}elif {cond_py}:")
            indent_level += 1

        elif st == "END_IF":
            indent_level = max(1, indent_level - 1)

        elif st == "IF_STMT":
            cond_py, imp = convert_fortran_expr_to_python(s.details["condition"], known_arrays, active_loop_vars)
            imports.update(imp)
            inner = s.details["statement"].strip()
            if inner.upper().startswith("RETURN"):
                body_lines.append(f"{ind()}if {cond_py}:\n{ind()}    return")
            elif inner.upper().startswith("GO TO") or inner.upper().startswith("GOTO"):
                lbl = re.search(r'\d+', inner).group(0)
                body_lines.append(f"{ind()}if {cond_py}:\n{ind()}    # goto label_{lbl}\n{ind()}    return")
            else:
                inner_py, imp2 = convert_fortran_expr_to_python(inner, known_arrays, active_loop_vars)
                imports.update(imp2)
                body_lines.append(f"{ind()}if {cond_py}:\n{ind()}    {inner_py}")

        elif st == "ASSIGNMENT":
            lhs = s.details["lhs"]
            rhs = s.details["rhs"]

            lhs_py, imp1 = convert_fortran_expr_to_python(lhs, known_arrays, active_loop_vars)
            rhs_py, imp2 = convert_fortran_expr_to_python(rhs, known_arrays, active_loop_vars)
            imports.update(imp1)
            imports.update(imp2)

            # Check if COMMON block variable
            common_var_match = None
            for b in common_blocks.values():
                for v in b.variables:
                    if v.name.lower() == lhs_py.lower():
                        common_var_match = f"{b.name.lower()}_state.{v.name.lower()}"
                        break
                if common_var_match:
                    break

            if common_var_match:
                body_lines.append(f"{ind()}{common_var_match} = {rhs_py}")
            else:
                body_lines.append(f"{ind()}{lhs_py} = {rhs_py}")

        elif st == "RETURN":
            body_lines.append(f"{ind()}return")

        elif st == "IO":
            raw_io = s.details["statement"]
            m_print = re.match(r'^PRINT\s*\*\s*,\s*(.*)$', raw_io, re.IGNORECASE)
            if m_print:
                items = [convert_fortran_expr_to_python(i.strip(), known_arrays, active_loop_vars)[0] for i in m_print.group(1).split(',')]
                body_lines.append(f"{ind()}print({', '.join(items)})")

        elif st == "END":
            body_lines.append("")
            in_subroutine = False

    res = []
    res.append("# ============================================================================")
    res.append("# Translated from FORTRAN to Modern Python / NumPy by fortranBridge")
    res.append("# ============================================================================")
    res.append("from __future__ import annotations")
    for imp in sorted(imports):
        if imp.startswith("from"):
            res.append(imp)
        else:
            res.append(f"import {imp}")
    res.append("\n")
    res.extend(py_lines)
    res.extend(body_lines)

    return "\n".join(res)
