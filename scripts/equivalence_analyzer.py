#!/usr/bin/env python3
r"""
equivalence_analyzer.py - Analyzes and De-aliases Fortran EQUIVALENCE Statements.

Translates legacy memory aliasing:
1. Scalar-to-array overlay:
   EQUIVALENCE (VEC(1), X), (VEC(2), Y), (VEC(3), Z)
   -> C++: double& x = vec[0]; double& y = vec[1]; double& z = vec[2];
   -> Python: x, y, z = vec[0], vec[1], vec[2] (or property accessors)

2. 1D-to-2D Matrix reshaping overlay:
   DIMENSION A(100), B(10, 10)
   EQUIVALENCE (A(1), B(1, 1))
   -> C++: std::span<double> a(b.data(), 100); or Eigen::Map
   -> Python: b = a.reshape((10, 10), order='F')
"""

from __future__ import annotations
import re
from typing import List, Dict, Any, Tuple, Optional


class EquivalenceGroup:
    def __init__(self, raw_str: str):
        self.raw_str = raw_str
        self.members: List[Dict[str, Any]] = []
        self._parse()

    def _parse(self):
        # Split by comma not inside parentheses: e.g. "FLAT(1), MAT(1, 1)" -> ["FLAT(1)", "MAT(1, 1)"]
        parts = re.split(r',\s*(?![^()]*\))', self.raw_str)
        for item in parts:
            item_clean = item.strip()
            m_arr = re.match(r'^([A-Za-z0-9_]+)\s*\(([^)]+)\)$', item_clean)
            if m_arr:
                name = m_arr.group(1).lower()
                indices = [idx.strip() for idx in m_arr.group(2).split(',')]
                self.members.append({
                    "name": name,
                    "is_array": True,
                    "indices": indices,
                    "raw": item_clean
                })
            else:
                self.members.append({
                    "name": item_clean.lower(),
                    "is_array": False,
                    "indices": [],
                    "raw": item_clean
                })

    def emit_cpp_aliases(self) -> List[str]:
        r"""Generate C++ reference aliases or spans."""
        cpp_lines = []
        if len(self.members) < 2:
            return cpp_lines

        primary = self.members[0]
        for sec in self.members[1:]:
            # Case 1: Primary is array element, Secondary is scalar
            if primary["is_array"] and not sec["is_array"]:
                idx_0 = int(primary["indices"][0]) - 1 if primary["indices"][0].isdigit() else f"{primary['indices'][0]} - 1"
                cpp_lines.append(f"double& {sec['name']} = {primary['name']}[{idx_0}]; // EQUIVALENCE alias")

            # Case 2: Primary is scalar, Secondary is array element
            elif not primary["is_array"] and sec["is_array"]:
                idx_0 = int(sec["indices"][0]) - 1 if sec["indices"][0].isdigit() else f"{sec['indices'][0]} - 1"
                cpp_lines.append(f"double& {primary['name']} = {sec['name']}[{idx_0}]; // EQUIVALENCE alias")

            # Case 3: Both are arrays (e.g. 1D to 2D view)
            elif primary["is_array"] and sec["is_array"]:
                cpp_lines.append(f"// EQUIVALENCE array memory alias between {primary['name']} and {sec['name']}")
                cpp_lines.append(f"auto* {sec['name']}_ptr = {primary['name']}.data();")

            # Case 4: Both are scalars
            else:
                cpp_lines.append(f"double& {sec['name']} = {primary['name']}; // EQUIVALENCE scalar alias")

        return cpp_lines

    def emit_python_aliases(self) -> List[str]:
        r"""Generate Python references or memory views."""
        py_lines = []
        if len(self.members) < 2:
            return py_lines

        primary = self.members[0]
        for sec in self.members[1:]:
            if primary["is_array"] and not sec["is_array"]:
                idx_0 = int(primary["indices"][0]) - 1 if primary["indices"][0].isdigit() else f"{primary['indices'][0]} - 1"
                py_lines.append(f"# EQUIVALENCE: {sec['name']} aliases {primary['name']}[{idx_0}]")
                py_lines.append(f"{sec['name']} = {primary['name']}[{idx_0}]")
            elif primary["is_array"] and sec["is_array"]:
                py_lines.append(f"# EQUIVALENCE zero-copy array view:")
                py_lines.append(f"{sec['name']} = {primary['name']}.view() # reshape with order='F' as needed")
            else:
                py_lines.append(f"{sec['name']} = {primary['name']} # EQUIVALENCE alias")

        return py_lines


def parse_equivalence_statements(code_text: str) -> List[EquivalenceGroup]:
    r"""Extract all EQUIVALENCE statement groups using balanced parenthesis parsing."""
    groups = []
    for line in code_text.splitlines():
        line_clean = line.strip()
        m = re.match(r'^EQUIVALENCE\s*(.*)$', line_clean, re.IGNORECASE)
        if not m:
            continue
        rest = m.group(1).strip()
        depth = 0
        current: List[str] = []
        for ch in rest:
            if ch == '(':
                if depth > 0:
                    current.append(ch)
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    group_str = ''.join(current).strip()
                    if group_str:
                        groups.append(EquivalenceGroup(group_str))
                    current = []
                else:
                    current.append(ch)
            elif depth > 0:
                current.append(ch)
    return groups


if __name__ == "__main__":
    sample = """
      DIMENSION VEC(3), MAT(3, 3), FLAT(9)
      EQUIVALENCE (VEC(1), X), (VEC(2), Y), (VEC(3), Z)
      EQUIVALENCE (FLAT(1), MAT(1, 1))
    """
    print("[equivalence_analyzer] Parsing EQUIVALENCE statements:")
    eq_groups = parse_equivalence_statements(sample)
    for idx, g in enumerate(eq_groups, 1):
        print(f"\nGroup {idx}: {g.raw_str}")
        print("C++ Aliases:")
        for line in g.emit_cpp_aliases():
            print(" ", line)
        print("Python Aliases:")
        for line in g.emit_python_aliases():
            print(" ", line)
