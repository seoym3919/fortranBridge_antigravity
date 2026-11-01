#!/usr/bin/env python3
r"""
fortran_lexer.py - Robust Fortran 66/77/90/95/2003 Statement Lexer & Normalizer.

Handles:
- Fixed-format card layout (columns 1-5 label, col 6 continuation, cols 7-72 body).
- Free-format modern Fortran (& line continuation, inline comments).
- Statement classification (DO, IF, ASSIGNMENT, COMMON, EQUIVALENCE, SUBROUTINE, etc.).
- Variable typing and dimension bounds.
"""

from __future__ import annotations
import re
from typing import List, Dict, Any, Optional, Tuple


class FortranStatement:
    def __init__(
        self,
        raw: str,
        clean: str,
        line_no: int,
        label: Optional[str] = None,
        stmt_type: str = "UNKNOWN",
        details: Optional[Dict[str, Any]] = None
    ):
        self.raw = raw
        self.clean = clean
        self.line_no = line_no
        self.label = label
        self.stmt_type = stmt_type
        self.details = details or {}

    def __repr__(self):
        lbl = f"[{self.label}] " if self.label else ""
        return f"<FortranStatement {lbl}{self.stmt_type} (L{self.line_no}): '{self.clean}'>"


def is_fixed_format(text: str) -> bool:
    r"""Heuristic to detect fixed-format vs free-format Fortran."""
    lines = text.splitlines()
    fixed_score = 0
    total_non_empty = 0

    for l in lines[:100]:
        if not l.strip():
            continue
        total_non_empty += 1
        # Fixed format comments in col 1
        if len(l) > 0 and l[0] in ('C', 'c', '*') and (len(l) == 1 or not l[1].isalnum()):
            fixed_score += 2
        # Fixed format continuation in col 6
        elif len(l) >= 6 and l[0:5].strip() == "" and l[5] not in (' ', '0', '\t'):
            fixed_score += 3
        # Statement label in cols 1-5
        elif len(l) >= 6 and l[0:5].strip().isdigit() and l[5] in (' ', '0', '\t'):
            fixed_score += 2

    return fixed_score >= 3 or (total_non_empty > 0 and fixed_score / total_non_empty > 0.15)


def normalize_fixed_format(lines: List[str]) -> List[Tuple[int, Optional[str], str]]:
    r"""
    Stitch fixed-format continuation lines and extract statement labels.
    Returns list of (start_line_no, label, statement_text).
    """
    statements = []
    current_label: Optional[str] = None
    current_text = ""
    current_start_line = 1

    for idx, raw_line in enumerate(lines, 1):
        # Handle column 73+ card sequence numbers if strictly numeric
        if len(raw_line) > 72 and raw_line[72:].strip().isdigit():
            line = raw_line[:72].rstrip()
        else:
            line = raw_line.rstrip()

        # Check comment in column 1 (a leading 'C' followed by a letter is code, e.g. CALL)
        if len(line) > 0 and (line[0] in ('*', '!') or (line[0] in ('C', 'c') and (len(line) == 1 or not line[1].isalnum()))):
            continue
        if not line.strip():
            continue

        col1_5 = line[:5] if len(line) >= 5 else line
        col6 = line[5] if len(line) >= 6 else ' '
        body = line[6:] if len(line) >= 6 else ""

        # Check if continuation line (col 6 is not space or 0)
        is_continuation = len(line) >= 6 and col6 not in (' ', '0')

        if is_continuation:
            # Append to current statement
            current_text += " " + body.strip()
        else:
            # Finalize previous statement if any
            if current_text:
                statements.append((current_start_line, current_label, current_text))
            
            # Start new statement
            lbl = col1_5.strip()
            current_label = lbl if lbl.isdigit() else None
            current_text = body.strip()
            current_start_line = idx

    if current_text:
        statements.append((current_start_line, current_label, current_text))

    return statements


def normalize_free_format(lines: List[str]) -> List[Tuple[int, Optional[str], str]]:
    r"""
    Stitch free-format continuation lines (&) and extract statement labels.
    """
    statements = []
    current_label: Optional[str] = None
    current_text = ""
    current_start_line = 1

    for idx, raw_line in enumerate(lines, 1):
        # Strip inline comments
        line = re.sub(r'!.*$', '', raw_line).strip()
        if not line:
            continue

        # Check for label at beginning (e.g. "10 FORMAT(...)")
        m_lbl = re.match(r'^(\d+)\s+(.*)$', line)
        line_label = None
        if m_lbl:
            line_label = m_lbl.group(1)
            line = m_lbl.group(2).strip()

        # Check continuation &
        has_cont = line.endswith('&')
        if has_cont:
            line_content = line[:-1].strip()
        else:
            line_content = line

        if current_text:
            # Strip leading & if present
            if line_content.startswith('&'):
                line_content = line_content[1:].strip()
            current_text += " " + line_content
        else:
            current_label = line_label
            current_text = line_content
            current_start_line = idx

        if not has_cont:
            # Semicolon splitting
            sub_stmts = [s.strip() for s in current_text.split(';') if s.strip()]
            for s_idx, s in enumerate(sub_stmts):
                lbl = current_label if s_idx == 0 else None
                statements.append((current_start_line, lbl, s))
            current_text = ""
            current_label = None

    if current_text:
        statements.append((current_start_line, current_label, current_text))

    return statements


_TYPE_SPEC = r'(DOUBLE\s+PRECISION|REAL|INTEGER|LOGICAL|COMPLEX|CHARACTER|TYPE\s*\(\s*[A-Za-z]\w*\s*\))'
_KIND_SPEC = r'(\*\s*\d+|\(\s*(?:(?:KIND|LEN)\s*=\s*)?[\w*]+\s*\))?'


def _split_top(text: str, sep: str = ',') -> List[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if ''.join(cur).strip():
        parts.append(''.join(cur).strip())
    return parts


def _canonical_type(base: str, kind: Optional[str]) -> str:
    if base.upper().startswith('TYPE'):
        return re.sub(r'\s+', '', base.upper())              # TYPE(POINT)
    base = re.sub(r'\s+', ' ', base.upper())
    if base == 'DOUBLE PRECISION' or not kind:
        return base
    if base == 'CHARACTER':
        # CHARACTER*8 / CHARACTER(8) / CHARACTER(LEN=8) / CHARACTER(LEN=n): keep the length; (*) is an assumed length
        inner = re.sub(r'^\*|^\(\s*(?:LEN\s*=\s*)?|\s*\)$', '', kind.strip(), flags=re.IGNORECASE)
        return f"CHARACTER*{inner.upper()}" if re.fullmatch(r'\d+|[A-Za-z_]\w*', inner) else base
    k = re.sub(r'[^0-9]', '', kind)
    if base == 'COMPLEX' and k and kind.lstrip().startswith('('):
        return f"COMPLEX*{2 * int(k)}"            # COMPLEX(8) is two 8-byte parts = COMPLEX*16; COMPLEX*8 is two 4-byte parts
    if not k:
        # a named kind such as REAL(dp): keep the name so the semantic layer can resolve it (REAL*DP)
        name = re.sub(r'^\(\s*(?:KIND\s*=\s*)?|\s*\)$', '', kind.strip(), flags=re.IGNORECASE)
        return f"{base}*{name.upper()}" if re.fullmatch(r'[A-Za-z_]\w*', name) else base
    return f"{base}*{k}"


def _match_type_decl(t: str) -> Optional[Dict[str, Any]]:
    r"""Parse `REAL(8), INTENT(IN) :: A(N)`, `DOUBLE PRECISION X, Y`, `INTEGER*4 N` declarations."""
    m = re.match(rf'^{_TYPE_SPEC}\s*{_KIND_SPEC}\s*(.*)$', t, re.IGNORECASE)
    if not m:
        return None
    base, kind, rest = m.group(1), m.group(2), m.group(3)
    # reject assignments to variables whose names merely start with a type word (e.g. REALVAL = 1)
    if re.match(rf'^{_TYPE_SPEC}[A-Za-z0-9_]', t, re.IGNORECASE):
        return None
    attrs: List[str] = []
    if '::' in rest:
        attr_part, decls = rest.split('::', 1)
        attrs = [a for a in _split_top(attr_part.lstrip(', ')) if a]
    else:
        decls = rest
    decls = decls.strip()
    if not decls or decls.startswith('=') or (not attrs and rest.startswith('(') and '::' not in rest):
        return None
    out: Dict[str, Any] = {"type": _canonical_type(base, kind), "declarations": decls, "attrs": []}
    for a in attrs:
        am = re.match(r'^(\w+)\s*(?:\((.*)\))?$', a.strip(), re.IGNORECASE)
        if not am:
            continue
        key = am.group(1).lower()
        out["attrs"].append(key)
        if key == 'intent' and am.group(2):
            out["intent"] = re.sub(r'\s+', '', am.group(2)).lower()
        elif key == 'dimension' and am.group(2):
            out["attr_dimension"] = am.group(2)
        elif key == 'parameter':
            out["is_parameter"] = True
    return out


def _match_parenthesized_if(t: str) -> Optional[Tuple[str, str]]:
    r"""For `IF (cond) rest` return (cond, rest) using balanced parentheses."""
    m = re.match(r'^IF\s*\(', t, re.IGNORECASE)
    if not m:
        return None
    depth, start = 0, m.end() - 1
    for k in range(start, len(t)):
        if t[k] == '(':
            depth += 1
        elif t[k] == ')':
            depth -= 1
            if depth == 0:
                return t[start + 1:k], t[k + 1:].strip()
    return None


def classify_statement(stmt_text: str) -> Tuple[str, Dict[str, Any]]:
    r"""Classify clean Fortran statement into statement type and extracted metadata."""
    t = stmt_text.strip()
    t_upper = t.upper()

    # Subroutine / Function
    m_sub = re.match(r'^(?:RECURSIVE\s+)?SUBROUTINE\s+([A-Za-z0-9_]+)(?:\s*\(([^)]*)\))?', t, re.IGNORECASE)
    if m_sub:
        params = [p.strip() for p in m_sub.group(2).split(',')] if m_sub.group(2) else []
        return "SUBROUTINE", {"name": m_sub.group(1), "params": params}

    m_func = re.match(rf'^(?:(?:{_TYPE_SPEC}\s*{_KIND_SPEC})\s+)?(?:RECURSIVE\s+|PURE\s+|ELEMENTAL\s+)*FUNCTION\s+([A-Za-z0-9_]+)(?:\s*\(([^)]*)\))?', t, re.IGNORECASE)
    if m_func:
        params = [p.strip() for p in m_func.group(4).split(',') if p.strip()] if m_func.group(4) else []
        result_type = _canonical_type(m_func.group(1), m_func.group(2)) if m_func.group(1) else None
        return "FUNCTION", {"name": m_func.group(3), "params": params, "result_type": result_type}

    m_prog = re.match(r'^PROGRAM\s+([A-Za-z0-9_]+)\s*$', t, re.IGNORECASE)
    if m_prog:
        return "PROGRAM", {"name": m_prog.group(1), "params": []}

    if re.match(r'^END\s*(?:(?:SUBROUTINE|FUNCTION|MODULE|PROGRAM)(?:\s+[A-Za-z0-9_]+)?)?$', t_upper):
        return "END", {}

    # TYPE name ... END TYPE (a derived-type definition; TYPE(name) :: x is a declaration)
    m_typedef = re.match(r'^TYPE(?!\s*\()\s*(?:,\s*(.*?))?\s*(?:::)?\s*([A-Za-z]\w*)$', t, re.IGNORECASE)
    if m_typedef:
        return "TYPE_DEF", {"name": m_typedef.group(2).lower(), "attrs": (m_typedef.group(1) or "").lower()}
    if re.match(r'^END\s*TYPE\b', t_upper):
        return "END_TYPE", {}

    # SELECT CASE (expr) / CASE (v, lo:hi) / CASE DEFAULT / END SELECT
    m_select = re.match(r'^SELECT\s*CASE\s*\((.*)\)$', t, re.IGNORECASE)
    if m_select:
        return "SELECT_CASE", {"expr": m_select.group(1)}
    if re.match(r'^CASE\s*DEFAULT$', t_upper):
        return "CASE", {"default": True, "selectors": []}
    m_case = re.match(r'^CASE\s*\((.*)\)$', t, re.IGNORECASE)
    if m_case:
        return "CASE", {"default": False, "selectors": _split_top(m_case.group(1))}
    if re.match(r'^END\s*SELECT$', t_upper):
        return "END_SELECT", {}

    # ALLOCATE / DEALLOCATE / NULLIFY, attribute statements, pointer assignment
    m_alloc = re.match(r'^(ALLOCATE|DEALLOCATE|NULLIFY)\s*\((.*)\)$', t, re.IGNORECASE)
    if m_alloc:
        items = [x.strip() for x in _split_top(m_alloc.group(2)) if x.strip()]
        options = [x for x in items if re.match(r'^(STAT|ERRMSG|SOURCE|MOLD)\s*=', x, re.IGNORECASE)]
        return m_alloc.group(1).upper(), {"objects": [x for x in items if x not in options], "options": options}
    m_attr = re.match(r'^(ALLOCATABLE|POINTER|TARGET)\b\s*(?:::)?\s*(.*)$', t, re.IGNORECASE)
    if m_attr and m_attr.group(2) and '=' not in t:
        return "ATTRIBUTE", {"attr": m_attr.group(1).lower(), "names": [x.strip() for x in _split_top(m_attr.group(2)) if x.strip()]}
    m_ptr = re.match(r'^([A-Za-z]\w*(?:\s*\(.*?\))?(?:%\w+(?:\s*\(.*?\))?)*)\s*=>\s*(.+)$', t)
    if m_ptr:
        return "POINTER_ASSIGN", {"lhs": m_ptr.group(1).strip(), "rhs": m_ptr.group(2).strip()}

    # Common Block: COMMON /NAME/ VAR1, VAR2(10)
    m_comm = re.match(r'^COMMON\s*(?:/([A-Za-z0-9_]*)/)?\s*(.*)$', t, re.IGNORECASE)
    if m_comm:
        block_name = m_comm.group(1) if m_comm.group(1) else "BLANK_COMMON"
        vars_list = [v.strip() for v in m_comm.group(2).split(',') if v.strip()]
        return "COMMON", {"block": block_name, "variables": vars_list}

    # Equivalence: EQUIVALENCE (A, B(3)), (C, D)
    m_equiv = re.match(r'^EQUIVALENCE\s*(\(.*)$', t, re.IGNORECASE)
    if m_equiv:
        sets, depth, start, body = [], 0, None, m_equiv.group(1)
        for k, ch in enumerate(body):
            if ch == '(':
                if depth == 0:
                    start = k + 1
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0 and start is not None:
                    sets.append([p.strip() for p in _split_top(body[start:k]) if p.strip()])
        return "EQUIVALENCE", {"sets": sets, "variables": [v for group in sets for v in group]}

    # Parameter: PARAMETER (PI = 3.1415D0, ...)
    m_param = re.match(r'^PARAMETER\s*\((.*)\)$', t, re.IGNORECASE)
    if m_param:
        params = {}
        for item in m_param.group(1).split(','):
            if '=' in item:
                k, v = item.split('=', 1)
                params[k.strip()] = v.strip()
        return "PARAMETER", {"constants": params}

    # Dimension: DIMENSION A(100), B(10, 20)
    m_dim = re.match(r'^DIMENSION\s+(.*)$', t, re.IGNORECASE)
    if m_dim:
        return "DIMENSION", {"raw": m_dim.group(1)}

    # Implicit
    if t_upper.startswith("IMPLICIT"):
        return "IMPLICIT", {"raw": t}

    # Type Declarations (DOUBLE PRECISION, REAL*8, REAL(8), INTEGER, LOGICAL, ... with attributes and ::)
    decl = _match_type_decl(t)
    if decl is not None:
        return "TYPE_DECL", decl

    # MODULE m / END MODULE / USE m [, ONLY: a, b] / CONTAINS
    m_module = re.match(r'^MODULE\s+([A-Za-z]\w*)$', t, re.IGNORECASE)
    if m_module and m_module.group(1).upper() != "PROCEDURE":
        return "MODULE", {"name": m_module.group(1).lower()}
    if re.match(r'^END\s*MODULE(?:\s+\w+)?$', t_upper):
        return "END_MODULE", {}
    m_use = re.match(r'^USE\s+(?:::\s*)?([A-Za-z]\w*)\s*(?:,\s*(.*))?$', t, re.IGNORECASE)
    if m_use:
        rest = (m_use.group(2) or "").strip()
        only: Optional[List[str]] = None
        if re.match(r'^ONLY\s*:', rest, re.IGNORECASE):
            only = []
            items = rest.split(':', 1)[1]
        else:
            items = rest
        renames: Dict[str, str] = {}
        names: List[str] = []
        for item in _split_top(items):
            if '=>' in item:
                local, remote = [x.strip().lower() for x in item.split('=>', 1)]
                renames[local] = remote
                names.append(remote)
            elif item.strip():
                names.append(item.strip().lower())
        if only is not None:
            only = names
        return "USE", {"module": m_use.group(1).lower(), "only": only, "renames": renames}
    m_iface = re.match(r'^INTERFACE\b\s*(.*)$', t, re.IGNORECASE)
    if m_iface:
        return "INTERFACE", {"name": m_iface.group(1).strip()}
    if re.match(r'^END\s*INTERFACE\b', t_upper):
        return "END_INTERFACE", {}
    if t_upper == "CONTAINS":
        return "CONTAINS", {}

    # STOP / non-executable declarations that carry no translatable semantics
    if re.match(r'^STOP\b', t_upper):
        return "STOP", {"text": t}
    if re.match(r'^(SAVE|EXTERNAL|INTRINSIC|DATA|USE|INCLUDE|CONTAINS|NAMELIST)\b', t_upper):
        return "NONEXEC", {"text": t}

    # DO WHILE / unbounded DO / EXIT / CYCLE (Fortran 90)
    m_dowhile = re.match(r'^DO\s+(?:(\d+)\s+)?WHILE\s*\((.*)\)$', t, re.IGNORECASE)
    if m_dowhile:
        return "DO_WHILE", {"loop_label": m_dowhile.group(1), "condition": m_dowhile.group(2)}
    if t_upper == "DO":
        return "DO_FOREVER", {}
    if t_upper in ("EXIT", "CYCLE"):
        return t_upper, {}

    # DO Loop: DO 10 I = 1, N, 2 or DO I = 1, N (bounds may contain commas: DO J = 1, SIZE(M, 2))
    m_do = re.match(r'^DO\s+(?:(\d+)\s+)?([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.+)$', t, re.IGNORECASE)
    if m_do:
        bounds = [b.strip() for b in _split_top(m_do.group(3))]
        if 2 <= len(bounds) <= 3:
            return "DO", {
                "loop_label": m_do.group(1),
                "var": m_do.group(2),
                "start": bounds[0],
                "end": bounds[1],
                "step": bounds[2] if len(bounds) == 3 else "1"
            }

    if re.match(r'^END\s*DO$', t_upper) or t_upper == "CONTINUE":
        return "END_DO" if "DO" in t_upper else "CONTINUE", {}

    # IF Statements (balanced-parenthesis aware)
    parsed_if = _match_parenthesized_if(t)
    if parsed_if is not None:
        cond, rest = parsed_if
        if rest.upper() == "THEN":
            return "IF_THEN", {"condition": cond}
        m_arith = re.match(r'^(\d+)\s*,\s*(\d+)\s*,\s*(\d+)$', rest)
        if m_arith:
            return "ARITHMETIC_IF", {
                "condition": cond,
                "label_neg": m_arith.group(1),
                "label_zero": m_arith.group(2),
                "label_pos": m_arith.group(3),
            }
        if rest and re.match(r'^[A-Za-z]', rest):
            return "IF_STMT", {"condition": cond, "statement": rest}

    if t_upper == "ELSE":
        return "ELSE", {}

    m_elseif = re.match(r'^ELSE\s*IF\s*\((.*)\)\s*THEN$', t, re.IGNORECASE)
    if m_elseif:
        return "ELSE_IF", {"condition": m_elseif.group(1)}

    if re.match(r'^END\s*IF$', t_upper):
        return "END_IF", {}

    # Computed GOTO: GOTO (10, 20, 30) I or GOTO (10, 20, 30), I
    m_cg = re.match(r'^GO\s*TO\s*\(([^)]+)\),?\s*([A-Za-z0-9_]+)$', t, re.IGNORECASE)
    if m_cg:
        labels = [lbl.strip() for lbl in m_cg.group(1).split(',')]
        return "COMPUTED_GOTO", {"labels": labels, "variable": m_cg.group(2)}

    # Simple GOTO: GOTO 100
    m_goto = re.match(r'^GO\s*TO\s*(\d+)$', t, re.IGNORECASE)
    if m_goto:
        return "GOTO", {"target_label": m_goto.group(1)}

    # Subroutine Call: CALL FOO(A, B)
    m_call = re.match(r'^CALL\s+([A-Za-z0-9_]+)(?:\s*\((.*)\))?$', t, re.IGNORECASE)
    if m_call:
        args = _split_top(m_call.group(2)) if m_call.group(2) else []     # a(1,2) is one argument
        return "CALL", {"subroutine": m_call.group(1), "args": args}

    # Return
    if t_upper.startswith("RETURN"):
        return "RETURN", {}

    # I/O: PRINT, WRITE, READ, FORMAT, and the file statements (their control lists contain '=', so they come before assignment)
    if re.match(r'^(PRINT|WRITE|READ|FORMAT)\b', t_upper) or re.match(r'^(OPEN|CLOSE|INQUIRE|FLUSH)\s*\(', t_upper) or \
            re.match(r'^(REWIND|BACKSPACE|ENDFILE)\b(?!\s*=)', t_upper):
        return "IO", {"statement": t}

    # Assignment: VAR = EXPR or ARRAY(I) = EXPR
    if '=' in t and not t_upper.startswith("DATA"):
        lhs, rhs = t.split('=', 1)
        return "ASSIGNMENT", {"lhs": lhs.strip(), "rhs": rhs.strip()}

    return "OTHER", {"text": t}


def lex_fortran_source(source_text: str) -> List[FortranStatement]:
    r"""Lex entire Fortran source into structured FortranStatement objects."""
    lines = source_text.splitlines()
    fixed = is_fixed_format(source_text)

    normalized = normalize_fixed_format(lines) if fixed else normalize_free_format(lines)

    statements = []
    for line_no, label, clean_text in normalized:
        s_type, details = classify_statement(clean_text)
        stmt = FortranStatement(
            raw=clean_text,
            clean=clean_text,
            line_no=line_no,
            label=label,
            stmt_type=s_type,
            details=details
        )
        statements.append(stmt)

    return statements


if __name__ == "__main__":
    sample_f77 = """C     ASTRONOMY ORBIT PROPAGATOR IN F77
      SUBROUTINE KEPLER(R0, V0, DT, GM, RF, VF)
      IMPLICIT DOUBLE PRECISION (A-H, O-Z)
      DIMENSION R0(3), V0(3), RF(3), VF(3)
      COMMON /ORBIT/ ECC, SEMI, INC
      PARAMETER (TOL = 1.0D-12, MAXIT = 100)
C     COMPUTE DISTANCE
      R = DSQRT(R0(1)**2 + R0(2)**2 + R0(3)**2)
      IF (R .LT. 1.0D-5) GO TO 999
      DO 10 I = 1, 3
         RF(I) = R0(I) + DT * V0(I)
 10   CONTINUE
      RETURN
 999  PRINT *, 'COLLISION DETECTED'
      END
"""
    print(f"[fortran_lexer] Testing on F77 sample (is_fixed: {is_fixed_format(sample_f77)}):")
    parsed = lex_fortran_source(sample_f77)
    for s in parsed:
        print(" ", s)
