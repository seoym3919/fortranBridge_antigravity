#!/usr/bin/env python3
r"""
fortran_to_python.py - Automated Fortran-to-Python / NumPy Code Emitter.

Converts (structured Fortran 66/77/90/95/2003 subset, see fortran_semantics.py):
- SUBROUTINE / FUNCTION / PROGRAM units -> typed Python functions (several units per file).
- 1-based array subscripts -> 0-based NumPy indexing with constant folding (A(I-1) -> a[i - 2]);
  non-unit lower bounds (A(0:N)) are honoured; local arrays keep Fortran (column-major) layout.
- Fortran DO loops keep the Fortran loop variable value; the subscript carries the -1 shift, so
  scalar uses of the loop variable stay correct.
- Integer division follows Fortran (truncation) using static type inference.
- Scalar dummy arguments that the routine modifies are returned (tuple, in argument order) and
  assigned back at CALL sites; arrays are modified in place.
- COMMON blocks -> @dataclass state objects passed as explicit arguments.
- Intrinsics (DSQRT, MAX, MOD, NINT, ...) -> math / numpy equivalents.

Anything outside the supported subset (GO TO, DATA, EQUIVALENCE, formatted I/O, ...) is emitted as
`raise NotImplementedError(...)` at that point -- never dropped silently.
"""

from __future__ import annotations

import keyword
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from common_block_analyzer import analyze_common_blocks
from fortran_emit_base import UnitEmitter
from goto_machine import GotoMachineMixin
from fortran_intrinsics import FORTRAN_INTRINSICS, STANDARD_INTRINSIC_NAMES, translate_intrinsic
from fortran_semantics import (
    CHAR, COMPLEX, INT, LOGICAL, REAL,
    ArrayCtor, Binary, Component, ComplexLit, DERIVED, FortranParseError, ImpliedDo, Logical, Name, Num, Ref, Slice, Str, Symbol,
    Unary, Unit, component_of, const_int, parse_expression,
    KIND_INQUIRY, add_const, base_type, common_extents, function_result_types, infer_type, kind_call_value, lex_and_split, num_bits,
    split_top_level,
    _split_decl_item,
)

_CLASH = {"np", "math", "cmath", "max", "min", "sum", "abs", "int", "float", "round", "range", "len",
          "print", "bool", "str", "complex", "list", "dict", "set", "tuple", "type", "id", "all", "any"}
_PYTYPE = {INT: "int", REAL: "float", LOGICAL: "bool", CHAR: "str", COMPLEX: "complex"}
_NPTYPE = {INT: "np.int64", REAL: "np.float64", LOGICAL: "np.bool_", COMPLEX: "np.complex128", CHAR: "object"}
_NP_REAL32 = "np.float32"
_NP_CPLX64 = "np.complex64"

# Helpers the generated module defines when it uses them (names are the keys of PyUnitEmitter.helpers).
_HELPERS = {
    "_i32": ["def _i32(x):", "    \"\"\"Wrap to a signed 32-bit INTEGER (two's complement), as Fortran integers do.\"\"\"",
             "    return ((x + 2147483648) & 4294967295) - 2147483648"],
    "_i64": ["def _i64(x):", "    \"\"\"Wrap to a signed 64-bit INTEGER (two's complement).\"\"\"",
             "    return ((x + 9223372036854775808) & 18446744073709551615) - 9223372036854775808"],
    "_idiv": ["def _idiv(a, b):", "    \"\"\"INTEGER division truncates toward zero (Python's // floors).\"\"\"",
              "    q = abs(a) // abs(b)", "    return q if (a < 0) == (b < 0) else -q"],
    "_imod": ["def _imod(a, b):", "    \"\"\"MOD(a, b) for INTEGER: the sign follows a (Fortran), unlike Python's %.\"\"\"",
              "    return a - b * _idiv(a, b)"],
    "_ipow": ["def _ipow(a, b):", "    \"\"\"INTEGER ** INTEGER: a negative exponent gives 0 unless the base is 1 or -1 (1 / a**-b truncates).\"\"\"",
              "    if b >= 0:", "        return a ** b", "    if a == 1:", "        return 1", "    if a == -1:",
              "        return 1 if b % 2 == 0 else -1", "    if a == 0:", "        raise ZeroDivisionError('0 ** negative integer')",
              "    return 0"],
}
_HELPERS.update({
    "_fpowi": ["def _fpowi(x, n):",
               "    \"\"\"REAL ** INTEGER by repeated squaring, as libgfortran does (pow() rounds differently in the last bit).\"\"\"",
               "    u = abs(n)", "    p = type(x)(1)", "    while True:", "        if u & 1:", "            p = p * x",
               "        u >>= 1", "        if not u:", "            break", "        x = x * x",
               "    if n < 0:", "        try:", "            return type(p)(1) / p", "        except ZeroDivisionError:",
               "            return math.copysign(math.inf, p)", "    return p"],
    "_fpow": ["def _fpow(a, b):", "    \"\"\"REAL ** REAL with IEEE results: Python raises, or returns a complex number for a negative base.\"\"\"",
              "    try:", "        r = a ** b", "    except ZeroDivisionError:",
              "        return math.copysign(math.inf, a) if float(b).is_integer() and int(b) % 2 else math.inf",
              "    except OverflowError:",
              "        return math.copysign(math.inf, a) if a < 0 and float(b).is_integer() and int(b) % 2 else math.inf",
              "    return math.nan if isinstance(r, complex) else r"],
    "_fm": ["def _ieee(fn):", "    \"\"\"math.* raises on a domain or range error; Fortran returns -inf, +inf or nan.\"\"\"",
            "    def wrapped(*args):", "        try:", "            return fn(*args)", "        except ValueError:",
            "            if fn in (math.log, math.log10) and args[0] == 0:", "                return -math.inf",
            "            return math.nan", "        except OverflowError:",
            "            return math.copysign(math.inf, args[0]) if fn is math.sinh else math.inf", "    return wrapped", "", "",
            "class _fm:", "    sqrt, log, log10, exp, sin, cos, tan, asin, acos, sinh, cosh, fmod = (",
            "        _ieee(f) for f in (math.sqrt, math.log, math.log10, math.exp, math.sin, math.cos, math.tan, math.asin,",
            "                           math.acos, math.sinh, math.cosh, math.fmod))"],
    "_dassign": ["def _dassign(dst, src):", "    \"\"\"Assignment of a derived-type value: copy the components into the target (value semantics).\"\"\"",
                 "    dst.__dict__.update(copy.deepcopy(src).__dict__)"],
    "_cfix": ["def _cfix(s, n):", "    \"\"\"Assign to CHARACTER(n): truncate or pad with blanks.\"\"\"", "    return str(s)[:n].ljust(n)"],
    "_ccmp": ["def _ccmp(a, b):", "    \"\"\"Compare CHARACTER values: the shorter is padded with blanks.\"\"\"",
              "    n = max(len(a), len(b))", "    a, b = a.ljust(n), b.ljust(n)", "    return (a > b) - (a < b)"],
    "_cdiv32": ["def _cdiv32(a, b):",
                "    \"\"\"COMPLEX division in single precision: Smith's algorithm in float32 (libgcc's __divsc3);",
                "    numpy's complex64 division rounds differently.\"\"\"",
                "    ar, ai, br, bi = np.float32(a.real), np.float32(a.imag), np.float32(b.real), np.float32(b.imag)",
                "    if abs(br) >= abs(bi):", "        r = bi / br", "        d = br + bi * r",
                "        return np.complex64(complex((ar + ai * r) / d, (ai - ar * r) / d))",
                "    r = br / bi", "    d = br * r + bi",
                "    return np.complex64(complex((ar * r + ai) / d, (ai * r - ar) / d))"],
    "_fdiv": ["def _fdiv(a, b):", "    \"\"\"REAL division with IEEE results: x/0 is +-inf, 0/0 is nan (Python raises ZeroDivisionError).\"\"\"",
              "    try:", "        return a / b", "    except ZeroDivisionError:",
              "        if a != a or a == 0:", "            return math.nan",
              "        return math.copysign(math.inf, a) * math.copysign(1.0, b)"],
    "_fsum": ["def _fsum(x):", "    \"\"\"SUM: accumulate in array (column-major) order in the array's precision, as Fortran does;",
              "    np.sum would add pairwise and round differently.\"\"\"",
              "    x = np.asarray(x)", "    return x.dtype.type(0) if x.size == 0 else np.cumsum(x.ravel(order=\"F\"))[-1]"],
    "_fprod": ["def _fprod(x):", "    \"\"\"PRODUCT: accumulate in array (column-major) order in the array's precision.\"\"\"",
               "    x = np.asarray(x)", "    return x.dtype.type(1) if x.size == 0 else np.cumprod(x.ravel(order=\"F\"))[-1]"],
    "_fmatmul": ["def _fmatmul(a, b):",
                 "    \"\"\"MATMUL: every element accumulates over the shared index in order, in the arrays' precision (no BLAS).",
                 "    libgfortran's MATMUL is blocked, unrolled and may use FMA (it depends on the CPU), so its last bits cannot be",
                 "    reproduced by any fixed order: results agree to a few ULP, not bitwise.\"\"\"",
                 "    a, b = np.asarray(a), np.asarray(b)", "    dtype = np.result_type(a, b)",
                 "    if a.ndim == 2 and b.ndim == 2:", "        c = np.zeros((a.shape[0], b.shape[1]), dtype=dtype, order=\"F\")",
                 "        for k in range(a.shape[1]):", "            c = c + np.outer(a[:, k], b[k, :])", "        return c",
                 "    if a.ndim == 2:", "        c = np.zeros(a.shape[0], dtype=dtype)", "        for k in range(a.shape[1]):",
                 "            c = c + a[:, k] * b[k]", "        return c",
                 "    c = np.zeros(b.shape[1], dtype=dtype)", "    for k in range(a.shape[0]):",
                 "        c = c + a[k] * b[k, :]", "    return c"],
    "_fdot": ["def _fdot(a, b):", "    \"\"\"DOT_PRODUCT: products rounded in the array's precision, summed in array order.\"\"\"",
              "    return _fsum(np.asarray(a) * np.asarray(b))"],
    "_fdo": ["def _fdo(start, end, step):",
             "    \"\"\"REAL DO variable: trip count max(int((end - start + step) / step), 0) in the variable's precision;",
             "    the variable accumulates x = x + step. Returns (values taken in the loop, value after the loop).\"\"\"",
             "    values, x = [], start", "    for _ in range(max(int((end - start + step) / step), 0)):",
             "        values.append(x)", "        x = x + step", "    return values, x"],
})
for _name in ("_idiv", "_imod", "_ipow"):
    _HELPERS[_name] = list(_HELPERS[_name])
# MATMUL: libgfortran's own routine through ctypes when it is installed (bit for bit gfortran's result), else a fixed-order sum
_MATMUL_RUNTIME = Path(__file__).with_name("fortran_matmul_runtime.py").read_text(encoding="utf-8")
_HELPERS["_fmatmul"] = _MATMUL_RUNTIME[_MATMUL_RUNTIME.index("class _FDim"):].rstrip("\n").split("\n")
# READ and file I/O: also a real module, tested against gfortran
_IO_RUNTIME = Path(__file__).with_name("fortran_io_runtime.py").read_text(encoding="utf-8")
_HELPERS["_fio"] = _IO_RUNTIME[_IO_RUNTIME.index("_funits = {}"):].rstrip("\n").split("\n")
# formatted output: the runtime is a real module (fortran_format_runtime.py, tested against gfortran) embedded verbatim
_FORMAT_RUNTIME = Path(__file__).with_name("fortran_format_runtime.py").read_text(encoding="utf-8")
_HELPERS["_fwrite"] = _FORMAT_RUNTIME[_FORMAT_RUNTIME.index("def _fparse"):].rstrip("\n").split("\n")
_HELPER_DEPENDS = {"_fio": ("_fwrite",), "_imod": ("_idiv",), "_fdot": ("_fsum",)}

_P_OR, _P_AND, _P_NOT, _P_CMP, _P_ADD, _P_MUL, _P_UNARY, _P_POW, _P_ATOM = 1, 2, 3, 4, 5, 6, 7, 8, 10
_BIN_PREC = {"or": _P_OR, "eqv": _P_CMP, "neqv": _P_CMP, "and": _P_AND, "==": _P_CMP, "!=": _P_CMP, "<": _P_CMP,
             "<=": _P_CMP, ">": _P_CMP, ">=": _P_CMP, "+": _P_ADD, "-": _P_ADD, "//": _P_ADD, "*": _P_MUL, "/": _P_MUL, "**": _P_POW}
_PY_OP = {"eqv": "==", "neqv": "!="}
_CMP_OPS = {"==", "!=", "<", "<=", ">", ">="}


def _constant_int(e) -> bool:
    if isinstance(e, Num):
        return True
    if isinstance(e, Unary):
        return _constant_int(e.operand)
    if isinstance(e, Binary):
        return _constant_int(e.left) and _constant_int(e.right)
    return False


def pyname(name: str) -> str:
    name = name.lower()
    return name + "_" if (keyword.iskeyword(name) or name in _CLASH) else name


def _wrap(text: str, prec: int, minimum: int) -> str:
    return f"({text})" if prec < minimum else text


def blk_var(block: str) -> str:
    return f"{block.lower()}_common"


class PyUnitEmitter(GotoMachineMixin, UnitEmitter):
    def __init__(self, unit, units, func_types, blocks, imports: Set[str]):
        super().__init__(unit, units, func_types)
        self.blocks_info = blocks
        self.imports = imports
        self.allocated: Set[str] = set()
        self.uses_untranslated_helper = False
        self.helpers: Set[str] = set()
        self._ring = 0              # >0 while emitting the operands of an INTEGER + - * (wrap only the outermost one)
        self._do_counter = 0
        self._do_stack: List[Optional[Tuple[str, str]]] = []      # (variable, value after normal completion) or None

    def run(self) -> List[str]:
        # GO TO has no Python equivalent: a unit that jumps is compiled to a state machine (see goto_machine.py)
        return self._run_machine() if self.goto_targets else super().run()

    # ------------------------------------------------------------------ expressions
    def ex(self, e) -> Tuple[str, int]:
        if isinstance(e, Num):
            if not e.is_int and self.bits(e) == 32:
                return f"{_NP_REAL32}({e.text})", _P_ATOM       # a REAL literal is single precision
            return e.text, _P_ATOM
        if isinstance(e, Logical):
            return ("True" if e.value else "False"), _P_ATOM
        if isinstance(e, Str):
            return repr(e.value), _P_ATOM
        if isinstance(e, Name):
            return self.name_ref(e.name), _P_ATOM
        if isinstance(e, Ref):
            return self.ref(e)
        if isinstance(e, Slice):
            raise FortranParseError("array section outside a subscript")
        if isinstance(e, Unary):
            ring_op = e.op == "-" and self.type_of(e) == INT
            saved = self._ring
            self._ring = saved + 1 if ring_op else 0
            try:
                text, prec = self.ex(e.operand)
            finally:
                self._ring = saved
            if e.op == "not":
                return "not " + _wrap(text, prec, _P_NOT), _P_NOT
            text = _wrap(text, prec, _P_MUL)
            out, out_prec = e.op + text, (_P_UNARY if prec >= _P_UNARY else _P_MUL)
            if ring_op and saved == 0:
                return self.wrap_int(out, e), _P_ATOM
            return out, out_prec
        if isinstance(e, Binary):
            return self.binary(e)
        if isinstance(e, ArrayCtor):
            return self.array_ctor(e), _P_ATOM
        if isinstance(e, Component):
            return self.component_text(e), _P_ATOM
        if isinstance(e, ComplexLit):
            body = f"complex({self.ex(e.re)[0]}, {self.ex(e.im)[0]})"
            return (f"{_NP_CPLX64}({body})" if self.bits(e) == 32 else body), _P_ATOM
        if isinstance(e, ImpliedDo):
            raise FortranParseError("an implied DO is only allowed in an array constructor or an output list")
        raise FortranParseError(f"cannot translate {e!r}")

    def component_text(self, e: Component) -> str:
        comp = component_of(e, self.unit)
        if comp is None:
            raise FortranParseError(f"unknown component {e.comp}")
        base = self.ex(e.base)[0]
        if not e.args:
            return f"{base}.{e.comp}" + ("[...]" if comp.dims else "")
        view = Symbol(e.comp, type=comp.type, bits=comp.bits, dims=comp.dims)
        return f"{base}.{e.comp}[{self.subscripts(view, e.args)}]"

    def structure_constructor(self, e: Ref) -> str:
        typedef = self.unit.types[e.name]
        if len(e.args) > len(typedef.components):
            raise FortranParseError(f"too many components for {e.name}")
        parts = []
        for comp, arg in zip(typedef.components, e.args):
            text = self.ex(arg)[0]
            parts.append(text if comp.dims or comp.type == DERIVED else self.convert(text, arg, comp.type, comp.bits))
        return f"{e.name}_t({', '.join(parts)})"

    def range_text(self, start, end, step) -> str:
        """range(...) for an integer loop start, end, step (step None means 1); end is inclusive as in Fortran."""
        s_txt = self._as_int(start)
        step_lit = "1" if step is None else None
        if step is not None and isinstance(step, Num) and step.is_int:
            step_lit = step.text
        elif step is not None and isinstance(step, Unary) and step.op == "-" and isinstance(step.operand, Num):
            step_lit = "-" + step.operand.text
        if step_lit is not None and int(step_lit) != 0:
            inc = 1 if int(step_lit) > 0 else -1
            tail = "" if step_lit == "1" else f", {step_lit}"
            return f"range({s_txt}, {self._range_end(end, inc)}{tail})"
        st = self._as_int(step)
        return f"range({s_txt}, {self._as_int(end)} + (1 if {st} > 0 else -1), {st})"

    def actual_argument(self, a, callee, pos: int) -> str:
        """Text of an actual argument. An array element given for an array dummy (sequence association) passes the
        storage from that element onwards, column-major: a view of the flattened array."""
        if callee is not None and pos < len(callee.params):
            formal = callee.symbols.get(callee.params[pos])
            actual = self.unit.symbols.get(getattr(a, "name", ""))
            if (formal is not None and formal.rank and isinstance(a, Ref) and actual is not None and actual.rank
                    and len(a.args) == actual.rank and not any(isinstance(x, Slice) for x in a.args)):
                offset: Expr = Num("0", True)
                stride: Expr = Num("1", True)
                for (lb, ub), sub in zip(actual.dims, a.args):
                    lo, hi = self.parse(lb), self.parse(ub)
                    offset = Binary("+", offset, Binary("*", Binary("-", sub, lo), stride))
                    stride = Binary("*", stride, Binary("+", Binary("-", hi, lo), Num("1", True)))
                saved, self._ring = self._ring, 1
                try:
                    start = self.ex(offset)[0]
                finally:
                    self._ring = saved
                return f'np.reshape({self.name_ref(a.name)}, -1, order="F")[{start}:]'
        return self.ex(a)[0]

    def array_ctor(self, e: ArrayCtor) -> str:
        t, width = self.type_of(e), self.bits(e)
        dtype = _NP_REAL32 if t == REAL and width == 32 else _NP_CPLX64 if t == COMPLEX and width == 32 else _NPTYPE[t]
        return f"np.array([{', '.join(self._ctor_part(item, t, width) for item in e.items)}], dtype={dtype})"

    def _ctor_part(self, item, t: str, width: int) -> str:
        """One element of a constructor's list; `*` splices implied DOs and nested arrays in."""
        if isinstance(item, ImpliedDo):
            saved, self._ring = self._ring, 1
            try:
                elems = [self._ctor_part(x, t, width) for x in item.items]
                rng = self.range_text(item.lo, item.hi, item.step)
            finally:
                self._ring = saved
            var = self.name_ref(item.var)
            if len(elems) == 1 and not elems[0].startswith("*"):
                return f"*[{elems[0]} for {var} in {rng}]"
            return f"*[_v for {var} in {rng} for _v in ({', '.join(elems)},)]"
        if isinstance(item, ArrayCtor):
            return "*" + self.array_ctor(item)
        if self.is_arrayish(item):
            return f'*np.ravel({self.ex(item)[0]}, order="F")'
        return self.convert(self.ex(item)[0], item, t, width)

    # ------------------------------------------------------------------ kinds and integer semantics
    def bits(self, e) -> int:
        return num_bits(e, self.unit, self.func_types, self.units)

    def wrap_int(self, text: str, e) -> str:
        """Wrap an INTEGER result to its width. Constant expressions are left alone."""
        if _constant_int(e):
            return text
        name = "_i64" if self.bits(e) > 32 else "_i32"
        self.helpers.add(name)
        return f"{name}({text})"

    def coerce(self, text: str, e, to_type: str, to_bits: int) -> str:
        """Convert an operand to the numeric type/width Fortran converts it to before the operation."""
        have, hb = self.type_of(e), self.bits(e)
        if to_type == COMPLEX:
            if have == COMPLEX:
                return f"complex({text})" if hb < to_bits else text
            return f"{_NP_CPLX64}({text})" if to_bits == 32 else f"complex({text})"
        if to_type == REAL and have == INT:
            return f"{_NP_REAL32}({text})" if to_bits == 32 else text
        if to_type == REAL and have == REAL and hb < to_bits:
            return f"float({text})"
        return text

    def _operands(self, e: Binary):
        """Emit both operands of a non-INTEGER-ring operator; each starts a new wrap context."""
        saved, self._ring = self._ring, 0
        try:
            return self.ex(e.left), self.ex(e.right)
        finally:
            self._ring = saved

    def binary(self, e: Binary) -> Tuple[str, int]:
        op = e.op
        prec = _BIN_PREC[op]
        result = self.type_of(e)
        both_int = self.type_of(e.left) == INT and self.type_of(e.right) == INT
        numeric = [t for t in (self.type_of(e.left), self.type_of(e.right)) if t in (INT, REAL)]
        if op in ("+", "-", "*") and result == INT:
            saved = self._ring
            self._ring = saved + 1
            try:
                (lt, lp), (rt, rp) = self.ex(e.left), self.ex(e.right)
            finally:
                self._ring = saved
            text = f"{_wrap(lt, lp, prec)} {op} {_wrap(rt, rp, prec + 1)}"
            return (self.wrap_int(text, e), _P_ATOM) if saved == 0 else (text, prec)
        if op == "**" and both_int:
            exponent = e.right
            if isinstance(exponent, Num) and exponent.is_int:          # b ** 2, b ** 0: exact, an integer product
                saved = self._ring
                self._ring = saved + 1
                try:
                    lt, lp = self.ex(e.left)
                finally:
                    self._ring = saved
                text = f"{_wrap(lt, lp, _P_POW + 1)} ** {exponent.text}"
                return (self.wrap_int(text, e), _P_ATOM) if saved == 0 else (text, _P_POW)
            (lt, _lp), (rt, _rp) = self._operands(e)
            self.helpers.add("_ipow")
            call = f"_ipow({lt}, {rt})"
            return (self.wrap_int(call, e), _P_ATOM) if self._ring == 0 else (call, _P_ATOM)
        (lt, lp), (rt, rp) = self._operands(e)
        width = 32
        if result == COMPLEX or (op in _CMP_OPS and COMPLEX in numeric):
            width = max([self.bits(x) for x in (e.left, e.right) if self.type_of(x) in (REAL, COMPLEX)] or [32])
            if op == "**" and self.type_of(e.right) == INT:
                lt = self.coerce(lt, e.left, COMPLEX, width)
                if isinstance(e.right, Num) and e.right.text == "2" and isinstance(e.left, (Name, Num)):
                    return f"{_wrap(lt, lp, _P_MUL)} * {_wrap(lt, lp, _P_MUL + 1)}", _P_MUL
                self.helpers.add("_fpowi")
                return f"_fpowi({lt}, {rt})", _P_ATOM
            lt, rt = self.coerce(lt, e.left, COMPLEX, width), self.coerce(rt, e.right, COMPLEX, width)
        elif result == REAL or (op in _CMP_OPS and REAL in numeric):
            kind = REAL
            width = max([self.bits(x) for x in (e.left, e.right) if self.type_of(x) == REAL] or [32])
            if op == "**" and self.type_of(e.right) == INT:
                rt_ = rt                                    # real ** integer keeps the integer exponent
                lt = self.coerce(lt, e.left, kind, width)
            else:
                lt, rt_ = self.coerce(lt, e.left, kind, width), self.coerce(rt, e.right, kind, width)
            rt = rt_
        if op == "//":
            return f"{_wrap(lt, lp, _P_ADD)} + {_wrap(rt, rp, _P_ADD + 1)}", _P_ADD
        if op in _CMP_OPS and CHAR in (self.type_of(e.left), self.type_of(e.right)):
            self.helpers.add("_ccmp")
            return f"(_ccmp({lt}, {rt}) {_PY_OP.get(op, op)} 0)", _P_ATOM
        if op == "/" and result == COMPLEX and width == 32:
            self.helpers.add("_cdiv32")
            return f"_cdiv32({lt}, {rt})", _P_ATOM
        if op == "/":
            if both_int:
                self.helpers.add("_idiv")
                return f"_idiv({lt}, {rt})", _P_ATOM
            nonzero_literal = isinstance(e.right, Num) and float(e.right.text) != 0.0
            if result == REAL and width == 64 and not nonzero_literal:
                self.helpers.add("_fdiv")                    # Python float division by zero raises; Fortran gives inf/nan
                return f"_fdiv({lt}, {rt})", _P_ATOM
            return f"{_wrap(lt, lp, _P_MUL)} / {_wrap(rt, rp, _P_MUL + 1)}", _P_MUL
        if op == "**" and result == REAL:
            if self.type_of(e.right) == INT:
                if isinstance(e.right, Num) and e.right.text == "2" and isinstance(e.left, (Name, Num)):
                    return f"{_wrap(lt, lp, _P_MUL)} * {_wrap(lt, lp, _P_MUL + 1)}", _P_MUL        # x**2 is x*x exactly
                self.helpers.add("_fpowi")
                return f"_fpowi({lt}, {rt})", _P_ATOM
            if width == 64:
                self.helpers.add("_fpow")
                return f"_fpow({lt}, {rt})", _P_ATOM
        if op == "**":
            return f"{_wrap(lt, lp, _P_POW + 1)} ** {_wrap(rt, rp, _P_UNARY)}", _P_POW
        if prec == _P_CMP:
            return f"{_wrap(lt, lp, _P_CMP + 1)} {_PY_OP.get(op, op)} {_wrap(rt, rp, _P_CMP + 1)}", _P_CMP
        if op in ("and", "or"):
            return f"{_wrap(lt, lp, prec)} {op} {_wrap(rt, rp, prec + 1)}", prec
        # + - * on REAL: left associative, keep the Fortran evaluation order exactly
        return f"{_wrap(lt, lp, prec)} {op} {_wrap(rt, rp, prec + 1)}", prec

    def name_ref(self, name: str) -> str:
        sym = self.unit.symbols.get(name)
        if sym is not None and sym.common_block:
            return f"{blk_var(sym.common_block)}.{sym.common_field or name}"
        if self.unit.kind == "FUNCTION" and name == self.unit.name:
            return pyname(name) + "_result"
        if sym is not None and not sym.rank and sym.type != DERIVED and (
                sym.pointer or (sym.target and not sym.is_dummy and not (sym.saved or sym.module or sym.common_block))):
            return f"{pyname(name)}[0]"                       # a scalar target lives in a one-element box; a pointer refers to one
        if sym is not None and sym.eq is not None and not sym.rank:
            return f"_eq{sym.eq[0]}[{sym.eq[1]}]"             # EQUIVALENCE: an element of the shared storage
        if sym is not None and sym.module:
            return f"{sym.module}_{name}"                     # MODULE entity: a variable of the generated module
        if sym is not None and sym.saved:
            return f"_save_{self.unit.name}_{name}"           # static storage: a module-level variable
        return pyname(name)

    def _shift(self, idx, lb_text: str):
        lb = self.parse(lb_text)
        if isinstance(lb, Num) and lb.is_int:
            return add_const(idx, -int(lb.text))
        return Binary("-", idx, lb)

    def subscripts(self, sym, args) -> str:
        saved, self._ring = self._ring, 1          # an index is never wrapped: it must be in range anyway
        try:
            return self._subscripts(sym, args)
        finally:
            self._ring = saved

    def _subscripts(self, sym, args) -> str:
        if len(args) != sym.rank:
            raise FortranParseError(f"array {sym.name} used with {len(args)} subscripts, declared rank {sym.rank}")
        parts = []
        for (lb, _ub), a in zip(sym.dims, args):
            if isinstance(a, Slice):
                lo = "" if a.lo is None else self.ex(self._shift(a.lo, lb))[0]
                if a.hi is None:
                    hi = ""
                else:
                    lbe = self.parse(lb)
                    hi_e = add_const(a.hi, 1 - int(lbe.text)) if isinstance(lbe, Num) and lbe.is_int \
                        else Binary("+", Binary("-", a.hi, lbe), Num("1", True))
                    hi = self.ex(hi_e)[0]
                parts.append(f"{lo}:{hi}")
            else:
                parts.append(self.ex(self._shift(a, lb))[0])
        return ", ".join(parts)

    def is_arrayish(self, a) -> bool:
        if isinstance(a, Slice):
            return True
        if isinstance(a, Name):
            s = self.unit.symbols.get(a.name)
            return bool(s and s.rank)
        if isinstance(a, Ref):
            s = self.unit.symbols.get(a.name)
            return bool(s and s.rank and any(isinstance(x, Slice) for x in a.args))
        return False

    def ref(self, e: Ref) -> Tuple[str, int]:
        sym = self.unit.symbols.get(e.name)
        user = self.units.get(e.name)
        if sym is not None and sym.rank:
            return f"{self.name_ref(e.name)}[{self.subscripts(sym, e.args)}]", _P_ATOM
        if e.name in self.unit.types and sym is None and not user:
            return self.structure_constructor(e), _P_ATOM
        key = e.name.upper()
        if key in KIND_INQUIRY and not user:
            return str(kind_call_value(e, self.unit, self.func_types, self.units)), _P_ATOM
        if key in FORTRAN_INTRINSICS and not (user and user.kind == "FUNCTION"):
            return self.intrinsic(key, e), _P_ATOM
        if key in STANDARD_INTRINSIC_NAMES and not user:
            raise FortranParseError(f"intrinsic {key} is not translated")
        saved, self._ring = self._ring, 0
        try:
            args = ", ".join(self.actual_argument(a, user, pos) for pos, a in enumerate(e.args))
        finally:
            self._ring = saved
        extra = [blk_var(b) for b in user.passed_commons()] if user else []
        return f"{pyname(e.name)}({', '.join([x for x in [args] if x] + extra)})", _P_ATOM

    def intrinsic(self, key: str, e: Ref) -> str:
        saved, self._ring = self._ring, 0           # each argument starts a new wrap context
        try:
            return self._intrinsic(key, e)
        finally:
            self._ring = saved

    def inquiry(self, key: str, arg) -> str:
        """EPSILON / HUGE / TINY of the kind of `arg`."""
        have, width = self.type_of(arg), self.bits(arg)
        if have == INT and key == "HUGE":
            return str(2 ** (width - 1) - 1)
        if have != REAL or key not in ("EPSILON", "HUGE", "TINY"):
            raise FortranParseError(f"{key} of a non-real argument is not translated")
        attr = {"EPSILON": "eps", "HUGE": "max", "TINY": "tiny"}[key]
        dtype = "np.float32" if width == 32 else "np.float64"
        return f"{dtype}(np.finfo({dtype}).{attr})" if width == 32 else f"float(np.finfo({dtype}).{attr})"

    def _intrinsic(self, key: str, e: Ref) -> str:
        vec = any(self.is_arrayish(a) for a in e.args)
        pieces = [self.ex(a) for a in e.args]
        types = [self.type_of(a) for a in e.args]
        strs = [t for t, _ in pieces]
        if key == "MOD" and len(strs) == 2 and all(t == INT for t in types):
            self.helpers.update(("_imod", "_idiv"))
            return self.wrap_int(f"_imod({strs[0]}, {strs[1]})", e)
        arg_complex = bool(e.args) and types and types[0] == COMPLEX
        if key in ("REAL", "DBLE", "DREAL") and len(strs) == 1 and arg_complex:
            return f"float(({strs[0]}).real)" if key != "REAL" else f"({strs[0]}).real"
        if key in ("AIMAG", "DIMAG") and len(strs) == 1:
            return f"float(({strs[0]}).imag)" if key == "DIMAG" else f"({strs[0]}).imag"
        if key in ("CONJG", "DCONJG") and len(strs) == 1:
            return f"({strs[0]}).conjugate()"
        if key in ("CMPLX", "DCMPLX") and 1 <= len(strs) <= 3 and not vec:
            width = self.bits(e)
            if len(strs) == 1 and arg_complex:
                inner = f"complex({strs[0]})"
            else:
                imag = f"float({strs[1]})" if len(strs) >= 2 else "0.0"
                inner = f"complex(float({strs[0]}), {imag})"
            return f"{_NP_CPLX64}({inner})" if width == 32 else inner
        if self.type_of(e) == COMPLEX and key in ("SQRT", "EXP", "LOG", "SIN", "COS", "TAN", "SINH", "COSH", "TANH",
                                                  "ASIN", "ACOS", "ATAN") and len(strs) == 1:
            return f"np.{ {'ASIN': 'arcsin', 'ACOS': 'arccos', 'ATAN': 'arctan'}.get(key, key.lower()) }({strs[0]})"
        if key in ("SUM", "PRODUCT") and len(strs) == 1:
            name = "_fsum" if key == "SUM" else "_fprod"
            self.helpers.add(name)
            return f"{name}({strs[0]})"
        if key == "MATMUL" and len(strs) == 2:
            self.helpers.add("_fmatmul")
            self.imports.update(("ctypes", "ctypes.util", "os"))
            return f"_fmatmul({strs[0]}, {strs[1]})"
        if key == "DOT_PRODUCT" and len(strs) == 2:
            self.helpers.update(("_fdot", "_fsum"))
            return f"_fdot({strs[0]}, {strs[1]})"
        if key in ("ASSOCIATED", "ALLOCATED") and len(e.args) == 1 and isinstance(e.args[0], Name):
            return f"({pyname(e.args[0].name)} is not None)"
        if key == "SIZE" and 1 <= len(strs) <= 2 and not vec or key == "SIZE" and len(strs) >= 1:
            return f"np.size({strs[0]})" if len(strs) == 1 else f"np.shape({strs[0]})[{strs[1]} - 1]"
        if key in ("EPSILON", "HUGE", "TINY") and len(e.args) == 1:
            return self.inquiry(key, e.args[0])
        if key == "SIGN" and len(strs) == 2 and all(t == INT for t in types) and not vec:
            return f"(abs({strs[0]}) if {strs[1]} >= 0 else -abs({strs[0]}))"
        if key in ("NINT", "IDNINT", "ANINT", "DNINT") and len(strs) == 1 and not vec:
            self.imports.add("math")
            arg = f"float({strs[0]})" if self.bits(e.args[0]) == 32 and types[0] == REAL else strs[0]
            body = f"math.copysign(math.floor(abs({arg}) + 0.5), {arg})"
            code = f"int({body})" if key in ("NINT", "IDNINT") else body
            return f"{_NP_REAL32}({code})" if self.type_of(e) == REAL and self.bits(e) == 32 else code
        tmpl = FORTRAN_INTRINSICS[key]["python_vector" if vec else "python_scalar"]
        if re.search(r"\{\d\}\s*[^\s,)}]", tmpl):
            strs = [_wrap(t, p, _P_ATOM) for t, p in pieces]
        code, module = translate_intrinsic(key, strs, target="python", vectorized=vec)
        if module:
            self.imports.add(module)
        if not vec and re.search(r"\bmath\.(?:sqrt|log|log10|exp|sin|cos|tan|asin|acos|sinh|cosh|fmod)\(", code):
            code = re.sub(r"\bmath\.(sqrt|log|log10|exp|sin|cos|tan|asin|acos|sinh|cosh|fmod)\(", r"_fm.\1(", code)
            self.helpers.add("_fm")
        for lib in ("cmath", "math", "np"):
            if f"{lib}." in code:
                self.imports.add({"np": "numpy as np"}.get(lib, lib))
        if (not vec and self.type_of(e) == REAL and self.bits(e) == 32
                and key not in ("ABS", "MAX", "MIN", "AMAX1", "AMIN1")):
            code = f"{_NP_REAL32}({code})"              # math.* computes in double: round the result to single
        return code

    # ------------------------------------------------------------------ l-values and conversion
    def lvalue(self, lhs) -> str:
        if isinstance(lhs, Component):
            return self.component_text(lhs)
        if isinstance(lhs, Name):
            sym = self.unit.symbols.get(lhs.name)
            if sym is not None and sym.rank:
                return f"{self.name_ref(lhs.name)}[...]"
            return self.name_ref(lhs.name)
        if isinstance(lhs, Ref):
            sym = self.unit.symbols.get(lhs.name)
            if sym is not None and sym.rank:
                return f"{self.name_ref(lhs.name)}[{self.subscripts(sym, lhs.args)}]"
        raise FortranParseError("assignment target is not a variable or array element")

    def lvalue_or_none(self, e) -> Optional[str]:
        """Assignable Python target for an actual argument, or None (constants, expressions, whole arrays)."""
        if isinstance(e, Component):
            return self.lvalue(e)
        sym = self.unit.symbols.get(getattr(e, "name", ""))
        if sym is None or sym.const_value is not None:
            return None
        if isinstance(e, Name) and not sym.rank:
            return self.lvalue(e)
        if isinstance(e, Ref) and sym.rank and not any(isinstance(a, Slice) for a in e.args):
            return self.lvalue(e)
        return None

    def target_bits(self, lhs) -> int:
        if isinstance(lhs, Component):
            comp = component_of(lhs, self.unit)
            return comp.bits if comp is not None and comp.bits else 32
        sym = self.unit.symbols.get(getattr(lhs, "name", ""))
        if sym is None or not sym.bits:
            return 32
        if sym.bits < 0:
            raise FortranParseError(f"kind of {sym.name} could not be resolved")
        return sym.bits

    def convert(self, text: str, expr, target_type: str, target_bits: int = 64) -> str:
        """Assignment conversion of `expr` (emitted as `text`) to a variable of the given type and width."""
        have, hb = self.type_of(expr), self.bits(expr)
        if target_type == COMPLEX and have in (INT, REAL, COMPLEX):
            if have == COMPLEX and hb == target_bits:
                return text
            return f"{_NP_CPLX64}({text})" if target_bits == 32 else f"complex({text})"
        if target_type == INT:
            if have == REAL:
                return self.wrap_int(f"int({text})", expr) if target_bits < 64 else f"int({text})"
            if have == INT and hb > target_bits:
                self.helpers.add("_i32")
                return f"_i32({text})"
            return text
        if target_type == REAL:
            if have == INT:
                if target_bits == 32:
                    return f"{_NP_REAL32}({text})"
                return text + ".0" if isinstance(expr, Num) else f"float({text})"
            if have == REAL and hb != target_bits:
                return f"{_NP_REAL32}({text})" if target_bits == 32 else f"float({text})"
        return text

    def static_definitions(self) -> List[str]:
        """Module-level definitions of this unit's static (DATA / SAVE) variables. Variables with no DATA start at zero,
        as static storage does."""
        out = []
        for name, sym in self.unit.symbols.items():
            if self.unit.kind == "MODULE" and sym.const_value is not None:
                e = self.parse(sym.const_value)
                out.append(f"{self.name_ref(name)} = {self.convert(self.ex(e)[0], e, sym.type, self.target_bits(Name(name)))}")
                continue
            if not sym.saved:
                continue
            if sym.allocatable or sym.pointer:
                out.append(f"{self.name_ref(name)} = None")
                continue
            values = self.unit.data_values.get(name)
            bits = self.target_bits(Name(name))
            dtype = self.nptype(sym)
            if sym.rank:
                shape = common_extents(sym, self.unit)
                shape_txt = f"({shape[0]},)" if len(shape) == 1 else f"({', '.join(map(str, shape))})"
                if values is None:
                    value = f'np.zeros({shape_txt}, dtype={dtype}, order="F")'
                else:
                    items = ", ".join(self.convert(self.ex(v)[0], v, sym.type, bits) for v in values)
                    value = f"np.array([{items}], dtype={dtype})"
                    if len(shape) > 1:
                        value += f'.reshape({shape_txt}, order="F")'      # DATA fills in column-major order
            elif values is None:
                value = {INT: "0", REAL: f"{_NP_REAL32}(0.0)" if bits == 32 else "0.0", LOGICAL: "False", CHAR: "''",
                         COMPLEX: f"{_NP_CPLX64}(0)" if bits == 32 else "0j"}[sym.type]
            else:
                value = self.convert(self.ex(values[0])[0], values[0], sym.type, bits)
            out.append(f"{self.name_ref(name)} = {value}")
        return out

    @staticmethod
    def pytype(sym) -> str:
        if sym.type == DERIVED:
            return f"{sym.derived}_t"
        if sym.type == COMPLEX and sym.bits == 32:
            return _NP_CPLX64
        return _NP_REAL32 if sym.type == REAL and sym.bits == 32 else _PYTYPE[sym.type]

    @staticmethod
    def nptype(sym) -> str:
        if sym.bits < 0:
            raise FortranParseError(f"kind of {sym.name} could not be resolved")
        if sym.type == COMPLEX and sym.bits == 32:
            return _NP_CPLX64
        return _NP_REAL32 if sym.type == REAL and sym.bits == 32 else _NPTYPE[sym.type]

    # ------------------------------------------------------------------ unit scaffolding
    def returns(self) -> str:
        names = []
        if self.unit.kind == "FUNCTION":
            names.append(self.name_ref(self.unit.name))
        names += [self.name_ref(p) for p in self.unit.modified_scalars]
        return ", ".join(names)

    def begin_unit(self):
        u = self.unit
        args = []
        for p in u.params:
            if p in u.out_only:
                continue               # INTENT(OUT) scalar: a result, not an input
            sym = u.symbols[p]
            args.append(f"{pyname(p)}: {'np.ndarray' if sym.rank else self.pytype(sym)}")
        owned = set(u.commons) if u.kind == "PROGRAM" else set(u.owned_blocks)   # the unit creates these blocks itself
        for blk in sorted(u.commons - owned):
            args.append(f"{blk_var(blk)}: {self.blocks_info[blk].struct_name}")
        out_types = ([self.pytype(u.symbols[u.name])] if u.kind == "FUNCTION" else []) + \
                    [self.pytype(u.symbols[p]) for p in u.modified_scalars]
        ann = "" if not out_types else (f" -> {out_types[0]}" if len(out_types) == 1
                                         else f" -> tuple[{', '.join(out_types)}]")
        fname = "main" if u.kind == "PROGRAM" else pyname(u.name)
        self.line(f"def {fname}({', '.join(args)}){ann}:")
        self.level = 1
        doc = f"Translated from Fortran {u.kind} {u.name}."
        rets = self.returns()
        if rets:
            doc += f"\n{self.INDENT}Returns: {rets} (Fortran scalar results / modified scalar arguments, in order)."
        self.line(f'r"""{doc}"""')
        for n, sy in u.symbols.items():                 # the host's PARAMETERs, as seen from an internal procedure
            if sy.host_const:
                e = self.parse(sy.const_value)
                self.line(f"{pyname(n)} = {self.convert(self.ex(e)[0], e, sy.type, self.target_bits(Name(n)))}")
        statics = [self.name_ref(n) for n, sy in u.symbols.items()
                   if sy.saved or (sy.module and sy.const_value is None)]
        if statics:
            self.line(f"global {', '.join(statics)}")
        for gid, group in enumerate(u.eq_groups):
            dtype = _NP_REAL32 if group["type"] == REAL and group["bits"] == 32 else _NPTYPE[group["type"]]
            self.line(f"_eq{gid} = np.zeros({group['size']}, dtype={dtype})        # EQUIVALENCE storage")
        for message in u.common_conflicts:
            self.do_unsupported(message, "COMMON")
        for message in u.static_problems:
            self.do_unsupported(message, "DATA/SAVE")
        for blk in sorted(set(u.commons) if u.kind == "PROGRAM" else set(u.owned_blocks)):
            self.line(f"{blk_var(blk)} = {self.blocks_info[blk].struct_name}()")
        self._init_call_outputs()
        zero = {INT: "0", REAL: "0.0", LOGICAL: "False", CHAR: "''", COMPLEX: "0j"}
        for p in u.out_only:
            sym = u.symbols[p]
            if sym.type == DERIVED:
                continue
            single = sym.type == REAL and sym.bits == 32
            self.line(f"{pyname(p)} = {_NP_REAL32 + '(0.0)' if single else _NP_CPLX64 + '(0)' if sym.type == COMPLEX and sym.bits == 32 else zero[sym.type]}")

    def _init_call_outputs(self) -> None:
        """A local scalar first set by the callee (`call counter(n)`) is unbound in Python at the call: its Fortran value is
        undefined, so start it at zero."""
        u = self.unit
        names: List[str] = []
        for st in u.stmts:
            text = st.raw if st.stmt_type == "CALL" else (st.details.get("statement", "") if st.stmt_type == "IF_STMT" else "")
            m = re.match(r"^CALL\s+\w+\s*\((.*)\)\s*$", st.raw if st.stmt_type == "CALL" else text, re.IGNORECASE)
            if not m:
                continue
            for arg in split_top_level(m.group(1)):
                arg = arg.strip().lower()
                sym = u.symbols.get(arg)
                if (re.fullmatch(r"[a-z_]\w*", arg) and sym is not None and not sym.rank and not sym.is_dummy
                        and sym.type != DERIVED
                        and not sym.common_block and not sym.saved and sym.const_value is None
                        and arg not in self.units and arg != u.name and arg not in names):
                    names.append(arg)
        zero = {INT: "0", REAL: "0.0", LOGICAL: "False", CHAR: "''", COMPLEX: "0j"}
        for n in names:
            sym = u.symbols[n]
            single = sym.type == REAL and sym.bits == 32
            self.line(f"{pyname(n)} = {_NP_REAL32 + '(0.0)' if single else _NP_CPLX64 + '(0)' if sym.type == COMPLEX and sym.bits == 32 else zero[sym.type]}")

    def end_unit(self):
        while self.blocks:
            self.close_block()
        rets = self.returns()
        last = next((l for l in reversed(self.lines) if l.strip()), "")
        top_level_return = last.startswith(self.INDENT + "return") and not last.startswith(self.INDENT * 2)
        if rets and not top_level_return:
            self.line(f"return {rets}")
        self.level = 0
        self.line()
        self.line()

    # ------------------------------------------------------------------ declarations
    def alloc(self, name: str) -> None:
        sym = self.unit.symbols.get(name)
        if not sym or not sym.rank or sym.is_dummy or sym.common_block or sym.saved or sym.module or name in self.allocated:
            return
        if sym.allocatable or sym.pointer:
            self.allocated.add(name)
            self.line(f"{pyname(name)} = None")                 # unallocated / unassociated until ALLOCATE or p => target
            return
        if sym.type == DERIVED:
            if sym.rank != 1:
                raise FortranParseError("arrays of derived type are translated for rank 1 only")
            lb, ub = (self.parse(x) for x in sym.dims[0])
            self.allocated.add(name)
            self.line(f"{pyname(name)} = [{sym.derived}_t() for _ in range({self.ex(Binary('+', Binary('-', ub, lb), Num('1', True)))[0]})]")
            return
        if sym.eq is not None:                        # EQUIVALENCE: a view of the shared storage
            shape = common_extents(sym, self.unit)
            size = 1
            for n in shape:
                size *= n
            gid, off = sym.eq
            shape_txt = f"({shape[0]},)" if len(shape) == 1 else f"({', '.join(map(str, shape))})"
            self.allocated.add(name)
            self.line(f'{pyname(name)} = _eq{gid}[{off}:{off + size}].reshape({shape_txt}, order="F")')
            return
        extents = []
        for lb, ub in sym.dims:
            if ub.strip() == "*":
                self.do_unsupported("assumed-size local array", name)
                return
            lbe, ube = self.parse(lb), self.parse(ub)
            if isinstance(lbe, Num) and lbe.is_int:
                ext = add_const(ube, 1 - int(lbe.text))
            else:
                ext = Binary("+", Binary("-", ube, lbe), Num("1", True))
            extents.append(self.ex(ext)[0])
        shape = extents[0] if len(extents) == 1 else f"({', '.join(extents)})"
        order = "" if len(extents) == 1 else ', order="F"'
        self.allocated.add(name)
        self.line(f"{pyname(name)} = np.zeros({shape}, dtype={self.nptype(sym)}{order})")

    def on_TYPE_DECL(self, d, raw):
        for item in split_top_level(d["declarations"]):
            try:
                name, _dim, init = _split_decl_item(item)
            except FortranParseError:
                continue
            sym = self.unit.symbols[name]
            if sym.rank:
                self.alloc(name)
            elif sym.pointer and not sym.rank and not sym.is_dummy:
                self.line(f"{pyname(name)} = None")
            elif sym.target and sym.type != DERIVED and not sym.is_dummy and not (sym.saved or sym.module or sym.common_block):
                box = f"np.zeros(1, dtype={self.nptype(sym)})"
                if init is not None:
                    e = self.parse(init)
                    box = f"np.array([{self.convert(self.ex(e)[0], e, sym.type, self.target_bits(Name(name)))}], dtype={self.nptype(sym)})"
                self.line(f"{pyname(name)} = {box}")
            elif sym.type == DERIVED and not sym.is_dummy and not sym.common_block and not sym.module and not sym.saved:
                e = self.parse(init) if init is not None else None
                self.line(f"{pyname(name)} = {self.ex(e)[0] if e is not None else sym.derived + '_t()'}")
            elif init is not None and not sym.is_dummy and not sym.common_block and not sym.saved and not sym.module:
                e = self.parse(init)
                self.line(f"{pyname(name)} = {self.convert(self.ex(e)[0], e, sym.type, self.target_bits(Name(name)))}")

    def on_DIMENSION(self, d, raw):
        for item in split_top_level(d["raw"]):
            try:
                name, _dim, _ = _split_decl_item(item)
            except FortranParseError:
                continue
            self.alloc(name)

    def on_PARAMETER(self, d, raw):
        for k, v in d["constants"].items():
            e = self.parse(v)
            sym = self.unit.sym(k)
            self.line(f"{pyname(k)} = {self.convert(self.ex(e)[0], e, sym.type, self.target_bits(Name(k)))}")

    # ------------------------------------------------------------------ control flow hooks
    def _as_int(self, e) -> str:
        text = self.ex(e)[0]
        return text if self.type_of(e) == INT else f"int({text})"

    def _range_end(self, end, inc: int) -> str:
        """Exclusive end for range(): Fortran's inclusive bound moved one past in the loop direction."""
        if self.type_of(end) == INT:
            if isinstance(end, Num):
                return self.ex(add_const(end, inc))[0]
            text, prec = self.ex(end)           # the +/-1 is not wrapped: end + 1 must not overflow to a negative bound
            return f"{_wrap(text, prec, _P_ADD)} {'+' if inc > 0 else '-'} 1"
        return f"int({self.ex(end)[0]}) {'+' if inc > 0 else '-'} 1"

    def open_do(self, var, start, end, step, raw):
        v = self.ex(var)[0]
        s_txt = self._as_int(start)
        step_lit = None
        if isinstance(step, Num) and step.is_int:
            step_lit = step.text
        elif isinstance(step, Unary) and step.op == "-" and isinstance(step.operand, Num) and step.operand.is_int:
            step_lit = "-" + step.operand.text
        if step_lit is not None:
            if int(step_lit) == 0:
                self.do_unsupported("DO loop with zero step", raw)
                self.line("for _ in ():")
                self._do_stack.append(None)
                return
            inc = 1 if int(step_lit) > 0 else -1
            tail = "" if step_lit == "1" else f", {step_lit}"
            rng = f"range({s_txt}, {self._range_end(end, inc)}{tail})"
        else:
            st = self._as_int(step)
            rng = f"range({s_txt}, {self._as_int(end)} + (1 if {st} > 0 else -1), {st})"
        if self.type_of(var) == REAL:
            self._open_real_do(var, v, start, end, step)
            return
        if self.type_of(var) != INT:
            self._do_stack.append(None)
            self.line(f"for {v} in {rng}:")
            return
        # The trip count is fixed on entry; on normal completion the variable is start + trips * step (also for zero
        # trips), and EXIT leaves it at the current value -- hence for/else on a named range object.
        self._do_counter += 1
        tmp = f"_do{self._do_counter}"
        self._do_stack.append((v, f"{tmp}.start + len({tmp}) * {tmp}.step"))
        self.line(f"{tmp} = {rng}")
        self.line(f"for {v} in {tmp}:")

    def _open_real_do(self, var, v, start, end, step):
        """DO with a REAL variable: see _fdo. The bounds are converted to the variable's precision first."""
        width = self.target_bits(var)
        args = ", ".join(self.convert(self.ex(x)[0], x, REAL, width) for x in (start, end, step))
        self.helpers.add("_fdo")
        self._do_counter += 1
        tmp = f"_do{self._do_counter}"
        self._do_stack.append((v, f"{tmp}_after"))
        self.line(f"{tmp}_values, {tmp}_after = _fdo({args})")
        self.line(f"for {v} in {tmp}_values:")

    def open_while(self, cond, raw):
        self.line("while True:" if cond is None and raw.strip().upper() == "DO" else f"while {self._cond_text(cond, raw)}:")

    def open_if(self, cond, raw):
        self.line(f"if {self._cond_text(cond, raw)}:")

    def open_elif(self, cond, raw):
        self.line(f"elif {self._cond_text(cond, raw)}:")

    def open_else(self):
        self.line("else:")

    def _cond_text(self, cond, raw) -> str:
        if cond is None:
            self.uses_untranslated_helper = True
            return f"_fortran_untranslated({raw!r})"
        return self.ex(cond)[0]

    def _pass_if_empty(self, empty: bool) -> None:
        if empty:
            self.level += 1
            self.line("pass")
            self.level -= 1

    def finish_block(self, kind, empty):
        self._pass_if_empty(empty)
        if kind == "do":
            entry = self._do_stack.pop() if self._do_stack else None
            if entry is not None:
                v, after = entry
                self.line("else:")
                self.level += 1
                self.line(f"{v} = {after}")
                self.level -= 1

    def finish_branch(self, empty):
        self._pass_if_empty(empty)

    # ------------------------------------------------------------------ simple statement hooks
    def do_assign(self, lhs, rhs, raw):
        target = self.lvalue(lhs)
        text = self.ex(rhs)[0]
        lhs_type = self.type_of(lhs if isinstance(lhs, (Name, Component)) else Name(lhs.name))
        if lhs_type == DERIVED:
            self.helpers.add("_dassign")
            self.imports.add("copy")
            self.line(f"_dassign({target}, {text})")
            return
        sym = self.unit.symbols.get(getattr(lhs, "name", ""))
        if lhs_type == CHAR and sym is not None and sym.char_len and not sym.rank:
            self.helpers.add("_cfix")
            text = f"_cfix({text}, {sym.char_len})"          # CHARACTER(n): truncate or pad with blanks
        self.line(f"{target} = {self.convert(text, rhs, lhs_type, self.target_bits(lhs))}")

    def do_call(self, callee, args, raw):
        cu = self.units.get(callee)
        for a in args:
            if isinstance(a, Slice):
                raise FortranParseError("array section as CALL argument")
        arg_strs = [self.actual_argument(a, cu, pos) for pos, a in enumerate(args)
                    if cu is None or pos >= len(cu.params) or cu.params[pos] not in cu.out_only]
        if cu is None:
            self.line(f"{pyname(callee)}({', '.join(arg_strs)})")
            return
        arg_strs += [blk_var(b) for b in cu.passed_commons()]
        call = f"{pyname(callee)}({', '.join(arg_strs)})"
        targets = []
        for formal in cu.modified_scalars:
            pos = cu.params.index(formal)
            targets.append((self.lvalue_or_none(args[pos]) if pos < len(args) else None) or "_")
        self.line(f"{', '.join(targets)} = {call}" if targets else call)

    def do_return(self):
        rets = self.returns()
        self.line(f"return {rets}" if rets else "return")

    def do_stop(self, text, raw):
        if not text:
            self.line("raise SystemExit(0)")
        else:
            self.line(f"raise SystemExit({self.ex(self.parse(text))[0]})")

    def do_exit(self):
        self.line("break")

    def do_cycle(self):
        self.line("continue")

    def do_goto(self, labels, selector, raw):
        self.do_unsupported("GO TO is not translated; restructure the control flow first (restructure-goto skill)", raw)

    def do_arith_if(self, expr, neg, zero, pos, raw):
        self.do_unsupported("arithmetic IF is not translated; restructure the control flow first (restructure-goto skill)", raw)

    def do_print(self, items, raw):
        # a whole array prints element by element in array (column-major) order at full precision;
        # print(ndarray) would round to NumPy's 8 printed digits
        parts = []
        for i in items:
            if self.type_of(i) == DERIVED:
                raise FortranParseError("printing a derived-type value is not translated; print its components")
            if isinstance(i, ImpliedDo):
                parts.append(self._ctor_part(i, self.type_of(i), self.bits(i)))
            elif self.type_of(i) == COMPLEX and not (self.is_arrayish(i) or self._array_valued(i)):
                z = self.ex(i)[0]
                parts.append(f'f"({{float(({z}).real)!r}},{{float(({z}).imag)!r}})"')       # Fortran: (re,im)
            elif self.is_arrayish(i) or self._array_valued(i):
                parts.append(f'*np.ravel({self.ex(i)[0]}, order="F")')
            else:
                parts.append(self.ex(i)[0])
        self.line(f"print({', '.join(parts)})")

    def _shape_text(self, sym, args) -> str:
        counts = []
        for a in args:
            if isinstance(a, Slice):
                counts.append(self.ex(Binary("+", Binary("-", a.hi, a.lo if a.lo is not None else Num("1", True)), Num("1", True)))[0])
            else:
                counts.append(self.ex(a)[0])
        return f"({counts[0]},)" if len(counts) == 1 else f"({', '.join(counts)})"

    def do_allocate(self, objects, options, raw):
        for obj in objects:
            sym = self.unit.symbols.get(getattr(obj, "name", ""))
            if isinstance(obj, Name) and sym is not None and sym.pointer and not sym.rank:
                self.line(f"{pyname(obj.name)} = " + (f"{sym.derived}_t()" if sym.type == DERIVED
                                                       else f"np.zeros(1, dtype={self.nptype(sym)})"))
                continue
            if not isinstance(obj, Ref) or sym is None or not (sym.allocatable or sym.pointer):
                raise FortranParseError("ALLOCATE of something other than an allocatable or pointer array")
            if sym.type == DERIVED:
                count = self._shape_text(sym, obj.args)[1:-2] if len(obj.args) == 1 else None
                self.line(f"{self.name_ref(obj.name)} = [{sym.derived}_t() for _ in range({count})]")
            else:
                self.line(f'{self.name_ref(obj.name)} = np.zeros({self._shape_text(sym, obj.args)}, dtype={self.nptype(sym)}, order="F")')
        for opt in options:
            m = re.match(r"^STAT\s*=\s*(.+)$", opt, re.IGNORECASE)
            if m:
                self.line(f"{self.ex(self.parse(m.group(1)))[0]} = 0")          # allocation always succeeds here

    def do_deallocate(self, objects, options, raw, nullify=False):
        for obj in objects:
            sym = self.unit.symbols.get(getattr(obj, "name", ""))
            raw_name = pyname(obj.name) if isinstance(obj, Name) and sym is not None and sym.pointer else self.ex(obj)[0]
            self.line(f"{raw_name} = None")
        for opt in options:
            m = re.match(r"^STAT\s*=\s*(.+)$", opt, re.IGNORECASE)
            if m:
                self.line(f"{self.ex(self.parse(m.group(1)))[0]} = 0")

    def do_pointer_assign(self, lhs, rhs, raw):
        sym = self.unit.symbols.get(getattr(lhs, "name", ""))
        if not isinstance(lhs, Name) or sym is None or not sym.pointer:
            raise FortranParseError("pointer assignment to something other than a pointer")
        if isinstance(rhs, Ref) and rhs.name == "null" and not rhs.args:
            self.line(f"{pyname(lhs.name)} = None")
            return
        if not sym.rank:
            if sym.type == DERIVED:                                 # the object itself is the reference
                self.line(f"{pyname(lhs.name)} = {self.ex(rhs)[0]}")
                return
            target = self.unit.symbols.get(getattr(rhs, "name", ""))
            if not isinstance(rhs, Name) or target is None or not (target.pointer or target.target) or target.rank:
                raise FortranParseError("a pointer to a scalar can point to a scalar variable with the TARGET attribute")
            self.line(f"{pyname(lhs.name)} = {pyname(rhs.name)}")            # share the box
            return
        if not self.array_valued(rhs) or isinstance(rhs, (Binary, Unary)):
            raise FortranParseError("a pointer array can point to an array or a section of one")
        self.line(f"{self.name_ref(lhs.name)} = {self.ex(rhs)[0]}")          # an alias: NumPy arrays and slices are views

    # ------------------------------------------------------------------ READ, WRITE to files, OPEN, CLOSE, ...
    def _io_value(self, text: str, default: str = "None") -> str:
        return default if text is None else self.ex(self.parse(text))[0]

    def _unit_text(self, io, standard: int) -> str:
        return str(standard) if io["unit"] in (None, "*") else self.ex(self.parse(io["unit"]))[0]

    def _iostat_line(self, io, value: str) -> None:
        target = io["controls"].get("iostat")
        if target:
            self.line(f"{self.lvalue(self.parse(target))} = {value}")
        elif io["controls"].get("end") or io["controls"].get("err"):
            self.line(f"_ios = {value}")
        else:
            self.line(value)

    def do_io(self, io, fmt, raw):
        self.helpers.update(("_fio", "_fwrite"))
        self.imports.update(("math", "re", "os", "sys", "tempfile"))
        kind, c = io["kind"], io["controls"]
        if kind == "READ":
            self._do_read(io, fmt, raw)
        elif kind in ("WRITE", "PRINT"):
            self._do_write_unit(io, fmt, raw)
        elif kind == "OPEN":
            for unsupported in ("newunit", "recl"):
                if unsupported in c:
                    raise FortranParseError(f"OPEN with {unsupported}= is not translated")
            if c.get("access", "'sequential'").strip("'\" ").lower() != "sequential" or \
                    c.get("form", "'formatted'").strip("'\" ").lower() != "formatted":
                raise FortranParseError("only sequential formatted files are translated")
            args = [self._unit_text(io, 6), f"file={self._io_value(c.get('file'))}"]
            for key in ("status", "action", "position"):
                if key in c:
                    args.append(f"{key}={self._io_value(c[key])}")
            self._iostat_line(io, f"_fopen({', '.join(args)})")
        elif kind == "CLOSE":
            status = f", status={self._io_value(c['status'])}" if "status" in c else ""
            self._iostat_line(io, f"_fclose({self._unit_text(io, 6)}{status})")
        elif kind == "REWIND":
            self._iostat_line(io, f"_frewind({self._unit_text(io, 6)})")
        elif kind == "INQUIRE":
            if set(c) - {"file", "exist", "iostat"} or "exist" not in c or "file" not in c:
                raise FortranParseError("only INQUIRE(FILE=..., EXIST=...) is translated")
            self.line(f"{self.lvalue(self.parse(c['exist']))} = _fexists({self._io_value(c['file'])})")
        else:
            raise FortranParseError(f"{kind} is not translated")

    def _item_parts(self, items) -> str:
        parts = []
        for i in items:
            if isinstance(i, ImpliedDo):
                parts.append(self._ctor_part(i, self.type_of(i), self.bits(i)))
            elif self.is_arrayish(i) or self._array_valued(i):
                parts.append(f'*np.ravel({self.ex(i)[0]}, order="F")')
            else:
                parts.append(self.ex(i)[0])
        return ", ".join(parts)

    def _do_write_unit(self, io, fmt, raw):
        items = [self.parse(i) for i in io["items"]]
        unit = self._unit_text(io, 6)
        text = f"_flist([{self._item_parts(items)}])" if fmt is None else \
            f"_fwrite({repr(fmt[1]) if fmt[0] == 'string' else self.ex(fmt[1])[0]}, [{self._item_parts(items)}])"
        self._iostat_line(io, f"_fwriteunit({unit}, {text})") if io["controls"] else self.line(f"_fwriteunit({unit}, {text})")

    def _read_kind(self, target):
        t = self.type_of(target)
        if t == INT:
            return "i"
        if t == REAL:
            return "r"
        if t == LOGICAL:
            return "l"
        if t == CHAR:
            return "a"
        raise FortranParseError("READ of this type is not translated")

    def _read_convert(self, value: str, target) -> str:
        t, bits = self.type_of(target), self.bits(target)
        if t == REAL:
            return f"{_NP_REAL32}({value})" if bits == 32 else f"float({value})"
        if t == CHAR:
            sym = self.unit.symbols.get(getattr(target, "name", ""))
            if sym is not None and sym.char_len and not sym.rank:
                self.helpers.add("_cfix")
                return f"_cfix({value}, {sym.char_len})"
        return value

    def _read_item(self, rd: str, item) -> None:
        if isinstance(item, ImpliedDo):
            saved, self._ring = self._ring, 1
            try:
                rng = self.range_text(item.lo, item.hi, item.step)
            finally:
                self._ring = saved
            self.line(f"for {self.name_ref(item.var)} in {rng}:")
            self.level += 1
            for sub in item.items:
                self._read_item(rd, sub)
            self.level -= 1
            return
        if self.array_valued(item):
            if not isinstance(item, Name):
                raise FortranParseError("READ into an array section or expression is not translated")
            sym = self.unit.symbols[item.name]
            kind, target = self._read_kind(item), self.name_ref(item.name)
            width = f", {sym.char_len}" if kind == "a" and sym.char_len else ""
            self.line(f"_vals = [{rd}.get('{kind}'{width}) for _ in range(np.size({target}))]")
            self.line(f'if {rd}.ok and None not in _vals:')
            self.line(f'    {target}[...] = np.array(_vals, dtype={self.nptype(sym)}).reshape(np.shape({target}), order="F")')
            return
        kind = self._read_kind(item)
        sym = self.unit.symbols.get(getattr(item, "name", ""))
        width = f", {sym.char_len}" if kind == "a" and sym is not None and sym.char_len else ""
        self.line(f"_v = {rd}.get('{kind}'{width})")
        self.line("if _v is not None:")
        self.line(f"    {self.lvalue(item)} = {self._read_convert('_v', item)}")

    def _do_read(self, io, fmt, raw):
        c = io["controls"]
        self._rd_counter = getattr(self, "_rd_counter", 0) + 1
        rd = f"_rd{self._rd_counter}"
        text = "None" if fmt is None else (repr(fmt[1]) if fmt[0] == "string" else self.ex(fmt[1])[0])
        die = not any(k in c for k in ("iostat", "end", "err"))
        self.line(f"{rd} = _freadstart({self._unit_text(io, 5)}, {text}, {die})")
        for item in io["items"]:
            self._read_item(rd, self.parse(item))
        self._iostat_line(io, f"{rd}.finish()")

    def do_formatted(self, fmt, items, raw):
        how, spec = fmt
        self.helpers.add("_fwrite")
        self.imports.update(("math", "re"))
        text = repr(spec) if how == "string" else self.ex(spec)[0]
        parts = []
        for i in items:
            if isinstance(i, ImpliedDo):
                parts.append(self._ctor_part(i, self.type_of(i), self.bits(i)))
            elif self.is_arrayish(i) or self._array_valued(i):
                parts.append(f'*np.ravel({self.ex(i)[0]}, order="F")')
            else:
                parts.append(self.ex(i)[0])
        self.line(f'print(_fwrite({text}, [{", ".join(parts)}]), end="")')

    def _array_valued(self, e) -> bool:
        """An expression made of whole arrays (a * 2, matmul(a, v), ...), which Fortran prints elementwise."""
        if isinstance(e, Binary):
            return self._array_valued(e.left) or self._array_valued(e.right) or self.is_arrayish(e.left) or self.is_arrayish(e.right)
        if isinstance(e, Unary):
            return self._array_valued(e.operand)
        if isinstance(e, ArrayCtor):
            return True
        if isinstance(e, Ref) and e.name.upper() in ("MATMUL", "TRANSPOSE"):
            return True
        return False

    def do_comment(self, text):
        self.line(f"# {text}")

    def do_label(self, label):
        self.line(f"# fortran label {label}")

    def do_unsupported(self, why, raw):
        self.line(f"raise NotImplementedError({('fortranBridge: ' + why + ': ' + raw.strip())!r})")


def _type_extent(text: str, units) -> int:
    for unit in units:
        value = const_int(text, unit)
        if value is not None:
            return value
    raise FortranParseError(f"the bound {text} of a derived-type component is not a constant")


def _python_type_definition(tdef, units, by_name) -> List[str]:
    """A dataclass for a derived type; value semantics come from copying (see _dassign)."""
    em = PyUnitEmitter(units[0], by_name, {}, {}, set())
    lines = ["@dataclass", f"class {tdef.name}_t:"]
    if not tdef.components:
        lines.append("    pass")
    for comp in tdef.components:
        dtype = _NP_REAL32 if comp.type == REAL and comp.bits == 32 else _NP_CPLX64 if comp.type == COMPLEX and comp.bits == 32 \
            else _NPTYPE.get(comp.type, "object")
        shape = [max(_type_extent(ub, units) - _type_extent(lb, units) + 1, 0) for lb, ub in comp.dims]
        shape_txt = f"({shape[0]},)" if len(shape) == 1 else f"({', '.join(map(str, shape))})"
        if comp.type == DERIVED:
            if comp.dims:
                lines.append(f"    {comp.name}: list = field(default_factory=lambda: [{comp.derived}_t() for _ in range({shape[0]})])")
            else:
                lines.append(f"    {comp.name}: {comp.derived}_t = field(default_factory={comp.derived}_t)")
        elif comp.dims:
            lines.append(f'    {comp.name}: np.ndarray = field(default_factory=lambda: np.zeros({shape_txt}, dtype={dtype}, order="F"))')
        else:
            if comp.init is not None:
                e = parse_expression(comp.init)
                default = em.convert(em.ex(e)[0], e, comp.type, comp.bits)
            else:
                default = {INT: "0", REAL: f"{_NP_REAL32}(0.0)" if comp.bits == 32 else "0.0", LOGICAL: "False",
                           CHAR: "''", COMPLEX: f"{_NP_CPLX64}(0)" if comp.bits == 32 else "0j"}[comp.type]
            ann = _NP_REAL32 if comp.type == REAL and comp.bits == 32 else _NP_CPLX64 if comp.type == COMPLEX and comp.bits == 32 \
                else _PYTYPE[comp.type]
            lines.append(f"    {comp.name}: {ann} = {default}")
    return lines


def translate_fortran_to_python(source_text: str) -> str:
    r"""Translate entire Fortran source into modern Python / NumPy."""
    stmts, units = lex_and_split(source_text)
    blocks = analyze_common_blocks(stmts, units)
    by_name = {u.name: u for u in units}
    func_types = function_result_types(units)

    imports: Set[str] = {"numpy as np", "math"}
    body: List[str] = []
    helper_needed = False
    helpers: Set[str] = set()
    statics: List[str] = []
    for u in sorted(units, key=lambda unit: unit.kind != "MODULE"):         # module definitions come first
        em = PyUnitEmitter(u, by_name, func_types, blocks, imports)
        if u.kind != "MODULE":
            body.extend(em.run())
        statics.extend(em.static_definitions() if not u.static_problems else [])
        helper_needed = helper_needed or em.uses_untranslated_helper
        helpers |= em.helpers
    for name in list(helpers):
        helpers.update(_HELPER_DEPENDS.get(name, ()))

    out: List[str] = [
        "# ============================================================================",
        "# Translated from FORTRAN to Modern Python / NumPy by fortranBridge",
        "# Scaffold: review every `raise NotImplementedError` (constructs that were not",
        "# translated) and verify numerically against the Fortran reference before use.",
        "# ============================================================================",
        "from __future__ import annotations",
    ]
    types = units[0].types if units else {}
    if blocks or types:
        out.append("from dataclasses import dataclass, field")
    for imp in sorted(imports):
        out.append(f"import {imp}")
    out.append("")
    out.append("")
    if helper_needed:
        out += ["def _fortran_untranslated(text):", "    raise NotImplementedError(f'fortranBridge: untranslated condition: {text}')", "", ""]
    for name in sorted(helpers):
        out += _HELPERS[name] + ["", ""]
    if statics:
        out += ["# Static storage (DATA, SAVE, initialised declarations): kept between calls", *statics, "", ""]
    for tdef in types.values():
        out += _python_type_definition(tdef, units, by_name) + ["", ""]
    for blk in blocks.values():
        out.append(blk.emit_python_dataclass())
        out.append("")
        out.append("")
    out.extend(body)
    return "\n".join(out).rstrip() + "\n"
