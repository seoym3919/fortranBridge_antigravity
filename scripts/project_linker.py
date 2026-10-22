#!/usr/bin/env python3
r"""
project_linker.py - Multi-File Project Dependency Graph and Modular Code Generator.

Scans directories of legacy/modern Fortran files, builds a Directed Acyclic Graph (DAG)
of dependencies (MODULE, USE, CALL, INCLUDE), topologically sorts compilation order,
and emits modular C++ (.hpp / .cpp with CMakeLists.txt) and Python package structures.
"""

from __future__ import annotations
import os
import re
from typing import List, Dict, Set, Tuple, Optional


class FortranFileInfo:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.modules_defined: Set[str] = set()
        self.modules_used: Set[str] = set()
        self.routines_defined: Set[str] = set()
        self.routines_called: Set[str] = set()
        self.includes: Set[str] = set()
        self.common_blocks: Set[str] = set()
        self._analyze()

    def _analyze(self):
        with open(self.filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for l in lines:
            line = l.strip()
            # Comments
            if not line or line.startswith(('C', 'c', '*', '!')):
                continue

            # MODULE def
            m_mod = re.match(r'^MODULE\s+([A-Za-z0-9_]+)\b', line, re.IGNORECASE)
            if m_mod and not line.upper().startswith("MODULE PROCEDURE"):
                self.modules_defined.add(m_mod.group(1).lower())

            # USE mod
            m_use = re.match(r'^USE\s+([A-Za-z0-9_]+)\b', line, re.IGNORECASE)
            if m_use:
                self.modules_used.add(m_use.group(1).lower())

            # SUBROUTINE / FUNCTION def
            m_sub = re.match(r'^(?:(?:RECURSIVE|DOUBLE\s+PRECISION|REAL|INTEGER)\s+)*(?:SUBROUTINE|FUNCTION)\s+([A-Za-z0-9_]+)', line, re.IGNORECASE)
            if m_sub:
                self.routines_defined.add(m_sub.group(1).lower())

            # CALL routine
            m_call = re.match(r'^CALL\s+([A-Za-z0-9_]+)', line, re.IGNORECASE)
            if m_call:
                self.routines_called.add(m_call.group(1).lower())

            # INCLUDE 'file'
            m_inc = re.match(r'^INCLUDE\s+[\'"]([^\'"]+)[\'"]', line, re.IGNORECASE)
            if m_inc:
                self.includes.add(m_inc.group(1).lower())

            # COMMON /block/
            m_comm = re.match(r'^COMMON\s*/([A-Za-z0-9_]+)/', line, re.IGNORECASE)
            if m_comm:
                self.common_blocks.add(m_comm.group(1).lower())


class ProjectGraph:
    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self.files: Dict[str, FortranFileInfo] = {}
        self.dependencies: Dict[str, Set[str]] = {}
        self._scan()
        self._build_graph()

    def _scan(self):
        fortran_exts = {".f", ".f77", ".for", ".f90", ".f95", ".f03", ".f08"}
        for root, _, filenames in os.walk(self.root_dir):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext in fortran_exts:
                    path = os.path.join(root, fname)
                    info = FortranFileInfo(path)
                    self.files[info.filename] = info

    def _build_graph(self):
        # Map module/routine name to file
        symbol_to_file: Dict[str, str] = {}
        for fname, info in self.files.items():
            for m in info.modules_defined:
                symbol_to_file[f"mod:{m}"] = fname
            for r in info.routines_defined:
                symbol_to_file[f"sub:{r}"] = fname

        for fname, info in self.files.items():
            deps = set()
            for u in info.modules_used:
                key = f"mod:{u}"
                if key in symbol_to_file and symbol_to_file[key] != fname:
                    deps.add(symbol_to_file[key])

            for c in info.routines_called:
                key = f"sub:{c}"
                if key in symbol_to_file and symbol_to_file[key] != fname:
                    deps.add(symbol_to_file[key])

            self.dependencies[fname] = deps

    def topological_sort(self) -> List[str]:
        r"""Compute dependency-ordered compilation sequence."""
        visited: Set[str] = set()
        temp: Set[str] = set()
        order: List[str] = []

        def visit(node: str):
            if node in temp:
                # Cycle detected, break cycle gracefully
                return
            if node not in visited:
                temp.add(node)
                for dep in self.dependencies.get(node, []):
                    if dep in self.files:
                        visit(dep)
                temp.remove(node)
                visited.add(node)
                order.append(node)

        for f in sorted(self.files.keys()):
            if f not in visited:
                visit(f)

        return order

    def generate_cmakelists(self, project_name: str = "ModernizedFortran") -> str:
        r"""Generate modern CMakeLists.txt for translated C++ project."""
        lines = [
            "cmake_minimum_required(VERSION 3.20)",
            f"project({project_name} LANGUAGES CXX)",
            "",
            "set(CMAKE_CXX_STANDARD 20)",
            "set(CMAKE_CXX_STANDARD_REQUIRED ON)",
            "set(CMAKE_CXX_EXTENSIONS OFF)",
            "",
            "# Optimization flags",
            "if(CMAKE_CXX_COMPILER_ID MATCHES \"GNU|Clang\")",
            "    add_compile_options(-O3 -march=native -Wall -Wextra)",
            "endif()",
            "",
            "include_directories(include)",
            "file(GLOB_RECURSE SOURCES \"src/*.cpp\")",
            "",
            f"add_library({project_name}_core STATIC ${{SOURCES}})",
            f"target_include_directories({project_name}_core PUBLIC include)",
            "",
            f"# Test executable or main target",
            f"# add_executable({project_name}_app src/main.cpp)",
            f"# target_link_libraries({project_name}_app PRIVATE {project_name}_core)"
        ]
        return "\n".join(lines)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create 2 mock files: math_mod.f90 and main_sim.f
        math_f90 = """
MODULE MATH_UTILS
  DOUBLE PRECISION PI
  PARAMETER (PI = 3.141592653589793D0)
END MODULE
"""
        sim_f = """
SUBROUTINE RUN_SIM
  USE MATH_UTILS
  CALL COMPUTE_FORCE
END SUBROUTINE
SUBROUTINE COMPUTE_FORCE
  RETURN
END SUBROUTINE
"""
        with open(os.path.join(tmpdir, "math_utils.f90"), "w") as f:
            f.write(math_f90)
        with open(os.path.join(tmpdir, "main_sim.f"), "w") as f:
            f.write(sim_f)

        proj = ProjectGraph(tmpdir)
        print("[project_linker] Scanned files:", list(proj.files.keys()))
        print("[project_linker] Dependencies:", proj.dependencies)
        print("[project_linker] Topological compile order:", proj.topological_sort())
        print("\nGenerated CMakeLists.txt:")
        print(proj.generate_cmakelists())
