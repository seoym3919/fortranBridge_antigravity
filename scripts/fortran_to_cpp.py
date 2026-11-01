#!/usr/bin/env python3
r"""
fortran_to_cpp.py - Automated Fortran-to-Modern-C++ Code Emitter.

Converts (structured Fortran 66/77/90/95/2003 subset, see fortran_semantics.py):
- SUBROUTINE / FUNCTION / PROGRAM units -> C++ free functions (several units per file).
- Types from declarations and implicit typing: INTEGER -> int, REAL/DOUBLE PRECISION -> double,
  LOGICAL -> bool. Integer division truncates exactly as in Fortran.
- 1-based subscripts -> 0-based std::vector indexing with constant folding (A(I-1) -> a[i - 2]).
  Rank >= 2 arrays are nested std::vector (row-major, jagged); non-unit lower bounds are honoured.
- DO loops keep the Fortran loop variable value (subscripts carry the -1 shift). Loop bounds are
  re-evaluated on every iteration (Fortran evaluates them once on entry).
- Scalar dummy arguments that the routine modifies become references; arrays are references.
- COMMON blocks -> structs passed by reference. GO TO / computed GO TO / arithmetic IF -> goto
  (all locals are declared at the top of each function so jumps never cross an initialisation).
- Intrinsics (DSQRT, MAX, MOD, NINT, ...) -> <cmath> / <algorithm>.

Anything outside the supported subset (DATA, EQUIVALENCE, array sections, formatted I/O, ...) is
emitted as `throw std::logic_error(...)` at that point -- never dropped silently.
The output has been checked for structure by the test-suite but needs a C++17 compiler to build.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from common_block_analyzer import analyze_common_blocks
from fortran_emit_base import ARRAY_REDUCERS, ARRAY_RESULT, UnitEmitter
from fortran_intrinsics import FORTRAN_INTRINSICS, STANDARD_INTRINSIC_NAMES, translate_intrinsic
from fortran_semantics import (
    CHAR, COMPLEX, INT, LOGICAL, REAL,
    ArrayCtor, Binary, Component, ComplexLit, DERIVED, FortranParseError, ImpliedDo, Logical, Name, Num, Ref, Slice, Str,
    Symbol, Unary, component_of, const_int, parse_expression,
    KIND_INQUIRY, add_const, common_extents, function_result_types, kind_call_value, lex_and_split, num_bits,
)

_CPP_RESERVED = {
    "alignas", "alignof", "and", "asm", "auto", "bool", "break", "case", "catch", "char", "class", "const",
    "constexpr", "continue", "decltype", "default", "delete", "do", "double", "else", "enum", "explicit",
    "export", "extern", "false", "float", "for", "friend", "goto", "if", "inline", "int", "long", "main",
    "mutable", "namespace", "new", "noexcept", "not", "nullptr", "operator", "or", "private", "protected",
    "public", "register", "return", "short", "signed", "sizeof", "static", "struct", "switch", "template",
    "this", "throw", "true", "try", "typedef", "typename", "union", "unsigned", "using", "virtual", "void",
    "volatile", "while", "xor", "std",
}
_CPPTYPE = {INT: "int", REAL: "double", LOGICAL: "bool", CHAR: "std::string", COMPLEX: "std::complex<double>"}


def ctype(sym) -> str:
    """C++ type of a symbol: REAL is float, DOUBLE PRECISION is double, INTEGER is int (32) or long long (64)."""
    if sym.type in (INT, REAL) and sym.bits < 0:
        raise FortranParseError(f"kind of {sym.name} could not be resolved")
    if sym.type == REAL and sym.bits == 32:
        return "float"
    if sym.type == INT and sym.bits > 32:
        return "long long"
    if sym.type == COMPLEX:
        return "std::complex<float>" if sym.bits == 32 else "std::complex<double>"
    if sym.type == DERIVED:
        return f"{sym.derived}_t"
    return _CPPTYPE[sym.type]

_P_OR, _P_AND, _P_EQ, _P_REL, _P_ADD, _P_MUL, _P_UNARY, _P_ATOM = 1, 2, 4, 5, 6, 7, 8, 10
_BIN_PREC = {"or": _P_OR, "and": _P_AND, "eqv": _P_EQ, "neqv": _P_EQ, "==": _P_EQ, "!=": _P_EQ,
             "<": _P_REL, "<=": _P_REL, ">": _P_REL, ">=": _P_REL, "+": _P_ADD, "-": _P_ADD, "//": _P_ADD, "*": _P_MUL, "/": _P_MUL}
_CPP_OP = {"and": "&&", "or": "||", "eqv": "==", "neqv": "!="}


_IPOW_HELPER = [
    "// INTEGER ** INTEGER: a negative exponent gives 0 unless the base is 1 or -1 (1 / a**-b truncates).",
    "inline long long fortran_ipow(long long a, long long b) {",
    "    if (b >= 0) {",
    "        unsigned long long result = 1, base = static_cast<unsigned long long>(a);",
    "        for (; b > 0; b >>= 1) {",
    "            if (b & 1) result *= base;",
    "            base *= base;",
    "        }",
    "        return static_cast<long long>(result);",
    "    }",
    "    if (a == 1) return 1;",
    "    if (a == -1) return (b % 2 == 0) ? 1 : -1;",
    "    if (a == 0) throw std::domain_error(\"0 ** negative integer\");",
    "    return 0;",
    "}",
]


_POWI_HELPER = [
    "// REAL ** INTEGER by repeated squaring, as libgfortran does (std::pow rounds differently in the last bit).",
    "template <class T>",
    "inline T fortran_powi(T x, long long n) {",
    "    unsigned long long u = n < 0 ? 0ULL - static_cast<unsigned long long>(n) : static_cast<unsigned long long>(n);",
    "    T p = 1;",
    "    for (;;) {",
    "        if (u & 1) p *= x;",
    "        u >>= 1;",
    "        if (!u) break;",
    "        x *= x;",
    "    }",
    "    return n < 0 ? T(1) / p : p;",
    "}",
]

_CHAR_HELPER = [
    "// CHARACTER(n): assignment truncates or pads with blanks; comparison pads the shorter operand; TRIM drops trailing blanks.",
    "inline std::string fortran_fix(const std::string& s, std::size_t n) { std::string r = s.substr(0, n); r.resize(n, ' '); return r; }",
    "inline std::string fortran_trim(const std::string& s) { std::size_t n = s.find_last_not_of(' '); return n == std::string::npos ? std::string() : s.substr(0, n + 1); }",
    "inline int fortran_cmp(std::string a, std::string b) {",
    "    std::size_t n = std::max(a.size(), b.size());",
    "    a.resize(n, ' '); b.resize(n, ' ');",
    "    return a < b ? -1 : (a > b ? 1 : 0);",
    "}",
]

_CDIV_HELPER = [
    "// COMPLEX division by Smith's algorithm in the precision of the operands (what libgcc's __divsc3 / __divdc3 compute).",
    "template <class T>",
    "inline std::complex<T> fortran_cdiv(std::complex<T> a, std::complex<T> b) {",
    "    T ar = a.real(), ai = a.imag(), br = b.real(), bi = b.imag();",
    "    if (std::abs(br) >= std::abs(bi)) {",
    "        T r = bi / br, d = br + bi * r;",
    "        return std::complex<T>((ar + ai * r) / d, (ai - ar * r) / d);",
    "    }",
    "    T r = br / bi, d = br * r + bi;",
    "    return std::complex<T>((ar * r + ai) / d, (ai * r - ar) / d);",
    "}",
]

# formatted output: the runtime is a real header (fortran_format_runtime.hpp, tested against gfortran) embedded verbatim
_FORMAT_RUNTIME = Path(__file__).with_name("fortran_format_runtime.hpp").read_text(encoding="utf-8").rstrip("\n").split("\n")
_IO_RUNTIME = Path(__file__).with_name("fortran_io_runtime.hpp").read_text(encoding="utf-8").rstrip("\n").split("\n")
_MATMUL_RUNTIME = Path(__file__).with_name("fortran_matmul_runtime.hpp").read_text(encoding="utf-8").rstrip("\n").split("\n")

_TRANSPOSE_HELPER = [
    "template <class T>",
    "inline std::vector<std::vector<T>> fortran_transpose(const std::vector<std::vector<T>>& a) {",
    "    std::size_t n = a.size(), m = a.empty() ? 0 : a[0].size();",
    "    std::vector<std::vector<T>> t(m, std::vector<T>(n));",
    "    for (std::size_t i = 0; i < n; ++i)",
    "        for (std::size_t j = 0; j < m; ++j) t[j][i] = a[i][j];",
    "    return t;",
    "}",
]

_MODULO_HELPER = [
    "// MODULO(a, b) takes the sign of b (MOD takes the sign of a).",
    "template <class T>",
    "inline T fortran_modulo(T a, T b) {",
    "    T r;",
    "    if constexpr (std::is_integral<T>::value) r = a % b; else r = std::fmod(a, b);",
    "    return (r != 0 && ((r < 0) != (b < 0))) ? r + b : r;",
    "}",
]


def cppname(name: str) -> str:
    name = name.lower()
    return name + "_" if name in _CPP_RESERVED else name


def _wrap(text: str, prec: int, minimum: int) -> str:
    return f"({text})" if prec < minimum else text


def _cstr(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def vec_type(elem: str, rank: int) -> str:
    t = f"std::vector<{elem}>"
    for _ in range(rank - 1):
        t = f"std::vector<{t}>"
    return t


def blk_var(block: str) -> str:
    return f"{block.lower()}_common"


class CppUnitEmitter(UnitEmitter):
    def __init__(self, unit, units, func_types, blocks, headers: Set[str]):
        super().__init__(unit, units, func_types)
        self.blocks_info = blocks
        self.headers = headers
        self.uses_untranslated_helper = False
        self.helpers: Set[str] = set()
        self.kind_problems: List[Tuple[str, str]] = []
        self.prototype: Optional[str] = None       # forward declaration of this unit (None for the main program)
        self.tmp_counter = 0

    def bits(self, e) -> int:
        return num_bits(e, self.unit, self.func_types, self.units)

    def _reject_sequence_association(self, callee, args) -> None:
        """An array element given for an array dummy shares storage from that element on; nested std::vectors have no
        such contiguous storage, so say so instead of emitting code that does not compile."""
        if callee is None:
            return
        for pos, a in enumerate(args):
            if pos >= len(callee.params):
                break
            formal = callee.symbols.get(callee.params[pos])
            actual = self.unit.symbols.get(getattr(a, "name", ""))
            if (formal is not None and formal.rank and isinstance(a, Ref) and actual is not None and actual.rank
                    and not any(isinstance(x, Slice) for x in a.args)):
                raise FortranParseError("an array element passed for an array dummy argument (sequence association) is "
                                        "not translated to C++")

    def ctype_or_fail(self, sym) -> str:
        """ctype(sym); an unresolved kind becomes a runtime throw in the unit (fallback type double/int), not a crash."""
        try:
            return ctype(sym)
        except FortranParseError as exc:
            self.kind_problems.append((sym.name, str(exc)))
            return "int" if sym.type == INT else "double"

    # ------------------------------------------------------------------ expressions
    def ex(self, e) -> Tuple[str, int]:
        if isinstance(e, Num):
            if e.is_int:
                return (e.text + "LL" if self.bits(e) > 32 else e.text), _P_ATOM
            text = e.text if re.search(r"[.eE]", e.text) else e.text + ".0"      # 1d0 -> "1": keep it a real
            return (text + "f" if self.bits(e) == 32 else text), _P_ATOM
        if isinstance(e, Logical):
            return ("true" if e.value else "false"), _P_ATOM
        if isinstance(e, Str):
            return _cstr(e.value), _P_ATOM
        if isinstance(e, Name):
            return self.name_ref(e.name), _P_ATOM
        if isinstance(e, Ref):
            return self.ref(e)
        if isinstance(e, Slice):
            raise FortranParseError("array sections are not translated to C++")
        if isinstance(e, Unary):
            text, prec = self.ex(e.operand)
            if e.op == "not":
                return "!" + _wrap(text, prec, _P_UNARY), _P_UNARY
            text = _wrap(text, prec, _P_MUL)
            if text.startswith(e.op):          # avoid '--x' / '++x'
                text = f"({text})"
            return e.op + text, (_P_UNARY if prec >= _P_UNARY else _P_MUL)
        if isinstance(e, Binary):
            return self.binary(e)
        if isinstance(e, ArrayCtor):
            return self.array_ctor(e), _P_ATOM
        if isinstance(e, Component):
            return self.component_text(e), _P_ATOM
        if isinstance(e, ComplexLit):
            part = "float" if self.bits(e) == 32 else "double"
            return f"std::complex<{part}>({self.ex(e.re)[0]}, {self.ex(e.im)[0]})", _P_ATOM
        if isinstance(e, ImpliedDo):
            raise FortranParseError("an implied DO is only allowed in an array constructor or an output list")
        raise FortranParseError(f"cannot translate {e!r}")

    def component_text(self, e: Component) -> str:
        comp = component_of(e, self.unit)
        if comp is None:
            raise FortranParseError(f"unknown component {e.comp}")
        base = self.ex(e.base)[0]
        if not e.args:
            return f"{base}.{e.comp}"
        view = Symbol(e.comp, type=comp.type, bits=comp.bits, dims=comp.dims)
        return f"{base}.{e.comp}{self.subscripts(view, e.args)}"

    def structure_constructor(self, e: Ref) -> str:
        typedef = self.unit.types[e.name]
        if len(e.args) > len(typedef.components):
            raise FortranParseError(f"too many components for {e.name}")
        parts = []
        for comp, arg in zip(typedef.components, e.args):
            text = self.ex(arg)[0]
            if comp.dims or comp.type == DERIVED:
                parts.append(text)
            else:
                parts.append(f"static_cast<{self._elem_type(comp.type, comp.bits)}>({text})")
        return f"{e.name}_t{{{', '.join(parts)}}}"

    # ------------------------------------------------------------------ array constructors
    def _elem_type(self, t: str, width: int) -> str:
        return {INT: "long long" if width > 32 else "int", REAL: "float" if width == 32 else "double",
                LOGICAL: "bool", CHAR: "std::string",
                COMPLEX: "std::complex<float>" if width == 32 else "std::complex<double>"}[t]

    def array_ctor(self, e: ArrayCtor) -> str:
        """std::vector built from a constructor; implied DOs and nested arrays need a lambda that fills it."""
        t, width = self.type_of(e), self.bits(e)
        elem = self._elem_type(t, width)
        plain = all(not isinstance(i, ImpliedDo) and not self.array_valued(i) for i in e.items)
        if plain:
            return f"std::vector<{elem}>{{{', '.join(f'static_cast<{elem}>({self.ex(i)[0]})' for i in e.items)}}}"
        lines = [f"std::vector<{elem}> r;"] + [l for item in e.items for l in self._ctor_push(item, elem)] + ["return r;"]
        return "[&]() { " + " ".join(lines) + " }()"

    def _ctor_push(self, item, elem: str) -> List[str]:
        if isinstance(item, ImpliedDo):
            var = cppname(item.var)
            lo, hi = self.ex(item.lo)[0], self.ex(item.hi)[0]
            step = "1" if item.step is None else self.ex(item.step)[0]
            cmp_op = "<=" if item.step is None or not (isinstance(item.step, Unary) and item.step.op == "-") else ">="
            body = [l for x in item.items for l in self._ctor_push(x, elem)]
            return [f"for (int {var} = {lo}; {var} {cmp_op} {hi}; {var} += {step}) {{"] + body + ["}"]
        if isinstance(item, ArrayCtor):
            return [f"{{ auto part = {self.array_ctor(item)}; r.insert(r.end(), part.begin(), part.end()); }}"]
        if self.array_valued(item):
            return [f"{{ auto part = {self.ex(item)[0]}; r.insert(r.end(), part.begin(), part.end()); }}"]
        return [f"r.push_back(static_cast<{elem}>({self.ex(item)[0]}));"]

    def _simple(self, e) -> bool:
        return isinstance(e, (Name, Num)) or (isinstance(e, Ref) and self.unit.symbols.get(e.name) is not None
                                              and self.unit.symbols[e.name].rank > 0)

    def binary(self, e: Binary) -> Tuple[str, int]:
        op = e.op
        if op == "**":
            self.headers.add("<cmath>")
            lt, _ = self.ex(e.left)
            exponent = e.right
            both_int = self.type_of(e.left) == INT and self.type_of(exponent) == INT
            if isinstance(exponent, Num) and exponent.is_int and exponent.text in ("2", "3") and self._simple(e.left):
                n = int(exponent.text)
                return "(" + " * ".join([lt] * n) + ")", _P_ATOM
            rt, _ = self.ex(exponent)
            if both_int:
                self.helpers.add("fortran_ipow")
                result = "long long" if self.bits(e) > 32 else "int"
                return f"static_cast<{result}>(fortran_ipow({lt}, {rt}))", _P_ATOM
            if self.type_of(e.left) == REAL and self.type_of(exponent) == INT:
                self.helpers.add("fortran_powi")      # REAL ** INTEGER multiplies by repeated squaring, as libgfortran does
                return f"fortran_powi({lt}, {rt})", _P_ATOM
            return f"std::pow({lt}, {rt})", _P_ATOM
        prec = _BIN_PREC[op]
        lt, lp = self.ex(e.left)
        rt, rp = self.ex(e.right)
        if op == "//":
            return f"(std::string({lt}) + std::string({rt}))", _P_ATOM
        if op in ("==", "!=", "<", "<=", ">", ">=") and COMPLEX not in (self.type_of(e.left), self.type_of(e.right)) \
                and CHAR in (self.type_of(e.left), self.type_of(e.right)):
            self.helpers.add("fortran_char")
            return f"(fortran_cmp(std::string({lt}), std::string({rt})) {op} 0)", _P_ATOM
        if self.type_of(e) == COMPLEX or (op in ("==", "!=") and COMPLEX in (self.type_of(e.left), self.type_of(e.right))):
            width = max([self.bits(x) for x in (e.left, e.right) if self.type_of(x) in (REAL, COMPLEX)] or [32])
            part = "float" if width == 32 else "double"
            cx = f"std::complex<{part}>"
            if not (self.type_of(e.left) == COMPLEX and self.bits(e.left) == width):
                lt, lp = f"{cx}({lt})", _P_ATOM
            if not (self.type_of(e.right) == COMPLEX and self.bits(e.right) == width):
                rt, rp = f"{cx}({rt})", _P_ATOM
            if op == "/":
                self.helpers.add("fortran_cdiv")      # Smith's algorithm, as libgcc's __divsc3 / __divdc3 (no library dependence)
                return f"fortran_cdiv({lt}, {rt})", _P_ATOM
        sym = _CPP_OP.get(op, op)
        if prec in (_P_EQ, _P_REL):
            return f"{_wrap(lt, lp, prec + 1)} {sym} {_wrap(rt, rp, prec + 1)}", prec
        return f"{_wrap(lt, lp, prec)} {sym} {_wrap(rt, rp, prec + 1)}", prec

    def name_ref(self, name: str) -> str:
        sym = self.unit.symbols.get(name)
        if sym is not None and sym.pointer:
            return f"(*{cppname(name)})"                 # a pointer: what it points to
        if sym is not None and sym.common_block:
            return f"{blk_var(sym.common_block)}.{sym.common_field or name}"
        if self.unit.kind == "FUNCTION" and name == self.unit.name:
            return cppname(name) + "_result"
        return cppname(name)

    def _shift(self, idx, lb_text: str):
        lb = self.parse(lb_text)
        if isinstance(lb, Num) and lb.is_int:
            return add_const(idx, -int(lb.text))
        return Binary("-", idx, lb)

    def subscripts(self, sym, args) -> str:
        if len(args) != sym.rank:
            raise FortranParseError(f"array {sym.name} used with {len(args)} subscripts, declared rank {sym.rank}")
        parts = []
        for (lb, _ub), a in zip(sym.dims, args):
            if isinstance(a, Slice):
                raise FortranParseError("array sections are not translated to C++")
            parts.append(f"[{self.ex(self._shift(a, lb))[0]}]")
        return "".join(parts)

    def ref(self, e: Ref) -> Tuple[str, int]:
        sym = self.unit.symbols.get(e.name)
        user = self.units.get(e.name)
        if sym is not None and sym.rank:
            return f"{self.name_ref(e.name)}{self.subscripts(sym, e.args)}", _P_ATOM
        if e.name in self.unit.types and sym is None and not user:
            return self.structure_constructor(e), _P_ATOM
        key = e.name.upper()
        if key in KIND_INQUIRY and not user:
            return str(kind_call_value(e, self.unit, self.func_types, self.units)), _P_ATOM
        if key in FORTRAN_INTRINSICS and not (user and user.kind == "FUNCTION"):
            return self.intrinsic(key, e), _P_ATOM
        if key in STANDARD_INTRINSIC_NAMES and not user:
            raise FortranParseError(f"intrinsic {key} is not translated")
        self._reject_sequence_association(user, e.args)
        args = [self.ex(a)[0] for a in e.args]
        if user:
            args += [blk_var(b) for b in user.passed_commons()]
        return f"{cppname(e.name)}({', '.join(args)})", _P_ATOM

    def _needs_value(self, a) -> bool:
        """An array expression or section (not a plain array, constructor or MATMUL) used as an argument: evaluate it first."""
        if not self.array_valued(a) or isinstance(a, (Name, ArrayCtor)):
            return False
        return not (isinstance(a, Ref) and a.name.upper() in ARRAY_RESULT)

    def array_value_text(self, e) -> str:
        """C++ expression for the value of an array expression: a lambda that fills a std::vector element by element."""
        self.tmp_counter += 1
        ctx = {"id": self.tmp_counter, "rank": None, "counts": None}
        temps: List[str] = []
        elem = self._elem(e, ctx, temps)
        rank, counts = ctx["rank"], ctx["counts"]
        t = self._elem_type(self.type_of(e), self.bits(e))
        if rank == 1:
            decl = f"std::vector<{t}> t(static_cast<std::size_t>({counts[0]}));"
        elif rank == 2:
            decl = f"std::vector<std::vector<{t}>> t({counts[0]}, std::vector<{t}>({counts[1]}));"
        else:
            raise FortranParseError("array expressions of rank above 2 are not translated to C++")
        ks = [f"k{ctx['id']}_{d}" for d in range(rank)]
        loops = "".join(f"for (int {k} = 0; {k} < {c}; ++{k}) {{ " for k, c in zip(ks, counts))
        store = f"t{''.join(f'[{k}]' for k in ks)} = static_cast<{t}>({self.ex(elem)[0]});"
        return "[&]() { " + " ".join(temps) + f" {decl} {loops}{store} {'} ' * rank}return t; }}()"

    def intrinsic(self, key: str, e: Ref) -> str:
        strs = [self.array_value_text(a) if self._needs_value(a) else self.ex(a)[0] for a in e.args]
        types = [self.type_of(a) for a in e.args]
        if key == "MOD" and len(strs) == 2 and all(t == INT for t in types):
            return f"(({strs[0]}) % ({strs[1]}))"
        arg_complex = bool(e.args) and types and types[0] == COMPLEX
        if key in ("REAL", "DBLE", "DREAL") and len(strs) == 1 and arg_complex:
            return f"static_cast<{'double' if key != 'REAL' or self.bits(e) > 32 else 'float'}>(({strs[0]}).real())"
        if key in ("AIMAG", "DIMAG") and len(strs) == 1:
            return f"static_cast<{'double' if key == 'DIMAG' or self.bits(e) > 32 else 'float'}>(({strs[0]}).imag())"
        if key in ("CMPLX", "DCMPLX") and 1 <= len(strs) <= 3:
            part = "float" if self.bits(e) == 32 else "double"
            if len(strs) == 1 and arg_complex:
                return f"std::complex<{part}>({strs[0]})"
            imag = strs[1] if len(strs) >= 2 else "0"
            return f"std::complex<{part}>(static_cast<{part}>({strs[0]}), static_cast<{part}>({imag}))"
        if key == "ASSOCIATED" and len(e.args) == 1 and isinstance(e.args[0], Name):
            return f"({cppname(e.args[0].name)} != nullptr)"
        if key == "SIZE" and e.args and isinstance(e.args[0], Name) and self.unit.symbols.get(e.args[0].name) is not None \
                and self.unit.symbols[e.args[0].name].rank:
            arr, rank = strs[0], self.unit.symbols[e.args[0].name].rank
            if len(strs) == 2:
                dim = const_int(self.parse_text(e.args[1]), self.unit) if False else None
                dim_arg = e.args[1]
                if not (isinstance(dim_arg, Num) and dim_arg.is_int):
                    raise FortranParseError("SIZE with a non-constant DIM is not translated to C++")
                d = int(dim_arg.text)
                return f"static_cast<int>({arr}{'[0]' * (d - 1)}.size())"
            return "static_cast<int>(" + " * ".join(f"{arr}{'[0]' * k}.size()" for k in range(rank)) + ")"
        if key in ("TRIM", "LEN_TRIM"):
            self.helpers.add("fortran_char")
            return (f"fortran_trim(std::string({strs[0]}))" if key == "TRIM"
                    else f"static_cast<int>(fortran_trim(std::string({strs[0]})).size())")
        if key == "MODULO" and len(strs) == 2:
            self.helpers.add("fortran_modulo")
            return f"fortran_modulo({strs[0]}, {strs[1]})"
        if key == "SIGN" and len(strs) == 2 and all(t == INT for t in types):
            key = "ISIGN"
        if key == "MATMUL" and len(strs) == 2:
            self.helpers.add("fortran_matmul")
            return f"fortran_matmul({strs[0]}, {strs[1]})"
        if key == "TRANSPOSE" and len(strs) == 1:
            self.helpers.add("fortran_transpose")
            return f"fortran_transpose({strs[0]})"
        if key in ("SUM", "PRODUCT", "DOT_PRODUCT"):
            # the accumulator has the element type: std::accumulate(..., 0.0) would add a float array in double
            kind = self.type_of(e)
            one = key == "PRODUCT"
            init = ("1" if one else "0") if kind == INT else (("1.0" if one else "0.0") + ("f" if self.bits(e) == 32 else ""))
            self.headers.add("<numeric>")
            args = []
            lets = ""
            for k, text in enumerate(strs):       # an expression argument is evaluated once, into a named temporary
                if re.fullmatch(r"[\w:]+(?:\[[^\]]*\])*", text):
                    args.append(text)
                else:
                    lets += f"auto a{k} = {text}; "
                    args.append(f"a{k}")
            if key == "SUM":
                call = f"std::accumulate({args[0]}.begin(), {args[0]}.end(), {init})"
            elif key == "PRODUCT":
                call = f"std::accumulate({args[0]}.begin(), {args[0]}.end(), {init}, std::multiplies<>())"
            else:
                call = f"std::inner_product({args[0]}.begin(), {args[0]}.end(), {args[1]}.begin(), {init})"
            return f"[&]() {{ {lets}return {call}; }}()" if lets else call
        if key in ("EPSILON", "HUGE", "TINY") and len(e.args) == 1:
            self.headers.add("<limits>")
            elem = {INT: "long long" if self.bits(e.args[0]) > 32 else "int",
                    REAL: "float" if self.bits(e.args[0]) == 32 else "double"}.get(self.type_of(e.args[0]))
            if elem is None:
                raise FortranParseError(f"{key} of this argument type is not translated")
            member = {"EPSILON": "epsilon()", "HUGE": "max()", "TINY": "min()"}[key]
            return f"std::numeric_limits<{elem}>::{member}"
        if key == "REAL" and len(strs) == 1:
            return f"static_cast<{'double' if self.bits(e) > 32 else 'float'}>({strs[0]})"
        if key in ("MAX", "MIN", "AMAX1", "AMIN1", "DMAX1", "DMIN1") and len(set(types)) > 1:
            strs = [f"static_cast<double>({s})" if t == INT else s for s, t in zip(strs, types)]
        code, header = translate_intrinsic(key, strs, target="cpp")
        if header:
            self.headers.add(header)
        return code

    # ------------------------------------------------------------------ l-values and conversion
    def lvalue(self, lhs) -> str:
        if isinstance(lhs, Component):
            return self.component_text(lhs)
        if isinstance(lhs, Name):
            return self.name_ref(lhs.name)
        if isinstance(lhs, Ref):
            sym = self.unit.symbols.get(lhs.name)
            if sym is not None and sym.rank:
                return f"{self.name_ref(lhs.name)}{self.subscripts(sym, lhs.args)}"
        raise FortranParseError("assignment target is not a variable or array element")

    def lvalue_or_none(self, e) -> Optional[str]:
        if isinstance(e, Component):
            return self.lvalue(e)
        sym = self.unit.symbols.get(getattr(e, "name", ""))
        if sym is None or sym.const_value is not None:
            return None
        try:
            if isinstance(e, Name) and not sym.rank:
                return self.lvalue(e)
            if isinstance(e, Ref) and sym.rank:
                return self.lvalue(e)
        except FortranParseError:
            return None
        return None

    def convert(self, text: str, expr, target_type: str, target_bits: int = 32) -> str:
        have = self.type_of(expr)
        if target_type == INT and have == REAL:
            return f"static_cast<{'long long' if target_bits > 32 else 'int'}>({text})"
        return text

    def returns_value(self) -> bool:
        return self.unit.kind == "FUNCTION"

    # ------------------------------------------------------------------ unit scaffolding
    def param_decl(self, p: str) -> str:
        sym = self.unit.symbols[p]
        elem = self.ctype_or_fail(sym)
        if sym.rank:
            const = "const " if sym.intent == "in" else ""
            return f"{const}{vec_type(elem, sym.rank)}& {cppname(p)}"
        ref = "&" if p in self.unit.modified_scalars or sym.type == DERIVED else ""
        return f"{elem}{ref} {cppname(p)}"

    def begin_unit(self):
        u = self.unit
        args = [self.param_decl(p) for p in u.params]
        owned = set(u.commons) if u.kind == "PROGRAM" else set(u.owned_blocks)
        args += [f"{self.blocks_info[b].struct_name}& {blk_var(b)}" for b in sorted(u.commons - owned)]
        if u.kind == "PROGRAM":
            self.line("int main() {")
            self.headers.add("<iomanip>")
            self.level = 1
            self.line("std::cout << std::setprecision(17);   // print every digit, so values can be compared exactly")
            self.level = 0
        else:
            ret = self.ctype_or_fail(u.symbols[u.name]) if u.kind == "FUNCTION" else "void"
            self.prototype = f"{ret} {cppname(u.name)}({', '.join(args)});"
            self.line(f"{ret} {cppname(u.name)}({', '.join(args)}) {{")
        self.level = 1
        self.line(f"// Translated from Fortran {u.kind} {u.name}.")
        self._declare_locals()
        for name, why in self.kind_problems:
            self.do_unsupported(why, name)
        for message in u.common_conflicts:
            self.do_unsupported(message, "COMMON")
        for message in u.static_problems:
            self.do_unsupported(message, "DATA/SAVE")

    def _declare_locals(self) -> None:
        u = self.unit
        aliases: List[str] = []
        consts = [(n, s) for n, s in u.symbols.items() if s.const_value is not None and not s.is_dummy and not s.module]
        for name, sym in consts:
            e = self.parse(sym.const_value)
            self.line(f"constexpr {self.ctype_or_fail(sym)} {cppname(name)} = {self.convert(self.ex(e)[0], e, sym.type, sym.bits)};")
        for name, sym in u.symbols.items():
            if sym.is_dummy or sym.common_block or sym.const_value is not None or sym.module:
                continue
            if u.kind == "FUNCTION" and name == u.name:
                continue
            if name in self.units and self.units[name].kind != "PROGRAM" and not sym.rank:
                continue   # a called function, not a variable
            elem = self.ctype_or_fail(sym)
            if sym.pointer and not sym.rank and not sym.saved:
                self.line(f"{elem}* {cppname(name)} = nullptr;")
                continue
            if (sym.allocatable or sym.pointer) and sym.rank and not sym.saved:
                vt = vec_type(elem, sym.rank)
                self.line(f"{vt}* {cppname(name)} = nullptr;" if sym.pointer else f"{vt} {cppname(name)};")
                continue
            if sym.eq is not None and not sym.rank:
                primary = u.eq_groups[sym.eq[0]]["members"][0]
                if name != primary:
                    aliases.append(f"{elem}& {cppname(name)} = {cppname(primary)};")      # EQUIVALENCE of scalars
                    continue
            if sym.saved:
                self.line(self.static_declaration(name, sym, elem))
                continue
            if sym.rank:
                if any(ub.strip() == "*" for _lb, ub in sym.dims):
                    self.do_unsupported("assumed-size local array", name)
                    continue
                extents = []
                for lb, ub in sym.dims:
                    lbe, ube = self.parse(lb), self.parse(ub)
                    ext = add_const(ube, 1 - int(lbe.text)) if isinstance(lbe, Num) and lbe.is_int \
                        else Binary("+", Binary("-", ube, lbe), Num("1", True))
                    extents.append(self.ex(ext)[0])
                self.line(self._array_decl(elem, cppname(name), extents))
            else:
                self.line(f"{elem} {cppname(name)}{{}};")
        for line in aliases:
            self.line(line)
        for group in u.eq_groups:
            if any(u.symbols[m].rank for m in group["members"]):
                self.do_unsupported("EQUIVALENCE of arrays is not translated to C++ (std::vector storage cannot overlap)",
                                    ", ".join(group["members"]))
        if u.kind == "FUNCTION":
            self.line(f"{self.ctype_or_fail(u.symbols[u.name])} {cppname(u.name)}_result{{}};")
        for blk in sorted(set(u.commons) if u.kind == "PROGRAM" else set(u.owned_blocks)):    # blocks the unit creates itself
            self.line(f"{self.blocks_info[blk].struct_name} {blk_var(blk)}{{}};")

    def module_declarations(self) -> List[str]:
        """File-scope declarations for a MODULE: its parameters (constexpr) and its variables (static storage)."""
        out = []
        for name, sym in self.unit.symbols.items():
            elem = self.ctype_or_fail(sym)
            if sym.const_value is not None:
                e = self.parse(sym.const_value)
                out.append(f"constexpr {elem} {cppname(name)} = {self.convert(self.ex(e)[0], e, sym.type, sym.bits)};")
            elif sym.saved:
                out.append(self.static_declaration(name, sym, elem))
        for message in self.unit.static_problems:
            self.do_unsupported(message, "MODULE")
            out.extend(l.strip() for l in self.lines[-1:])
        return out

    def static_declaration(self, name: str, sym, elem: str) -> str:
        """`static` local for a DATA / SAVE variable: it keeps its value between calls; without DATA it starts at zero."""
        values = self.unit.data_values.get(name)
        cname = cppname(name)
        if (sym.allocatable or sym.pointer) and sym.rank:
            vt = vec_type(elem, sym.rank)
            return f"static {vt}* {cname} = nullptr;" if sym.pointer else f"static {vt} {cname};"
        if not sym.rank:
            if values is None:
                return f"static {elem} {cname}{{}};"
            return f"static {elem} {cname} = {self.convert(self.ex(values[0])[0], values[0], sym.type, sym.bits)};"
        shape = common_extents(sym, self.unit)
        if values is None:
            return "static " + self._array_decl(elem, cname, [str(n) for n in shape])
        texts = [f"static_cast<{elem}>({self.ex(v)[0]})" for v in values]
        strides = [1]
        for n in shape[:-1]:
            strides.append(strides[-1] * n)

        def build(dim: int, base: int) -> str:                  # DATA fills in column-major order
            if dim == len(shape):
                return texts[base]
            return "{" + ", ".join(build(dim + 1, base + i * strides[dim]) for i in range(shape[dim])) + "}"
        return f"static {vec_type(elem, len(shape))} {cname} = {build(0, 0)};"

    @staticmethod
    def _array_decl(elem: str, name: str, extents: List[str]) -> str:
        """e.g. 'std::vector<std::vector<double>> a(n, std::vector<double>(m));'"""
        rank = len(extents)
        if rank == 1:
            return f"{vec_type(elem, 1)} {name}({extents[0]});"
        inner = f"{vec_type(elem, 1)}({extents[-1]})"
        for k in range(rank - 2, 0, -1):
            inner = f"{vec_type(elem, rank - k)}({extents[k]}, {inner})"
        return f"{vec_type(elem, rank)} {name}({extents[0]}, {inner});"

    def end_unit(self):
        while self.blocks:
            self.close_block()
        last = next((l for l in reversed(self.lines) if l.strip()), "")
        top_level_return = last.startswith(self.INDENT + "return") and not last.startswith(self.INDENT * 2)
        if self.unit.kind == "PROGRAM" and not top_level_return:
            self.line("return 0;")
        elif self.returns_value() and not top_level_return:
            self.line(f"return {self.name_ref(self.unit.name)};")
        self.level = 0
        self.line("}")
        self.line()

    # ------------------------------------------------------------------ declarations (done up front)
    def on_TYPE_DECL(self, d, raw):
        pass

    def on_DIMENSION(self, d, raw):
        pass

    def on_PARAMETER(self, d, raw):
        pass

    # ------------------------------------------------------------------ control flow hooks
    def _may_change(self, expr) -> bool:
        """Could the unit change any variable `expr` reads? Conservative: an array element or function call counts, a
        name counts when it is an assignment target, a DO variable or a CALL argument anywhere in the unit."""
        if not hasattr(self, "_assigned"):
            assigned: Set[str] = set()
            for st in self.unit.stmts:
                text = " ".join(filter(None, [st.raw, str(st.details.get("statement", ""))]))
                for m in re.finditer(r"(?:^|[\s)])([A-Za-z_]\w*)\s*(?:\([^=]*?\))?\s*=(?!=)", text):
                    assigned.add(m.group(1).lower())
                if st.stmt_type == "DO":
                    assigned.add(st.details["var"].lower())
                if st.stmt_type == "CALL":
                    for a in st.details["args"]:
                        assigned.update(n.lower() for n in re.findall(r"[A-Za-z_]\w*", a))
            self._assigned = assigned
        names: Set[str] = set()
        stack = [expr]
        while stack:
            e = stack.pop()
            if isinstance(e, Name):
                names.add(e.name)
            elif isinstance(e, Ref):
                return True
            elif isinstance(e, Unary):
                stack.append(e.operand)
            elif isinstance(e, Binary):
                stack += [e.left, e.right]
        return bool(names & self._assigned) or any(
            (self.unit.symbols.get(n) is not None and self.unit.symbols[n].common_block) for n in names)

    def _open_real_do(self, var, v, start, end, step):
        """DO with a REAL variable. Fortran: trip count max(int((end - start + step) / step), 0) in the variable's
        precision; the variable accumulates x = x + step and ends one step past the last iteration."""
        t = self.ctype_or_fail(self.unit.symbols[var.name])
        self.tmp_counter += 1
        k, n = f"do{self.tmp_counter}_k", f"do{self.tmp_counter}_n"
        s_t, e_t, st_t = (f"static_cast<{t}>({self.ex(x)[0]})" for x in (start, end, step))
        self.line(f"{v} = {s_t};")
        count = f"static_cast<long long>(({e_t} - {s_t} + {st_t}) / {st_t})"
        self.line(f"for (long long {k} = 0, {n} = std::max(0LL, {count}); {k} < {n}; ++{k}, {v} += {st_t}) {{")

    def open_do(self, var, start, end, step, raw):
        v = self.ex(var)[0]
        if self.type_of(var) == REAL and isinstance(var, Name):
            self._open_real_do(var, v, start, end, step)
            return
        s_txt = self.convert(self.ex(start)[0], start, self.type_of(var))
        e_txt, e_prec = self.ex(end)
        if not isinstance(end, Num) and not self.goto_targets and self._may_change(end):
            # Fortran fixes the trip count on entry: a bound changed inside the body must not change the loop.
            # (Not done in a unit that uses GO TO: a jump may not bypass the initialisation.)
            self.tmp_counter += 1
            bound = f"do{self.tmp_counter}_end"
            self.line(f"const auto {bound} = {e_txt};")
            e_txt, e_prec = bound, _P_ATOM
        step_lit = None
        if isinstance(step, Num) and step.is_int:
            step_lit = int(step.text)
        elif isinstance(step, Unary) and step.op == "-" and isinstance(step.operand, Num) and step.operand.is_int:
            step_lit = -int(step.operand.text)
        if step_lit == 0:
            self.line('throw std::logic_error("fortranBridge: DO loop with zero step");')
            self.line("for (;false;) {")
            return
        if step_lit is not None:
            cmp_op = "<=" if step_lit > 0 else ">="
            inc = (f"++{v}" if step_lit == 1 else f"--{v}" if step_lit == -1 else f"{v} += {step_lit}")
            self.line(f"for ({v} = {s_txt}; {v} {cmp_op} {_wrap(e_txt, e_prec, _P_REL + 1)}; {inc}) {{")
        else:
            st = self.ex(step)[0]
            if not isinstance(step, Num) and not self.goto_targets and self._may_change(step):
                self.tmp_counter += 1
                bound = f"do{self.tmp_counter}_step"
                self.line(f"const auto {bound} = {st};")
                st = bound
            self.line(f"for ({v} = {s_txt}; (({st}) > 0 ? {v} <= {e_txt} : {v} >= {e_txt}); {v} += {st}) {{")

    def open_while(self, cond, raw):
        if cond is None and raw.strip().upper() == "DO":
            self.line("while (true) {")
        else:
            self.line(f"while ({self._cond_text(cond, raw)}) {{")

    def open_if(self, cond, raw):
        self.line(f"if ({self._cond_text(cond, raw)}) {{")

    def open_elif(self, cond, raw):
        self.line(f"}} else if ({self._cond_text(cond, raw)}) {{")

    def open_else(self):
        self.line("} else {")

    def _cond_text(self, cond, raw) -> str:
        if cond is None:
            self.uses_untranslated_helper = True
            return f"fortran_untranslated({_cstr(raw)})"
        return self.ex(cond)[0]

    def finish_block(self, kind, empty):
        self.line("}")

    def finish_branch(self, empty):
        pass   # the next line ('} else ...') closes the branch

    # ------------------------------------------------------------------ simple statement hooks
    # ------------------------------------------------------------------ whole-array statements
    def _free_dims(self, e):
        """(lo, hi) Fortran index bounds of each free dimension of a whole array or an array section, else None."""
        if isinstance(e, Name):
            sym = self.unit.symbols.get(e.name)
            if sym is None or not sym.rank:
                return None
            return [(self.parse(lb), self._upper(sym, e.name, d, lb, ub)) for d, (lb, ub) in enumerate(sym.dims)]
        if isinstance(e, Ref):
            sym = self.unit.symbols.get(e.name)
            if sym is None or not sym.rank:
                return None
            dims = []
            for d, ((lb, ub), a) in enumerate(zip(sym.dims, e.args)):
                if isinstance(a, Slice):
                    dims.append(((a.lo if a.lo is not None else self.parse(lb)),
                                 (a.hi if a.hi is not None else self._upper(sym, e.name, d, lb, ub))))
            return dims or None
        return None

    def _upper(self, sym, name: str, d: int, lb: str, ub: str):
        """Upper bound of dimension d; an allocatable, pointer or assumed-shape array has none declared: its extent is
        read from the vector at run time."""
        if ub.strip() not in ("", "*"):
            return self.parse(ub)
        extent = Ref("size", [Name(name), Num(str(d + 1), True)])
        return Binary("-", Binary("+", self.parse(lb), extent), Num("1", True))

    def _count_text(self, lo, hi) -> str:
        return self.ex(Binary("+", Binary("-", hi, lo), Num("1", True)))[0]

    def _loop_var(self, ctx: dict, d: int) -> Name:
        name = f"k{ctx['id']}_{d}"
        self.unit.symbols.setdefault(name, Symbol(name, type=INT, bits=32))
        return Name(name)

    def _elem(self, e, ctx: dict, temps: List[str]):
        """Rewrite an array-valued expression into the expression for one element, indexed by loop variables
        k<id>_0, k<id>_1, ...; constructors and MATMUL/TRANSPOSE results are first computed into temporaries."""
        if not self.array_valued(e):
            return e
        if isinstance(e, (Name, Ref)) and self._free_dims(e) is not None:
            dims = self._free_dims(e)
            if ctx["rank"] is None:
                ctx["rank"], ctx["counts"] = len(dims), [self._count_text(lo, hi) for lo, hi in dims]
            elif ctx["rank"] != len(dims):
                raise FortranParseError("array operands of different rank are not conformable")
            if isinstance(e, Name):
                sym = self.unit.symbols[e.name]
                return Ref(e.name, [Binary("+", self.parse(lb), self._loop_var(ctx, d)) for d, (lb, _ub) in enumerate(sym.dims)])
            args, j = [], 0
            sym = self.unit.symbols[e.name]
            for (lb, _ub), a in zip(sym.dims, e.args):
                if isinstance(a, Slice):
                    low = a.lo if a.lo is not None else self.parse(lb)
                    args.append(Binary("+", low, self._loop_var(ctx, j)))
                    j += 1
                else:
                    args.append(a)
            return Ref(e.name, args)
        if isinstance(e, Unary):
            return Unary(e.op, self._elem(e.operand, ctx, temps))
        if isinstance(e, Binary):
            return Binary(e.op, self._elem(e.left, ctx, temps), self._elem(e.right, ctx, temps))
        if isinstance(e, Ref) and e.name.upper() not in ARRAY_RESULT and e.name.upper() in FORTRAN_INTRINSICS:
            return Ref(e.name, [self._elem(a, ctx, temps) for a in e.args])
        # a constructor or an array-valued intrinsic: compute it once into a temporary
        self.tmp_counter += 1
        tmp = f"arr{self.tmp_counter}_"
        rank = self._result_rank(e)
        temps.append(f"const auto {tmp} = {self.ex(e)[0]};")
        sym = Symbol(tmp, type=self.type_of(e), bits=self.bits(e), dims=[("1", "1")] * rank)
        self.unit.symbols[tmp] = sym
        if ctx["rank"] is None:
            ctx["rank"] = rank
            ctx["counts"] = [f"static_cast<int>({tmp}.size())"] + [
                f"static_cast<int>({tmp}[0]{'[0]' * (d - 1)}.size())" for d in range(1, rank)]
        return Ref(tmp, [Binary("+", Num("1", True), self._loop_var(ctx, d)) for d in range(rank)])

    def _result_rank(self, e) -> int:
        if isinstance(e, ArrayCtor):
            return 1
        key = e.name.upper()
        ranks = [len(self._free_dims(a) or []) or (1 if self.array_valued(a) else 0) for a in e.args]
        return ARRAY_RESULT[key](ranks)

    def _mentions(self, e, name: str) -> bool:
        from fortran_semantics import _names_in
        names: set = set()
        _names_in(e, names)
        return name in names

    def _open_loops(self, ctx: dict, outer_first: List[int]) -> None:
        for d in outer_first:
            k = f"k{ctx['id']}_{d}"
            self.line(f"for (int {k} = 0; {k} < {ctx['counts'][d]}; ++{k}) {{")
            self.level += 1

    def _close_loops(self, n: int) -> None:
        for _ in range(n):
            self.level -= 1
            self.line("}")

    def array_assign(self, lhs, rhs, raw) -> None:
        self.tmp_counter += 1
        ctx = {"id": self.tmp_counter, "rank": None, "counts": None}
        temps: List[str] = []
        lhs_elem = self._elem(lhs, ctx, temps)
        rhs_elem = self._elem(rhs, ctx, temps)
        for t in temps:
            self.line(t)
        sym = self.unit.symbols[lhs.name]
        order = list(range(ctx["rank"]))
        if self._mentions(rhs, lhs.name):          # the right side is evaluated in full before any element is stored
            elem = self.ctype_or_fail(sym)
            buf = f"buf{ctx['id']}_"
            self.line(f"std::vector<{elem}> {buf};")
            self._open_loops(ctx, order)
            self.line(f"{buf}.push_back(static_cast<{elem}>({self.convert(self.ex(rhs_elem)[0], rhs_elem, sym.type, sym.bits)}));")
            self._close_loops(len(order))
            pos = f"pos{ctx['id']}_"
            self.line(f"std::size_t {pos} = 0;")
            self._open_loops(ctx, order)
            self.line(f"{self.lvalue(lhs_elem)} = {buf}[{pos}++];")
            self._close_loops(len(order))
            return
        self._open_loops(ctx, order)
        self.do_assign(lhs_elem, rhs_elem, raw)
        self._close_loops(len(order))

    def do_assign(self, lhs, rhs, raw):
        sym = self.unit.symbols.get(getattr(lhs, "name", ""))
        if sym is not None and sym.rank and self._free_dims(lhs) is not None:
            self.array_assign(lhs, rhs, raw)             # whole array or section on the left
            return
        if isinstance(lhs, Component):
            comp = component_of(lhs, self.unit)
            if comp is not None and comp.dims and not lhs.args:        # a whole array component: fill it with a scalar
                if self.array_valued(rhs):
                    raise FortranParseError("assigning an array expression to an array component is not translated to C++")
                target, value = self.component_text(lhs), self.ex(rhs)[0]
                if len(comp.dims) == 1:
                    self.line(f"std::fill({target}.begin(), {target}.end(), {value});")
                else:
                    self.line(f"for (auto& row_ : {target}) std::fill(row_.begin(), row_.end(), {value});")
                return
        lhs_type = self.type_of(lhs if isinstance(lhs, (Name, Component)) else Name(lhs.name))
        text = self.convert(self.ex(rhs)[0], rhs, lhs_type, sym.bits if sym is not None else 32)
        if lhs_type == CHAR and sym is not None and sym.char_len and not sym.rank:
            self.helpers.add("fortran_char")
            text = f"fortran_fix(std::string({text}), {sym.char_len})"
        self.line(f"{self.lvalue(lhs)} = {text};")

    def _sequence_actual(self, callee, pos: int, a):
        """An array element given for an array dummy shares storage from that element on. std::vector storage cannot be
        shared that way, so the call gets a copy of the elements from there on, copied back afterwards -- copy-in /
        copy-out, which the standard permits for array dummies. Returns (before, after, temp name) or None."""
        if callee is None or pos >= len(callee.params) or not isinstance(a, Ref):
            return None
        formal = callee.symbols.get(callee.params[pos])
        actual = self.unit.symbols.get(a.name)
        if (formal is None or not formal.rank or actual is None or not actual.rank or len(a.args) != actual.rank
                or any(isinstance(x, Slice) for x in a.args)):
            return None
        if formal.rank != 1 or actual.rank > 2:
            raise FortranParseError("a sequence-associated actual argument is translated for rank-1 dummies and "
                                    "rank-1 or rank-2 actual arrays only")
        self.tmp_counter += 1
        n = self.tmp_counter
        tmp, elem, arr = f"seq{n}_", self.ctype_or_fail(actual), self.name_ref(a.name)
        offsets = [self.ex(Binary("-", sub, self.parse(lb)))[0] for (lb, _ub), sub in zip(actual.dims, a.args)]
        if actual.rank == 1:
            before = [f"std::vector<{elem}> {tmp}({arr}.begin() + ({offsets[0]}), {arr}.end());"]
            after = [f"std::copy({tmp}.begin(), {tmp}.end(), {arr}.begin() + ({offsets[0]}));"]
            return before, after, tmp
        i0, j0 = f"i0_{n}", f"j0_{n}"
        walk = (f"for (std::size_t jj = {j0}; jj < {arr}[0].size(); ++jj) "
                f"for (std::size_t ii = (jj == {j0} ? {i0} : 0); ii < {arr}.size(); ++ii)")
        before = [f"std::size_t {i0} = {offsets[0]}, {j0} = {offsets[1]};",
                  f"std::vector<{elem}> {tmp}; {walk} {tmp}.push_back({arr}[ii][jj]);"]
        after = [f"{{ std::size_t pos_ = 0; {walk} {arr}[ii][jj] = {tmp}[pos_++]; }}"]
        return before, after, tmp

    def do_call(self, callee, args, raw):
        cu = self.units.get(callee)
        pieces = []
        temps = []
        after_call: List[str] = []
        for pos, a in enumerate(args):
            if isinstance(a, Slice):
                raise FortranParseError("array section as CALL argument")
            seq = self._sequence_actual(cu, pos, a)
            if seq is not None:
                temps += seq[0]
                after_call += seq[1]
                pieces.append(seq[2])
                continue
            text = self.ex(a)[0]
            if cu is not None and pos < len(cu.params):
                formal = cu.params[pos]
                fsym = cu.symbols[formal]
                if formal in cu.modified_scalars and not fsym.rank:
                    tgt = self.lvalue_or_none(a)
                    if tgt is None or self.type_of(a) != fsym.type or (
                            fsym.type in (INT, REAL) and self.bits(a) != fsym.bits):
                        self.tmp_counter += 1
                        tmp = f"tmp{self.tmp_counter}_"
                        temps.append(f"{self.ctype_or_fail(fsym)} {tmp} = {text};")
                        text = tmp
            pieces.append(text)
        if cu is not None:
            pieces += [blk_var(b) for b in cu.passed_commons()]
        call = f"{cppname(callee)}({', '.join(pieces)});"
        if temps:
            self.line("{")
            self.level += 1
            for t in temps:
                self.line(t)
            self.line(call)
            for t in after_call:
                self.line(t)
            self.level -= 1
            self.line("}")
        else:
            self.line(call)

    def do_return(self):
        if self.unit.kind == "PROGRAM":
            self.line("return 0;")
        elif self.returns_value():
            self.line(f"return {self.name_ref(self.unit.name)};")
        else:
            self.line("return;")

    def do_stop(self, text, raw):
        self.headers.add("<cstdlib>")
        if not text:
            self.line("std::exit(0);")
            return
        e = self.parse(text)
        if self.type_of(e) == INT:
            self.line(f"std::exit({self.ex(e)[0]});")
        else:
            self.line(f"std::cout << {self.ex(e)[0]} << std::endl;")
            self.line("std::exit(0);")

    def do_exit(self):
        self.line("break;")

    def do_cycle(self):
        self.line("continue;")

    def do_goto(self, labels, selector, raw):
        if selector is None:
            self.line(f"goto label_{labels[0]};")
            return
        sel = self.ex(selector)[0]
        self.line(f"switch ({sel}) {{")
        for k, lbl in enumerate(labels, 1):
            self.line(f"    case {k}: goto label_{lbl};")
        self.line("    default: break;")
        self.line("}")

    def do_arith_if(self, expr, neg, zero, pos, raw):
        t = _wrap(*self.ex(expr), _P_REL + 1)
        self.line(f"if ({t} < 0) goto label_{neg};")
        self.line(f"else if ({t} == 0) goto label_{zero};")
        self.line(f"else goto label_{pos};")

    def do_print(self, items, raw):
        self.headers.add("<iostream>")
        simple: List[str] = []

        def flush():
            if simple:
                self.line("std::cout << " + " << \" \" << ".join(simple) + " << \" \";")
                simple.clear()

        for item in items:
            if self.type_of(item) == DERIVED:
                raise FortranParseError("printing a derived-type value is not translated; print its components")
            if isinstance(item, ImpliedDo):
                flush()
                var = cppname(item.var)
                lo, hi = self.ex(item.lo)[0], self.ex(item.hi)[0]
                step = "1" if item.step is None else self.ex(item.step)[0]
                cmp_op = "<=" if item.step is None or not (isinstance(item.step, Unary) and item.step.op == "-") else ">="
                self.line(f"for (int {var} = {lo}; {var} {cmp_op} {hi}; {var} += {step}) {{")
                self.level += 1
                for x in item.items:
                    self.line(f"std::cout << {_wrap(*self.ex(x), _P_ADD)} << \" \";")
                self.level -= 1
                self.line("}")
            elif self.array_valued(item):
                flush()
                self.tmp_counter += 1
                ctx = {"id": self.tmp_counter, "rank": None, "counts": None}
                temps: List[str] = []
                elem = self._elem(item, ctx, temps)
                for t in temps:
                    self.line(t)
                order = list(reversed(range(ctx["rank"])))          # Fortran output order: the first index varies fastest
                self._open_loops(ctx, order)
                self.line(f"std::cout << {_wrap(*self.ex(elem), _P_ADD)} << \" \";")
                self._close_loops(len(order))
            elif self.type_of(item) == COMPLEX:
                flush()
                z = self.ex(item)[0]
                self.line(f'{{ const auto z_ = {z}; std::cout << "(" << z_.real() << "," << z_.imag() << ") "; }}')
            else:
                simple.append(_wrap(*self.ex(item), _P_ADD))     # `<<` binds tighter than == && ||
        flush()
        self.line("std::cout << std::endl;")

    def _nested_vector(self, elem: str, counts: List[str]) -> str:
        """std::vector<...> value of the given extents, zero-initialised."""
        text = f"{elem}()"
        for k in range(len(counts) - 1, 0, -1):
            text = f"{vec_type(elem, len(counts) - k)}(static_cast<std::size_t>({counts[k]}), {text})"
        return f"static_cast<std::size_t>({counts[0]}), {text}"

    def do_allocate(self, objects, options, raw):
        for obj in objects:
            sym = self.unit.symbols.get(getattr(obj, "name", ""))
            if isinstance(obj, Name) and sym is not None and sym.pointer and not sym.rank:
                self.line(f"{cppname(obj.name)} = new {self.ctype_or_fail(sym)}();")
                continue
            if not isinstance(obj, Ref) or sym is None or not (sym.allocatable or sym.pointer):
                raise FortranParseError("ALLOCATE of something other than an allocatable or pointer array")
            counts = []
            for a in obj.args:
                if isinstance(a, Slice):
                    counts.append(self.ex(Binary("+", Binary("-", a.hi, a.lo if a.lo is not None else Num("1", True)), Num("1", True)))[0])
                else:
                    counts.append(self.ex(a)[0])
            elem = self.ctype_or_fail(sym)
            args = self._nested_vector(elem, counts)
            vt = vec_type(elem, sym.rank)
            if sym.pointer:
                self.line(f"{cppname(obj.name)} = new {vt}({args});")
            else:
                self.line(f"{self.name_ref(obj.name)} = {vt}({args});")
        for opt in options:
            m = re.match(r"^STAT\s*=\s*(.+)$", opt, re.IGNORECASE)
            if m:
                self.line(f"{self.ex(self.parse(m.group(1)))[0]} = 0;")

    def do_deallocate(self, objects, options, raw, nullify=False):
        for obj in objects:
            sym = self.unit.symbols.get(getattr(obj, "name", ""))
            if sym is not None and sym.pointer:
                self.line(f"{cppname(obj.name)} = nullptr;")
            else:
                self.line(f"{self.ex(obj)[0]}.clear();")
        for opt in options:
            m = re.match(r"^STAT\s*=\s*(.+)$", opt, re.IGNORECASE)
            if m:
                self.line(f"{self.ex(self.parse(m.group(1)))[0]} = 0;")

    def do_pointer_assign(self, lhs, rhs, raw):
        sym = self.unit.symbols.get(getattr(lhs, "name", ""))
        if not isinstance(lhs, Name) or sym is None or not sym.pointer:
            raise FortranParseError("pointer assignment to something other than a pointer")
        if isinstance(rhs, Ref) and rhs.name == "null" and not rhs.args:
            self.line(f"{cppname(lhs.name)} = nullptr;")
            return
        if not sym.rank:
            target = self.unit.symbols.get(getattr(rhs, "name", ""))
            if isinstance(rhs, Name) and target is not None and target.pointer:
                self.line(f"{cppname(lhs.name)} = {cppname(rhs.name)};")        # q => p: q points where p points
            elif isinstance(rhs, (Name, Ref, Component)):
                self.line(f"{cppname(lhs.name)} = &{self.lvalue(rhs)};")
            else:
                raise FortranParseError("a pointer can point to a variable, an array element or a component")
            return
        if not isinstance(rhs, Name) or not (self.unit.symbols.get(rhs.name) and self.unit.symbols[rhs.name].rank):
            raise FortranParseError("a pointer to an array section is not translated to C++ (std::vector has no views)")
        self.line(f"{cppname(lhs.name)} = &{self.name_ref(rhs.name)};")

    def _push_items(self, buf: str, items) -> None:
        """Statements that append every output item (arrays in array order, implied DOs expanded) to a std::vector<FItem>."""
        for item in items:
            if isinstance(item, ImpliedDo):
                var = cppname(item.var)
                lo, hi = self.ex(item.lo)[0], self.ex(item.hi)[0]
                step = "1" if item.step is None else self.ex(item.step)[0]
                cmp_op = "<=" if item.step is None or not (isinstance(item.step, Unary) and item.step.op == "-") else ">="
                self.line(f"for (int {var} = {lo}; {var} {cmp_op} {hi}; {var} += {step}) {{")
                self.level += 1
                for x in item.items:
                    self.line(f"{buf}.emplace_back({self.ex(x)[0]});")
                self.level -= 1
                self.line("}")
            elif self.array_valued(item):
                self.tmp_counter += 1
                ctx = {"id": self.tmp_counter, "rank": None, "counts": None}
                temps: List[str] = []
                elem = self._elem(item, ctx, temps)
                for t in temps:
                    self.line(t)
                order = list(reversed(range(ctx["rank"])))          # Fortran output order: the first index varies fastest
                self._open_loops(ctx, order)
                self.line(f"{buf}.emplace_back({self.ex(elem)[0]});")
                self._close_loops(len(order))
            else:
                self.line(f"{buf}.emplace_back({self.ex(item)[0]});")

    def do_formatted(self, fmt, items, raw):
        how, spec = fmt
        self.helpers.add("fortran_format")
        text = _cstr(spec) if how == "string" else self.ex(spec)[0]
        self.tmp_counter += 1
        buf = f"items{self.tmp_counter}_"
        self.line("{")
        self.level += 1
        self.line(f"std::vector<FItem> {buf};")
        self._push_items(buf, items)
        self.line(f"std::cout << fortran_write({text}, {buf});")
        self.level -= 1
        self.line("}")

    # ------------------------------------------------------------------ READ, WRITE to files, OPEN, CLOSE, ...
    def _unit_text(self, io, standard: int) -> str:
        return str(standard) if io["unit"] in (None, "*") else self.ex(self.parse(io["unit"]))[0]

    def _string_arg(self, text) -> str:
        return "std::string()" if text is None else f"std::string({self.ex(self.parse(text))[0]})"

    def _iostat_assign(self, io, value: str, label_jumps: bool = False) -> None:
        c = io["controls"]
        if c.get("iostat"):
            self.line(f"{self.lvalue(self.parse(c['iostat']))} = {value};")
        elif label_jumps and (c.get("end", "").isdigit() or c.get("err", "").isdigit()):
            self.line(f"int ios_ = {value};")
            if c.get("end", "").isdigit():
                self.line(f"if (ios_ < 0) goto label_{c['end']};")
            if c.get("err", "").isdigit():
                self.line(f"if (ios_ > 0) goto label_{c['err']};")
        else:
            self.line(f"{value};")

    def do_io(self, io, fmt, raw):
        self.helpers.add("fortran_io")
        kind, c = io["kind"], io["controls"]
        if kind == "READ":
            self._do_read(io, fmt, raw)
        elif kind in ("WRITE", "PRINT"):
            items = [self.parse(i) for i in io["items"]]
            self.tmp_counter += 1
            buf = f"items{self.tmp_counter}_"
            self.line("{")
            self.level += 1
            self.line(f"std::vector<FItem> {buf};")
            self._push_items(buf, items)
            text = f"fortran_list({buf})" if fmt is None else \
                f"fortran_write({_cstr(fmt[1]) if fmt[0] == 'string' else self.ex(fmt[1])[0]}, {buf})"
            self._iostat_assign(io, f"fortran_write_unit({self._unit_text(io, 6)}, {text})")
            self.level -= 1
            self.line("}")
        elif kind == "OPEN":
            for unsupported in ("newunit", "recl"):
                if unsupported in c:
                    raise FortranParseError(f"OPEN with {unsupported}= is not translated")
            if c.get("access", "'sequential'").strip("'\" ").lower() != "sequential" or \
                    c.get("form", "'formatted'").strip("'\" ").lower() != "formatted":
                raise FortranParseError("only sequential formatted files are translated")
            args = [self._unit_text(io, 6), self._string_arg(c.get("file"))]
            for key, default in (("status", "unknown"), ("action", "readwrite"), ("position", "asis")):
                args.append(self._string_arg(c[key]) if key in c else f'std::string("{default}")')
            self._iostat_assign(io, f"fortran_open({', '.join(args)})")
        elif kind == "CLOSE":
            status = f", {self._string_arg(c['status'])}" if "status" in c else ""
            self._iostat_assign(io, f"fortran_close({self._unit_text(io, 6)}{status})")
        elif kind == "REWIND":
            self._iostat_assign(io, f"fortran_rewind({self._unit_text(io, 6)})")
        elif kind == "INQUIRE":
            if set(c) - {"file", "exist", "iostat"} or "exist" not in c or "file" not in c:
                raise FortranParseError("only INQUIRE(FILE=..., EXIST=...) is translated")
            self.line(f"{self.lvalue(self.parse(c['exist']))} = fortran_exists({self._string_arg(c['file'])});")
        else:
            raise FortranParseError(f"{kind} is not translated")

    def _read_item(self, rd: str, item) -> None:
        if isinstance(item, ImpliedDo):
            var = cppname(item.var)
            lo, hi = self.ex(item.lo)[0], self.ex(item.hi)[0]
            step = "1" if item.step is None else self.ex(item.step)[0]
            cmp_op = "<=" if item.step is None or not (isinstance(item.step, Unary) and item.step.op == "-") else ">="
            self.line(f"for (int {var} = {lo}; {var} {cmp_op} {hi}; {var} += {step}) {{")
            self.level += 1
            for sub in item.items:
                self._read_item(rd, sub)
            self.level -= 1
            self.line("}")
            return
        if self.array_valued(item):
            self.tmp_counter += 1
            ctx = {"id": self.tmp_counter, "rank": None, "counts": None}
            temps: List[str] = []
            elem = self._elem(item, ctx, temps)
            order = list(reversed(range(ctx["rank"])))              # Fortran array order: the first index varies fastest
            self._open_loops(ctx, order)
            self._read_scalar(rd, elem)
            self._close_loops(len(order))
            return
        self._read_scalar(rd, item)

    def _read_scalar(self, rd: str, target) -> None:
        t = self.type_of(target)
        if t == COMPLEX or t == DERIVED:
            raise FortranParseError("READ of this type is not translated")
        place = self.lvalue(target)
        sym = self.unit.symbols.get(getattr(target, "name", ""))
        if t == CHAR and sym is not None and sym.char_len and not sym.rank:
            self.helpers.add("fortran_char")
            self.line(f"{rd}.get({place}, {sym.char_len}); {place} = fortran_fix({place}, {sym.char_len});")
        else:
            self.line(f"{rd}.get({place});")

    def _do_read(self, io, fmt, raw):
        c = io["controls"]
        self.tmp_counter += 1
        rd = f"rd{self.tmp_counter}_"
        die = 0 if any(k in c for k in ("iostat", "end", "err")) else 1
        self.line("{")
        self.level += 1
        if fmt is None:
            ctor = f"FReader {rd}({self._unit_text(io, 5)}, nullptr, {'true' if die else 'false'});"
        else:
            self.line(f"std::string fmt_ = {_cstr(fmt[1]) if fmt[0] == 'string' else self.ex(fmt[1])[0]};")
            ctor = f"FReader {rd}({self._unit_text(io, 5)}, &fmt_, {'true' if die else 'false'});"
        self.line(ctor)
        for item in io["items"]:
            self._read_item(rd, self.parse(item))
        self._iostat_assign(io, f"{rd}.finish({die})", label_jumps=True)
        self.level -= 1
        self.line("}")

    def do_comment(self, text):
        self.line(f"// {text}")

    def do_label(self, label):
        self.line(f"label_{label}: ;")

    def do_unsupported(self, why, raw):
        self.headers.add("<stdexcept>")
        self.line(f"throw std::logic_error({_cstr('fortranBridge: ' + why + ': ' + raw.strip())});")


def _cpp_type_definition(tdef, units, by_name, headers_ref) -> List[str]:
    """A struct for a derived type: value-initialised members, copy by assignment."""
    from fortran_to_python import _type_extent
    em = CppUnitEmitter(units[0], by_name, {}, {}, set())
    lines = [f"struct {tdef.name}_t {{"]
    for comp in tdef.components:
        elem = f"{comp.derived}_t" if comp.type == DERIVED else em._elem_type(comp.type, comp.bits)
        shape = [max(_type_extent(ub, units) - _type_extent(lb, units) + 1, 0) for lb, ub in comp.dims]
        decl = elem
        for extent in reversed(shape):
            decl = f"std::array<{decl}, {extent}>"
        if comp.init is not None and not shape:
            e = parse_expression(comp.init)
            lines.append(f"    {decl} {comp.name} = static_cast<{elem}>({em.ex(e)[0]});")
        else:
            lines.append(f"    {decl} {comp.name}{{}};")
    return lines + ["};"]


def translate_fortran_to_cpp(source_text: str) -> str:
    r"""Translate entire Fortran source into modern C++17/20."""
    stmts, units = lex_and_split(source_text)
    blocks = analyze_common_blocks(stmts, units)
    by_name = {u.name: u for u in units}
    func_types = function_result_types(units)

    headers: Set[str] = {"<algorithm>", "<array>", "<cctype>", "<cmath>", "<complex>", "<cstdio>", "<cstdlib>", "<functional>",
                         "<fstream>", "<iomanip>", "<iostream>", "<map>", "<memory>", "<sstream>", "<stdexcept>", "<string>", "<type_traits>", "<vector>"}
    body: List[str] = []
    helper_needed = False
    helpers: Set[str] = set()
    prototypes: List[str] = []
    module_lines: List[str] = []
    for u in units:
        em = CppUnitEmitter(u, by_name, func_types, blocks, headers)
        if u.kind == "MODULE":
            module_lines.extend(em.module_declarations())
        else:
            body.extend(em.run())
        helper_needed = helper_needed or em.uses_untranslated_helper
        helpers |= em.helpers
        if em.prototype:
            prototypes.append(em.prototype)

    out: List[str] = [
        "// ============================================================================",
        "// Translated from FORTRAN to Modern C++17/20 by fortranBridge",
        "// Scaffold: review every `throw std::logic_error` (constructs that were not",
        "// translated) and verify numerically against the Fortran reference before use.",
        "// ============================================================================",
        "",
    ]
    if "fortran_matmul" in helpers:
        headers |= {"<dlfcn.h>", "<cstdlib>", "<cstddef>", "<vector>"}
    for h in sorted(headers):
        out.append(f"#include {h}")
    out.append("")
    out += ["// Integer overflow wraps modulo 2**32 / 2**64 as in Fortran: compile with -fwrapv.", ""]
    if "fortran_ipow" in helpers:
        out += _IPOW_HELPER + [""]
    if "fortran_powi" in helpers:
        out += _POWI_HELPER + [""]
    if "fortran_io" in helpers:
        helpers.add("fortran_format")
    if "fortran_format" in helpers:
        out += _FORMAT_RUNTIME + [""]
    if "fortran_io" in helpers:
        out += _IO_RUNTIME + [""]
    if "fortran_char" in helpers:
        out += _CHAR_HELPER + [""]
    if "fortran_cdiv" in helpers:
        out += _CDIV_HELPER + [""]
    if "fortran_matmul" in helpers:
        out += _MATMUL_RUNTIME + [""]
    if "fortran_transpose" in helpers:
        out += _TRANSPOSE_HELPER + [""]
    if "fortran_modulo" in helpers:
        out += _MODULO_HELPER + [""]
    if helper_needed:
        out += ['inline bool fortran_untranslated(const char* text) { throw std::logic_error(text); }', ""]
    for blk in blocks.values():
        out.append(blk.emit_cpp_struct())
        out.append("")
    for tdef in (units[0].types.values() if units else ()):
        out += _cpp_type_definition(tdef, units, by_name, headers_ref=headers) + [""]
    if module_lines:
        out += ["// MODULE entities (file scope)"] + module_lines + [""]
    if prototypes:
        out += ["// Forward declarations: a unit may be called before it is defined."] + prototypes + [""]
    out.extend(body)
    return "\n".join(out).rstrip() + "\n"
