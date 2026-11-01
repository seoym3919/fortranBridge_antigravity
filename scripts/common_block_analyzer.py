#!/usr/bin/env python3
r"""
common_block_analyzer.py - Encapsulates Fortran COMMON Blocks into Modern C++ & Python Structures.

Translates legacy shared COMMON blocks:
    COMMON /ORBIT/ ECC, SEMI, INC, POS(3)
into:
- C++ struct / class with typed members and encapsulation:
    struct OrbitBlock { double ecc; double semi; double inc; std::array<double, 3> pos; };
- Python @dataclass:
    @dataclass
    class OrbitBlock:
        ecc: float = 0.0
        semi: float = 0.0
        inc: float = 0.0
        pos: np.ndarray = field(default_factory=lambda: np.zeros(3))
"""

from __future__ import annotations
import re
from typing import List, Dict, Any, Tuple
from fortran_lexer import FortranStatement, lex_fortran_source


class CommonBlockVariable:
    def __init__(self, name: str, f_type: str = "REAL*8", dimensions: str = "", symbol=None, extents=None):
        self.name = name.lower()
        self.f_type = f_type.upper()
        self.dimensions = dimensions  # e.g., "3" or "10, 20"
        self.symbol = symbol          # resolved Symbol (type, kind) when built from analysed units
        self.extents = extents        # constant extents of that Symbol's dimensions

    def _resolved_cpp_type(self) -> str:
        sym = self.symbol
        if sym.type == "real":
            return "float" if sym.bits == 32 else "double"
        if sym.type == "integer":
            return "long long" if sym.bits > 32 else "int"
        return {"logical": "bool", "character": "std::string", "complex": "std::complex<double>"}.get(sym.type, "double")

    def _resolved_cpp(self) -> str:
        base = self._resolved_cpp_type()
        if not self.extents:
            return f"{base} {self.name}{{}};"
        text = base
        for extent in reversed(self.extents):
            text = f"std::array<{text}, {extent}>"
        return f"{text} {self.name}{{}};"

    def _resolved_python(self) -> str:
        sym = self.symbol
        single = sym.type == "real" and sym.bits == 32
        dtype = ("np.float32" if single else {"real": "np.float64", "integer": "np.int64", "logical": "np.bool_",
                                              "complex": "np.complex128"}.get(sym.type, "object"))
        if self.extents:
            shape = f"({', '.join(map(str, self.extents))},)" if len(self.extents) == 1 else f"({', '.join(map(str, self.extents))})"
            return (f"{self.name}: np.ndarray = field(default_factory=lambda: "
                    f"np.zeros({shape}, dtype={dtype}, order=\"F\"))")
        if single:
            return f"{self.name}: np.float32 = np.float32(0.0)"
        default = {"real": "float = 0.0", "integer": "int = 0", "logical": "bool = False", "complex": "complex = 0j",
                   "character": "str = ''"}[sym.type]
        return f"{self.name}: {default}"

    def to_cpp(self) -> str:
        r"""Emit modern C++ declaration."""
        if self.symbol is not None:
            return self._resolved_cpp()
        type_map = {
            "REAL*8": "double",
            "DOUBLE PRECISION": "double",
            "REAL": "float",
            "REAL*4": "float",
            "INTEGER": "int",
            "INTEGER*4": "int",
            "INTEGER*2": "short",
            "LOGICAL": "bool",
            "COMPLEX*16": "std::complex<double>",
            "COMPLEX*8": "std::complex<float>"
        }
        cpp_base = type_map.get(self.f_type, "double")

        if self.dimensions:
            dims = [d.strip() for d in self.dimensions.split(',')]
            if len(dims) == 1:
                return f"std::array<{cpp_base}, {dims[0]}> {self.name};"
            elif len(dims) == 2:
                return f"std::array<std::array<{cpp_base}, {dims[1]}>, {dims[0]}> {self.name};"
            else:
                return f"std::vector<{cpp_base}> {self.name}; /* dims: {self.dimensions} */"
        return f"{cpp_base} {self.name}{{0}};"

    def to_python(self) -> str:
        r"""Emit Python dataclass field."""
        if self.symbol is not None:
            return self._resolved_python()
        if self.dimensions:
            dims = [d.strip() for d in self.dimensions.split(',')]
            shape_str = f"({', '.join(dims)})" if len(dims) > 1 else f"({dims[0]},)"
            return f"{self.name}: np.ndarray = field(default_factory=lambda: np.zeros({shape_str}, dtype=np.float64))"
        else:
            py_type = "float = 0.0" if "REAL" in self.f_type or "DOUBLE" in self.f_type else "int = 0"
            if "LOGICAL" in self.f_type:
                py_type = "bool = False"
            return f"{self.name}: {py_type}"


class CommonBlock:
    def __init__(self, name: str):
        self.name = name
        self.struct_name = "".join(part.capitalize() for part in name.split('_')) + "Block"
        self.variables: List[CommonBlockVariable] = []

    def add_variable(self, name: str, f_type: str = "REAL*8", dimensions: str = ""):
        if any(v.name == name.lower() for v in self.variables):
            return                       # the same member declared again (another unit): one field
        self.variables.append(CommonBlockVariable(name, f_type, dimensions))

    def emit_cpp_struct(self) -> str:
        r"""Generate C++ struct definition."""
        lines = [
            f"// Encapsulated Fortran COMMON block: /{self.name}/",
            f"struct {self.struct_name} {{"
        ]
        for v in self.variables:
            lines.append(f"    {v.to_cpp()}")
        lines.append("};")
        return "\n".join(lines)

    def emit_python_dataclass(self) -> str:
        r"""Generate Python dataclass definition."""
        lines = [
            f"# Encapsulated Fortran COMMON block: /{self.name}/",
            "@dataclass",
            f"class {self.struct_name}:"
        ]
        if not self.variables:
            lines.append("    pass")
        for v in self.variables:
            lines.append(f"    {v.to_python()}")
        return "\n".join(lines)


def blocks_from_units(units) -> Dict[str, CommonBlock]:
    r"""COMMON blocks laid out from analysed units: members associate by position (see link_common_blocks) and keep the
    declared or implicit type and kind (REAL is single precision) and their constant shape."""
    from fortran_semantics import common_extents, link_common_blocks
    blocks: Dict[str, CommonBlock] = {}
    for name, (unit, names) in link_common_blocks(units).items():
        block = blocks[name] = CommonBlock(name)
        for member in names:
            sym = unit.symbols[member]
            extents = common_extents(sym, unit) if sym.rank else None
            block.variables.append(CommonBlockVariable(member, symbol=sym, extents=extents))
    return blocks


def analyze_common_blocks(statements: List[FortranStatement], units=None) -> Dict[str, CommonBlock]:
    r"""Extract and type-analyze all COMMON blocks. With `units` (from fortran_semantics.lex_and_split) members are
    associated by position and typed from the symbol tables; without, they are matched by name from the statements."""
    if units is not None:
        return blocks_from_units(units)
    common_blocks: Dict[str, CommonBlock] = {}
    known_types: Dict[str, str] = {}
    known_dims: Dict[str, str] = {}

    # Pass 1: Collect types and dimensions
    for s in statements:
        if s.stmt_type == "TYPE_DECL":
            t_name = s.details["type"]
            decl_str = s.details["declarations"]
            # Parse commas not inside parentheses
            parts = re.split(r',\s*(?![^()]*\))', decl_str)
            for p in parts:
                p = p.strip()
                m_dim = re.match(r'^([A-Za-z0-9_]+)\s*\(([^)]+)\)$', p)
                if m_dim:
                    v_name = m_dim.group(1).upper()
                    known_types[v_name] = t_name
                    known_dims[v_name] = m_dim.group(2)
                else:
                    known_types[p.upper()] = t_name

        elif s.stmt_type == "DIMENSION":
            raw = s.details["raw"]
            parts = re.split(r',\s*(?![^()]*\))', raw)
            for p in parts:
                p = p.strip()
                m_dim = re.match(r'^([A-Za-z0-9_]+)\s*\(([^)]+)\)$', p)
                if m_dim:
                    v_name = m_dim.group(1).upper()
                    known_dims[v_name] = m_dim.group(2)

    # Pass 2: Extract COMMON blocks
    for s in statements:
        if s.stmt_type == "COMMON":
            block_name = s.details["block"].upper()
            if block_name not in common_blocks:
                common_blocks[block_name] = CommonBlock(block_name)

            block = common_blocks[block_name]
            for v in s.details["variables"]:
                v_clean = v.strip()
                m_inline_dim = re.match(r'^([A-Za-z0-9_]+)\s*\(([^)]+)\)$', v_clean)
                if m_inline_dim:
                    v_name = m_inline_dim.group(1).upper()
                    dims = m_inline_dim.group(2)
                else:
                    v_name = v_clean.upper()
                    dims = known_dims.get(v_name, "")

                f_type = known_types.get(v_name, "REAL*8")
                block.add_variable(v_name, f_type, dims)

    return common_blocks


if __name__ == "__main__":
    test_code = """      SUBROUTINE PROPAGATE
      DOUBLE PRECISION ECC, SEMI, INC, MASS
      INTEGER*4 STEP_ID
      DIMENSION POS(3), VEL(3)
      COMMON /ORBIT/ ECC, SEMI, INC, POS, VEL
      COMMON /PHYSICS/ MASS, STEP_ID
      END"""
    stmts = lex_fortran_source(test_code)
    blocks = analyze_common_blocks(stmts)
    for name, blk in blocks.items():
        print("=" * 60)
        print(blk.emit_cpp_struct())
        print("\n" + blk.emit_python_dataclass())
