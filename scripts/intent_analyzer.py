#!/usr/bin/env python3
r"""
intent_analyzer.py - Static Dataflow Intent Inference Engine for Fortran Subroutines.

Infers parameter intents:
- INTENT(IN)    : Read-only -> C++: const T& / std::span<const T>, Python: input arg
- INTENT(OUT)   : Write-only -> C++: T&, Python: return tuple member
- INTENT(INOUT) : Read & Written -> C++: T&, Python: in-place modification or return member
"""

from __future__ import annotations
import re
from typing import List, Dict, Any, Set, Tuple
from fortran_lexer import FortranStatement, lex_fortran_source


class ParameterIntent:
    def __init__(self, name: str):
        self.name = name.lower()
        self.is_read = False
        self.is_written = False
        self.is_array = False
        self.inferred_type = "double"

    @property
    def intent(self) -> str:
        if self.is_written and not self.is_read:
            return "OUT"
        elif self.is_read and self.is_written:
            return "INOUT"
        else:
            return "IN"

    def to_cpp_param(self) -> str:
        r"""Format C++ parameter declaration with const correctness."""
        if self.is_array:
            if self.intent == "IN":
                return f"const std::vector<{self.inferred_type}>& {self.name}"
            else:
                return f"std::vector<{self.inferred_type}>& {self.name}"
        else:
            if self.intent == "IN":
                return f"{self.inferred_type} {self.name}"
            else:
                return f"{self.inferred_type}& {self.name}"


def analyze_routine_intents(statements: List[FortranStatement]) -> Dict[str, Dict[str, ParameterIntent]]:
    r"""Analyze definition-use chains for parameters across all routines in statements."""
    routines: Dict[str, Dict[str, ParameterIntent]] = {}

    current_routine: Optional[str] = None
    params: Dict[str, ParameterIntent] = {}
    known_arrays: Set[str] = set()

    # Pass 1: Identify DIMENSION arrays
    for s in statements:
        if s.stmt_type == "DIMENSION":
            raw_dim = s.details["raw"]
            for item in re.split(r',\s*(?![^()]*\))', raw_dim):
                m_dim = re.match(r'^([A-Za-z0-9_]+)\s*\(', item.strip())
                if m_dim:
                    known_arrays.add(m_dim.group(1).lower())
        elif s.stmt_type == "TYPE_DECL":
            decl_str = s.details["declarations"]
            for item in re.split(r',\s*(?![^()]*\))', decl_str):
                m_dim = re.match(r'^([A-Za-z0-9_]+)\s*\(', item.strip())
                if m_dim:
                    known_arrays.add(m_dim.group(1).lower())

    # Pass 2: Dataflow Def-Use Analysis
    for s in statements:
        st = s.stmt_type

        if st in ("SUBROUTINE", "FUNCTION"):
            current_routine = s.details["name"].lower()
            params = {}
            for p in s.details["params"]:
                p_name = p.lower()
                pi = ParameterIntent(p_name)
                if p_name in known_arrays:
                    pi.is_array = True
                params[p_name] = pi
            routines[current_routine] = params

        elif current_routine is not None:
            # Check LHS write
            if st == "ASSIGNMENT":
                lhs = s.details["lhs"].strip().lower()
                rhs = s.details["rhs"].strip().lower()

                # Extract target name (e.g., A or A(I))
                m_lhs_var = re.match(r'^([A-Za-z0-9_]+)', lhs)
                if m_lhs_var:
                    v = m_lhs_var.group(1).lower()
                    if v in params:
                        params[v].is_written = True

                # RHS reads
                tokens = re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*\b', rhs)
                for t in tokens:
                    if t in params:
                        params[t].is_read = True

            elif st in ("IF_THEN", "IF_STMT", "ELSE_IF"):
                cond = s.details["condition"].lower()
                tokens = re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*\b', cond)
                for t in tokens:
                    if t in params:
                        params[t].is_read = True

            elif st == "DO":
                # Start, end, step are read
                for key in ("start", "end", "step"):
                    val = s.details[key].lower()
                    for t in re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*\b', val):
                        if t in params:
                            params[t].is_read = True

            elif st == "END":
                current_routine = None

    return routines


if __name__ == "__main__":
    sample = """
      SUBROUTINE STEP(X0, V0, DT, GM, XF, VF)
      DIMENSION X0(3), V0(3), XF(3), VF(3)
      DO 10 I = 1, 3
         XF(I) = X0(I) + DT * V0(I)
         VF(I) = V0(I) - DT * GM / (X0(1)**2)
 10   CONTINUE
      RETURN
      END
    """
    stmts = lex_fortran_source(sample)
    intents = analyze_routine_intents(stmts)
    for r_name, p_dict in intents.items():
        print(f"[intent_analyzer] Subroutine: {r_name}")
        for p_name, pi in p_dict.items():
            print(f"  {p_name:10s} -> INTENT({pi.intent:5s}) | C++: {pi.to_cpp_param()}")
