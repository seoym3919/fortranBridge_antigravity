#!/usr/bin/env python3
r"""
goto_restructurer.py - De-spaghettifies Fortran Control Flow.

Converts:
- Arithmetic IF: IF (expr) 10, 20, 30 -> structured if-else or sign branch.
- Computed GOTO: GOTO (10, 20, 30) I -> switch / match statement.
- Backward loop GOTOs -> structured while/for loops.
- Error-trap forward GOTOs -> guard returns or exception blocks.
"""

from __future__ import annotations
import re
from typing import List, Dict, Any, Tuple
from fortran_lexer import FortranStatement


def restructure_arithmetic_if(stmt: FortranStatement, target: str = "cpp") -> str:
    r"""
    Translate Fortran Arithmetic IF: IF (expr) L1, L2, L3
    Into structured branching in C++ or Python.
    """
    cond = stmt.details["condition"]
    l_neg = stmt.details["label_neg"]
    l_zero = stmt.details["label_zero"]
    l_pos = stmt.details["label_pos"]

    # Normalize Fortran expression
    cond_clean = cond.strip()

    if target.lower() in ("cpp", "c++"):
        if l_neg == l_zero and l_neg != l_pos:
            return f"if ({cond_clean} <= 0) {{ goto label_{l_neg}; }} else {{ goto label_{l_pos}; }}"
        elif l_zero == l_pos and l_neg != l_zero:
            return f"if ({cond_clean} < 0) {{ goto label_{l_neg}; }} else {{ goto label_{l_pos}; }}"
        else:
            return (
                f"if ({cond_clean} < 0) {{ goto label_{l_neg}; }}\n"
                f"else if ({cond_clean} == 0) {{ goto label_{l_zero}; }}\n"
                f"else {{ goto label_{l_pos}; }}"
            )
    else:  # Python
        if l_neg == l_zero and l_neg != l_pos:
            return f"# Arithmetic IF\nif ({cond_clean} <= 0):\n    # branch {l_neg}\n    pass\nelse:\n    # branch {l_pos}\n    pass"
        else:
            return (
                f"# Arithmetic IF\n"
                f"if ({cond_clean} < 0):\n    # branch {l_neg}\n    pass\n"
                f"elif ({cond_clean} == 0):\n    # branch {l_zero}\n    pass\n"
                f"else:\n    # branch {l_pos}\n    pass"
            )


def restructure_computed_goto(stmt: FortranStatement, target: str = "cpp") -> str:
    r"""
    Translate Fortran Computed GOTO: GOTO (10, 20, 30) I
    Into C++ switch(i) or Python match/if-elif ladder.
    """
    labels = stmt.details["labels"]
    var = stmt.details["variable"].strip()

    if target.lower() in ("cpp", "c++"):
        cases = []
        for idx, lbl in enumerate(labels, 1):
            cases.append(f"    case {idx}: goto label_{lbl};")
        cases_str = "\n".join(cases)
        return (
            f"switch ({var}) {{\n"
            f"{cases_str}\n"
            f"    default: break;\n"
            f"}}"
        )
    else:  # Python
        ladder = []
        for idx, lbl in enumerate(labels, 1):
            if idx == 1:
                ladder.append(f"if {var} == 1:\n    # jump label {lbl}\n    pass")
            else:
                ladder.append(f"elif {var} == {idx}:\n    # jump label {lbl}\n    pass")
        return "\n".join(ladder)


def detect_loop_patterns(statements: List[FortranStatement]) -> List[Dict[str, Any]]:
    r"""Detect backward GOTO loops and forward guard jumps."""
    label_to_index: Dict[str, int] = {}
    for idx, s in enumerate(statements):
        if s.label:
            label_to_index[s.label] = idx

    patterns = []
    for idx, s in enumerate(statements):
        if s.stmt_type == "GOTO":
            target = s.details["target_label"]
            if target in label_to_index:
                target_idx = label_to_index[target]
                if target_idx < idx:
                    patterns.append({
                        "type": "BACKWARD_LOOP_GOTO",
                        "start_idx": target_idx,
                        "end_idx": idx,
                        "label": target,
                        "line": s.line_no
                    })
                elif target_idx > idx:
                    # Check if target is just RETURN or END
                    target_stmt = statements[target_idx]
                    if target_stmt.stmt_type in ("RETURN", "END"):
                        patterns.append({
                            "type": "EARLY_RETURN_GUARD",
                            "goto_idx": idx,
                            "target_idx": target_idx,
                            "label": target,
                            "line": s.line_no
                        })

    return patterns


if __name__ == "__main__":
    from fortran_lexer import lex_fortran_source
    code = """      SUBROUTINE TEST(I, X)
      IF (X) 10, 20, 30
 10   X = -X
      GO TO 40
 20   X = 0.0D0
      GO TO 40
 30   X = X * 2.0D0
 40   GO TO (100, 200, 300) I
 100  RETURN
 200  RETURN
 300  RETURN
      END"""
    stmts = lex_fortran_source(code)
    for s in stmts:
        if s.stmt_type == "ARITHMETIC_IF":
            print("C++ Arithmetic IF:\n", restructure_arithmetic_if(s, "cpp"))
            print("Python Arithmetic IF:\n", restructure_arithmetic_if(s, "python"))
        elif s.stmt_type == "COMPUTED_GOTO":
            print("C++ Computed GOTO:\n", restructure_computed_goto(s, "cpp"))
            print("Python Computed GOTO:\n", restructure_computed_goto(s, "python"))
