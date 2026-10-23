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
from typing import Dict, List, Optional, Set, Tuple

from common_block_analyzer import analyze_common_blocks
from fortran_emit_base import UnitEmitter
from fortran_intrinsics import FORTRAN_INTRINSICS, translate_intrinsic
from fortran_semantics import (
    CHAR, COMPLEX, INT, LOGICAL, REAL,
    Binary, FortranParseError, Logical, Name, Num, Ref, Slice, Str, Unary, Unit,
    add_const, base_type, function_result_types, lex_and_split, split_top_level,
    _split_decl_item,
)

_CLASH = {"np", "math", "cmath", "max", "min", "sum", "abs", "int", "float", "round", "range", "len",
          "print", "bool", "str", "complex", "list", "dict", "set", "tuple", "type", "id", "all", "any"}
_PYTYPE = {INT: "int", REAL: "float", LOGICAL: "bool", CHAR: "str", COMPLEX: "complex"}
_NPTYPE = {INT: "np.int64", REAL: "np.float64", LOGICAL: "np.bool_", COMPLEX: "np.complex128", CHAR: "object"}

_P_OR, _P_AND, _P_NOT, _P_CMP, _P_ADD, _P_MUL, _P_UNARY, _P_POW, _P_ATOM = 1, 2, 3, 4, 5, 6, 7, 8, 10
_BIN_PREC = {"or": _P_OR, "eqv": _P_CMP, "neqv": _P_CMP, "and": _P_AND, "==": _P_CMP, "!=": _P_CMP, "<": _P_CMP,
             "<=": _P_CMP, ">": _P_CMP, ">=": _P_CMP, "+": _P_ADD, "-": _P_ADD, "*": _P_MUL, "/": _P_MUL, "**": _P_POW}
_PY_OP = {"eqv": "==", "neqv": "!="}


def pyname(name: str) -> str:
    name = name.lower()
    return name + "_" if (keyword.iskeyword(name) or name in _CLASH) else name


def _wrap(text: str, prec: int, minimum: int) -> str:
    return f"({text})" if prec < minimum else text


def blk_var(block: str) -> str:
    return f"{block.lower()}_common"


class PyUnitEmitter(UnitEmitter):
    def __init__(self, unit, units, func_types, blocks, imports: Set[str]):
        super().__init__(unit, units, func_types)
        self.blocks_info = blocks
        self.imports = imports
        self.allocated: Set[str] = set()
        self.uses_untranslated_helper = False

    # ------------------------------------------------------------------ expressions
    def ex(self, e) -> Tuple[str, int]:
        if isinstance(e, Num):
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
            text, prec = self.ex(e.operand)
            if e.op == "not":
                return "not " + _wrap(text, prec, _P_NOT), _P_NOT
            text = _wrap(text, prec, _P_MUL)
            return e.op + text, (_P_UNARY if prec >= _P_UNARY else _P_MUL)
        if isinstance(e, Binary):
            return self.binary(e)
        raise FortranParseError(f"cannot translate {e!r}")

    def binary(self, e: Binary) -> Tuple[str, int]:
        op = e.op
        prec = _BIN_PREC[op]
        lt, lp = self.ex(e.left)
        rt, rp = self.ex(e.right)
        if op == "/":
            if self.type_of(e.left) == INT and self.type_of(e.right) == INT:
                return f"int({_wrap(lt, lp, _P_MUL)} / {_wrap(rt, rp, _P_MUL + 1)})", _P_ATOM
            return f"{_wrap(lt, lp, _P_MUL)} / {_wrap(rt, rp, _P_MUL + 1)}", _P_MUL
        if op == "**":
            return f"{_wrap(lt, lp, _P_POW + 1)} ** {_wrap(rt, rp, _P_UNARY)}", _P_POW
        if prec == _P_CMP:
            return f"{_wrap(lt, lp, _P_CMP + 1)} {_PY_OP.get(op, op)} {_wrap(rt, rp, _P_CMP + 1)}", _P_CMP
        if op in ("and", "or"):
            return f"{_wrap(lt, lp, prec)} {op} {_wrap(rt, rp, prec + 1)}", prec
        # + - * : left associative, keep the Fortran evaluation order exactly
        return f"{_wrap(lt, lp, prec)} {op} {_wrap(rt, rp, prec + 1)}", prec

    def name_ref(self, name: str) -> str:
        sym = self.unit.symbols.get(name)
        if sym is not None and sym.common_block:
            return f"{blk_var(sym.common_block)}.{name}"
        if self.unit.kind == "FUNCTION" and name == self.unit.name:
            return pyname(name) + "_result"
        return pyname(name)

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
        key = e.name.upper()
        if key in FORTRAN_INTRINSICS and not (user and user.kind == "FUNCTION"):
            return self.intrinsic(key, e), _P_ATOM
        args = ", ".join(self.ex(a)[0] for a in e.args)
        extra = [blk_var(b) for b in sorted(user.commons)] if user else []
        return f"{pyname(e.name)}({', '.join([x for x in [args] if x] + extra)})", _P_ATOM

    def intrinsic(self, key: str, e: Ref) -> str:
        vec = any(self.is_arrayish(a) for a in e.args)
        pieces = [self.ex(a) for a in e.args]
        types = [self.type_of(a) for a in e.args]
        strs = [t for t, _ in pieces]
        if key == "MOD" and len(strs) == 2 and all(t == INT for t in types):
            self.imports.add("math")
            return f"int(math.fmod({strs[0]}, {strs[1]}))"
        if key in ("NINT", "IDNINT", "ANINT", "DNINT") and len(strs) == 1 and not vec:
            self.imports.add("math")
            body = f"math.copysign(math.floor(abs({strs[0]}) + 0.5), {strs[0]})"
            return f"int({body})" if key in ("NINT", "IDNINT") else body
        tmpl = FORTRAN_INTRINSICS[key]["python_vector" if vec else "python_scalar"]
        if re.search(r"\{\d\}\s*[^\s,)}]", tmpl):
            strs = [_wrap(t, p, _P_ATOM) for t, p in pieces]
        code, module = translate_intrinsic(key, strs, target="python", vectorized=vec)
        if module:
            self.imports.add(module)
        for lib in ("cmath", "math", "np"):
            if f"{lib}." in code:
                self.imports.add({"np": "numpy as np"}.get(lib, lib))
        return code

    # ------------------------------------------------------------------ l-values and conversion
    def lvalue(self, lhs) -> str:
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
        sym = self.unit.symbols.get(getattr(e, "name", ""))
        if sym is None or sym.const_value is not None:
            return None
        if isinstance(e, Name) and not sym.rank:
            return self.lvalue(e)
        if isinstance(e, Ref) and sym.rank and not any(isinstance(a, Slice) for a in e.args):
            return self.lvalue(e)
        return None

    def convert(self, text: str, expr, target_type: str) -> str:
        have = self.type_of(expr)
        if target_type == INT and have == REAL:
            return f"int({text})"
        if target_type == REAL and have == INT:
            return text + ".0" if isinstance(expr, Num) else f"float({text})"
        return text

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
            args.append(f"{pyname(p)}: {'np.ndarray' if sym.rank else _PYTYPE[sym.type]}")
        for blk in sorted(u.commons):
            args.append(f"{blk_var(blk)}: {self.blocks_info[blk].struct_name}")
        out_types = ([_PYTYPE[u.result_type]] if u.kind == "FUNCTION" else []) + \
                    [_PYTYPE[u.symbols[p].type] for p in u.modified_scalars]
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
        zero = {INT: "0", REAL: "0.0", LOGICAL: "False", CHAR: "''", COMPLEX: "0j"}
        for p in u.out_only:
            self.line(f"{pyname(p)} = {zero[u.symbols[p].type]}")

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
        if not sym or not sym.rank or sym.is_dummy or sym.common_block or name in self.allocated:
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
        self.line(f"{pyname(name)} = np.zeros({shape}, dtype={_NPTYPE[sym.type]}{order})")

    def on_TYPE_DECL(self, d, raw):
        for item in split_top_level(d["declarations"]):
            try:
                name, _dim, init = _split_decl_item(item)
            except FortranParseError:
                continue
            sym = self.unit.symbols[name]
            if sym.rank:
                self.alloc(name)
            elif init is not None and not sym.is_dummy and not sym.common_block:
                e = self.parse(init)
                self.line(f"{pyname(name)} = {self.convert(self.ex(e)[0], e, sym.type)}")

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
            self.line(f"{pyname(k)} = {self.convert(self.ex(e)[0], e, sym.type)}")

    # ------------------------------------------------------------------ control flow hooks
    def _as_int(self, e) -> str:
        text = self.ex(e)[0]
        return text if self.type_of(e) == INT else f"int({text})"

    def _range_end(self, end, inc: int) -> str:
        """Exclusive end for range(): Fortran's inclusive bound moved one past in the loop direction."""
        if self.type_of(end) == INT:
            return self.ex(add_const(end, inc))[0]
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
                return
            inc = 1 if int(step_lit) > 0 else -1
            tail = "" if step_lit == "1" else f", {step_lit}"
            self.line(f"for {v} in range({s_txt}, {self._range_end(end, inc)}{tail}):")
        else:
            st = self._as_int(step)
            self.line(f"for {v} in range({s_txt}, {self._as_int(end)} + (1 if {st} > 0 else -1), {st}):")

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

    def finish_branch(self, empty):
        self._pass_if_empty(empty)

    # ------------------------------------------------------------------ simple statement hooks
    def do_assign(self, lhs, rhs, raw):
        target = self.lvalue(lhs)
        text = self.ex(rhs)[0]
        lhs_type = self.type_of(lhs if isinstance(lhs, Name) else Name(lhs.name))
        self.line(f"{target} = {self.convert(text, rhs, lhs_type)}")

    def do_call(self, callee, args, raw):
        cu = self.units.get(callee)
        for a in args:
            if isinstance(a, Slice):
                raise FortranParseError("array section as CALL argument")
        arg_strs = [self.ex(a)[0] for pos, a in enumerate(args)
                    if cu is None or pos >= len(cu.params) or cu.params[pos] not in cu.out_only]
        if cu is None:
            self.line(f"{pyname(callee)}({', '.join(arg_strs)})")
            return
        arg_strs += [blk_var(b) for b in sorted(cu.commons)]
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
        self.line(f"print({', '.join(self.ex(i)[0] for i in items)})")

    def do_comment(self, text):
        self.line(f"# {text}")

    def do_label(self, label):
        self.line(f"# fortran label {label}")

    def do_unsupported(self, why, raw):
        self.line(f"raise NotImplementedError({('fortranBridge: ' + why + ': ' + raw.strip())!r})")


def translate_fortran_to_python(source_text: str) -> str:
    r"""Translate entire Fortran source into modern Python / NumPy."""
    stmts, units = lex_and_split(source_text)
    blocks = analyze_common_blocks(stmts)
    by_name = {u.name: u for u in units}
    func_types = function_result_types(units)

    imports: Set[str] = {"numpy as np", "math"}
    body: List[str] = []
    helper_needed = False
    for u in units:
        em = PyUnitEmitter(u, by_name, func_types, blocks, imports)
        body.extend(em.run())
        helper_needed = helper_needed or em.uses_untranslated_helper

    out: List[str] = [
        "# ============================================================================",
        "# Translated from FORTRAN to Modern Python / NumPy by fortranBridge",
        "# Scaffold: review every `raise NotImplementedError` (constructs that were not",
        "# translated) and verify numerically against the Fortran reference before use.",
        "# ============================================================================",
        "from __future__ import annotations",
    ]
    if blocks:
        out.append("from dataclasses import dataclass, field")
    for imp in sorted(imports):
        out.append(f"import {imp}")
    out.append("")
    out.append("")
    if helper_needed:
        out += ["def _fortran_untranslated(text):", "    raise NotImplementedError(f'fortranBridge: untranslated condition: {text}')", "", ""]
    for blk in blocks.values():
        out.append(blk.emit_python_dataclass())
        out.append("")
        out.append("")
    out.extend(body)
    return "\n".join(out).rstrip() + "\n"
