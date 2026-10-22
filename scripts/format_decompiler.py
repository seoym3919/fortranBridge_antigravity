#!/usr/bin/env python3
r"""
format_decompiler.py - Decompiles Fortran FORMAT Edit Descriptors to C++20 std::format and Python f-Strings.

Decompiles descriptors:
- Iw, Iw.m -> {:wd}
- Fw.d -> {:w.df}
- Ew.d, ESw.d -> {:w.de}
- Gw.d -> {:w.dg}
- Aw, A -> {:s}
- nX -> ' ' * n
- 'Literal' -> literal text
- / -> newline \n
- Repetition (e.g., 3F10.5 -> F10.5, F10.5, F10.5)
"""

from __future__ import annotations
import re
from typing import List, Dict, Any, Tuple


def expand_repetitions(fmt_body: str) -> str:
    r"""Expand repeat prefixes like 3F10.5 into F10.5, F10.5, F10.5."""
    # Match digit followed by descriptor (I, F, E, G, A, X, etc.)
    def repl(m):
        count = int(m.group(1))
        desc = m.group(2)
        return ", ".join([desc] * count)

    # e.g., 3(1X, F8.2) or 3F10.5
    res = re.sub(r'(\d+)\s*([A-Za-z][A-Za-z0-9_.]*)', repl, fmt_body)
    return res


def decompile_format_descriptors(fmt_text: str) -> Dict[str, Any]:
    r"""
    Parse a Fortran FORMAT string:
        FORMAT(1X, 'ENERGY = ', 1P, E15.7, 2X, 'STEP = ', I6)
    Returns:
        {
            "cpp_format_string": " ENERGY = {:15.7e}  STEP = {:6d}\n",
            "python_format_template": " ENERGY = {var1:15.7e}  STEP = {var2:6d}",
            "argument_types": ["double", "int"]
        }
    """
    m = re.match(r'^(?:(?:\d+)\s+)?FORMAT\s*\((.*)\)$', fmt_text.strip(), re.IGNORECASE)
    body = m.group(1).strip() if m else fmt_text.strip()

    expanded = expand_repetitions(body)

    # Tokenize: literals '...' or descriptors separated by commas
    tokens = []
    current = []
    in_quote = False
    quote_char = ''

    for ch in expanded:
        if ch in ("'", '"'):
            if not in_quote:
                in_quote = True
                quote_char = ch
                current.append(ch)
            elif ch == quote_char:
                in_quote = False
                current.append(ch)
            else:
                current.append(ch)
        elif ch == ',' and not in_quote:
            tok = ''.join(current).strip()
            if tok:
                tokens.append(tok)
            current = []
        elif ch == '/' and not in_quote:
            tok = ''.join(current).strip()
            if tok:
                tokens.append(tok)
            tokens.append('/')
            current = []
        else:
            current.append(ch)

    if current:
        tok = ''.join(current).strip()
        if tok:
            tokens.append(tok)

    cpp_parts = []
    py_parts = []
    arg_types = []
    arg_idx = 0

    for tok in tokens:
        t = tok.strip()

        # String literal 'text' or "text"
        if (t.startswith("'") and t.endswith("'")) or (t.startswith('"') and t.endswith('"')):
            lit = t[1:-1]
            cpp_parts.append(lit)
            py_parts.append(lit)

        # Newline /
        elif t == '/':
            cpp_parts.append("\\n")
            py_parts.append("\\n")

        # nX spacing
        elif re.match(r'^(\d+)X$', t, re.IGNORECASE):
            m_x = re.match(r'^(\d+)X$', t, re.IGNORECASE)
            n_spaces = int(m_x.group(1))
            cpp_parts.append(" " * n_spaces)
            py_parts.append(" " * n_spaces)

        elif t.upper() in ("1X", "X"):
            cpp_parts.append(" ")
            py_parts.append(" ")

        # 1P scale factor (Fortran convention for scientific notation, no direct print output)
        elif re.match(r'^\d*P$', t, re.IGNORECASE):
            continue

        # Integer: Iw or Iw.m
        elif re.match(r'^I(\d+)(?:\.(\d+))?$', t, re.IGNORECASE):
            m_i = re.match(r'^I(\d+)', t, re.IGNORECASE)
            w = m_i.group(1)
            cpp_parts.append(f"{{:{w}d}}")
            py_parts.append(f"{{arg{arg_idx}:{w}d}}")
            arg_types.append("int")
            arg_idx += 1

        # Float: Fw.d
        elif re.match(r'^F(\d+)\.(\d+)$', t, re.IGNORECASE):
            m_f = re.match(r'^F(\d+)\.(\d+)$', t, re.IGNORECASE)
            w, d = m_f.group(1), m_f.group(2)
            cpp_parts.append(f"{{:{w}.{d}f}}")
            py_parts.append(f"{{arg{arg_idx}:{w}.{d}f}}")
            arg_types.append("double")
            arg_idx += 1

        # Exponential: Ew.d or ESw.d
        elif re.match(r'^(?:E|ES)(\d+)\.(\d+)$', t, re.IGNORECASE):
            m_e = re.match(r'^(?:E|ES)(\d+)\.(\d+)$', t, re.IGNORECASE)
            w, d = m_e.group(1), m_e.group(2)
            cpp_parts.append(f"{{:{w}.{d}e}}")
            py_parts.append(f"{{arg{arg_idx}:{w}.{d}e}}")
            arg_types.append("double")
            arg_idx += 1

        # General float: Gw.d
        elif re.match(r'^G(\d+)\.(\d+)$', t, re.IGNORECASE):
            m_g = re.match(r'^G(\d+)\.(\d+)$', t, re.IGNORECASE)
            w, d = m_g.group(1), m_g.group(2)
            cpp_parts.append(f"{{:{w}.{d}g}}")
            py_parts.append(f"{{arg{arg_idx}:{w}.{d}g}}")
            arg_types.append("double")
            arg_idx += 1

        # String: A or Aw
        elif re.match(r'^A(\d+)?$', t, re.IGNORECASE):
            m_a = re.match(r'^A(\d+)?$', t, re.IGNORECASE)
            w = m_a.group(1) if m_a.group(1) else ""
            fmt_spec = f":{w}s" if w else ""
            cpp_parts.append(f"{{{fmt_spec}}}")
            py_parts.append(f"{{arg{arg_idx}{fmt_spec}}}")
            arg_types.append("std::string")
            arg_idx += 1

        else:
            # Fallback
            cpp_parts.append("{}")
            py_parts.append(f"{{arg{arg_idx}}}")
            arg_types.append("auto")
            arg_idx += 1

    return {
        "cpp_format_string": "".join(cpp_parts),
        "python_format_template": "".join(py_parts),
        "arg_types": arg_types,
        "num_args": arg_idx
    }


def decompile_write_statement(write_stmt: str, format_dict: Dict[str, str] = None) -> Tuple[str, str]:
    r"""
    Decompile a WRITE(UNIT, FORMAT) VAR1, VAR2 statement.
    Returns: (cpp_code, python_code)
    """
    format_dict = format_dict or {}
    # WRITE(6, 100) A, B
    m = re.match(r'^WRITE\s*\(([^)]+)\)\s*(.*)$', write_stmt.strip(), re.IGNORECASE)
    if not m:
        return f"// {write_stmt}", f"# {write_stmt}"

    controls = [c.strip() for c in m.group(1).split(',')]
    vars_raw = m.group(2).strip()
    var_list = [v.strip().lower() for v in vars_raw.split(',') if v.strip()]

    unit = controls[0] if len(controls) > 0 else "*"
    fmt_label = controls[1] if len(controls) > 1 else "*"

    # Check if format is a label or '*'
    if fmt_label == "*":
        vars_join_cpp = " << \" \" << ".join(var_list)
        vars_join_py = ", ".join(var_list)
        return (
            f"std::cout << {vars_join_cpp} << std::endl;",
            f"print({vars_join_py})"
        )

    if fmt_label in format_dict:
        raw_fmt = format_dict[fmt_label]
        decomp = decompile_format_descriptors(raw_fmt)
        cpp_fmt = decomp["cpp_format_string"]
        py_template = decomp["python_format_template"]

        # Substitute variable names into python template
        py_expr = py_template
        for i, v in enumerate(var_list):
            py_expr = py_expr.replace(f"arg{i}", v)

        cpp_code = f"std::cout << std::format(\"{cpp_fmt}\\n\", {', '.join(var_list)});"
        python_code = f"print(f\"{py_expr}\")"
        return cpp_code, python_code

    # Fallback if format label not found
    return (
        f"std::cout << /* format {fmt_label} */ {' << \" \" << '.join(var_list)} << std::endl;",
        f"print({', '.join(var_list)}) # format {fmt_label}"
    )


if __name__ == "__main__":
    fmt_sample = "100 FORMAT(1X, 'ENERGY = ', 1P, E15.7, 2X, 'STEP = ', I6)"
    print(f"[format_decompiler] Testing on: {fmt_sample}")
    res = decompile_format_descriptors(fmt_sample)
    print(" C++ format string:  ", repr(res["cpp_format_string"]))
    print(" Python template:    ", repr(res["python_format_template"]))
    print(" Arg types:          ", res["arg_types"])

    write_sample = "WRITE(6, 100) ENERGY, ISTEP"
    c_out, p_out = decompile_write_statement(write_sample, {"100": fmt_sample})
    print("\nDecompiled WRITE:")
    print(" C++:    ", c_out)
    print(" Python: ", p_out)
