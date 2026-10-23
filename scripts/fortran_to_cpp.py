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
from typing import Dict, List, Optional, Set, Tuple

from common_block_analyzer import analyze_common_blocks
from fortran_emit_base import UnitEmitter
from fortran_intrinsics import FORTRAN_INTRINSICS, translate_intrinsic
from fortran_semantics import (
    CHAR, COMPLEX, INT, LOGICAL, REAL,
    Binary, FortranParseError, Logical, Name, Num, Ref, Slice, Str, Unary,
    add_const, function_result_types, lex_and_split,
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

_P_OR, _P_AND, _P_EQ, _P_REL, _P_ADD, _P_MUL, _P_UNARY, _P_ATOM = 1, 2, 4, 5, 6, 7, 8, 10
_BIN_PREC = {"or": _P_OR, "and": _P_AND, "eqv": _P_EQ, "neqv": _P_EQ, "==": _P_EQ, "!=": _P_EQ,
             "<": _P_REL, "<=": _P_REL, ">": _P_REL, ">=": _P_REL, "+": _P_ADD, "-": _P_ADD, "*": _P_MUL, "/": _P_MUL}
_CPP_OP = {"and": "&&", "or": "||", "eqv": "==", "neqv": "!="}


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
        self.tmp_counter = 0

    # ------------------------------------------------------------------ expressions
    def ex(self, e) -> Tuple[str, int]:
        if isinstance(e, Num):
            return e.text, _P_ATOM
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
        raise FortranParseError(f"cannot translate {e!r}")

    def _simple(self, e) -> bool:
        return isinstance(e, (Name, Num)) or (isinstance(e, Ref) and self.unit.symbols.get(e.name) is not None
                                              and self.unit.symbols[e.name].rank > 0)

    def binary(self, e: Binary) -> Tuple[str, int]:
        op = e.op
        if op == "**":
            self.headers.add("<cmath>")
            lt, _ = self.ex(e.left)
            exponent = e.right
            if isinstance(exponent, Num) and exponent.is_int and exponent.text in ("2", "3") and self._simple(e.left):
                n = int(exponent.text)
                return "(" + " * ".join([lt] * n) + ")", _P_ATOM
            rt, _ = self.ex(exponent)
            call = f"std::pow({lt}, {rt})"
            if self.type_of(e.left) == INT and self.type_of(e.right) == INT:
                call = f"static_cast<int>({call})"
            return call, _P_ATOM
        prec = _BIN_PREC[op]
        lt, lp = self.ex(e.left)
        rt, rp = self.ex(e.right)
        sym = _CPP_OP.get(op, op)
        if prec in (_P_EQ, _P_REL):
            return f"{_wrap(lt, lp, prec + 1)} {sym} {_wrap(rt, rp, prec + 1)}", prec
        return f"{_wrap(lt, lp, prec)} {sym} {_wrap(rt, rp, prec + 1)}", prec

    def name_ref(self, name: str) -> str:
        sym = self.unit.symbols.get(name)
        if sym is not None and sym.common_block:
            return f"{blk_var(sym.common_block)}.{name}"
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
        key = e.name.upper()
        if key in FORTRAN_INTRINSICS and not (user and user.kind == "FUNCTION"):
            return self.intrinsic(key, e), _P_ATOM
        args = [self.ex(a)[0] for a in e.args]
        if user:
            args += [blk_var(b) for b in sorted(user.commons)]
        return f"{cppname(e.name)}({', '.join(args)})", _P_ATOM

    def intrinsic(self, key: str, e: Ref) -> str:
        strs = [self.ex(a)[0] for a in e.args]
        types = [self.type_of(a) for a in e.args]
        if key == "MOD" and len(strs) == 2 and all(t == INT for t in types):
            return f"({strs[0]} % {strs[1]})"
        if key in ("MAX", "MIN", "AMAX1", "AMIN1", "DMAX1", "DMIN1") and len(set(types)) > 1:
            strs = [f"static_cast<double>({s})" if t == INT else s for s, t in zip(strs, types)]
        code, header = translate_intrinsic(key, strs, target="cpp")
        if header:
            self.headers.add(header)
        return code

    # ------------------------------------------------------------------ l-values and conversion
    def lvalue(self, lhs) -> str:
        if isinstance(lhs, Name):
            return self.name_ref(lhs.name)
        if isinstance(lhs, Ref):
            sym = self.unit.symbols.get(lhs.name)
            if sym is not None and sym.rank:
                return f"{self.name_ref(lhs.name)}{self.subscripts(sym, lhs.args)}"
        raise FortranParseError("assignment target is not a variable or array element")

    def lvalue_or_none(self, e) -> Optional[str]:
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

    def convert(self, text: str, expr, target_type: str) -> str:
        have = self.type_of(expr)
        if target_type == INT and have == REAL:
            return f"static_cast<int>({text})"
        return text

    def returns_value(self) -> bool:
        return self.unit.kind == "FUNCTION"

    # ------------------------------------------------------------------ unit scaffolding
    def param_decl(self, p: str) -> str:
        sym = self.unit.symbols[p]
        elem = _CPPTYPE[sym.type]
        if sym.rank:
            const = "const " if sym.intent == "in" else ""
            return f"{const}{vec_type(elem, sym.rank)}& {cppname(p)}"
        ref = "&" if p in self.unit.modified_scalars else ""
        return f"{elem}{ref} {cppname(p)}"

    def begin_unit(self):
        u = self.unit
        args = [self.param_decl(p) for p in u.params]
        args += [f"{self.blocks_info[b].struct_name}& {blk_var(b)}" for b in sorted(u.commons)]
        if u.kind == "PROGRAM":
            self.line("int main() {")
        else:
            ret = _CPPTYPE[u.result_type] if u.kind == "FUNCTION" else "void"
            self.line(f"{ret} {cppname(u.name)}({', '.join(args)}) {{")
        self.level = 1
        self.line(f"// Translated from Fortran {u.kind} {u.name}.")
        self._declare_locals()

    def _declare_locals(self) -> None:
        u = self.unit
        consts = [(n, s) for n, s in u.symbols.items() if s.const_value is not None and not s.is_dummy]
        for name, sym in consts:
            e = self.parse(sym.const_value)
            self.line(f"constexpr {_CPPTYPE[sym.type]} {cppname(name)} = {self.convert(self.ex(e)[0], e, sym.type)};")
        for name, sym in u.symbols.items():
            if sym.is_dummy or sym.common_block or sym.const_value is not None:
                continue
            if u.kind == "FUNCTION" and name == u.name:
                continue
            if name in self.units and self.units[name].kind != "PROGRAM" and not sym.rank:
                continue   # a called function, not a variable
            elem = _CPPTYPE[sym.type]
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
        if u.kind == "FUNCTION":
            self.line(f"{_CPPTYPE[u.result_type]} {cppname(u.name)}_result{{}};")

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
    def open_do(self, var, start, end, step, raw):
        v = self.ex(var)[0]
        s_txt = self.convert(self.ex(start)[0], start, self.type_of(var))
        e_txt = self.ex(end)[0]
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
            self.line(f"for ({v} = {s_txt}; {v} {cmp_op} {_wrap(e_txt, self.ex(end)[1], _P_REL + 1)}; {inc}) {{")
        else:
            st = self.ex(step)[0]
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
    def do_assign(self, lhs, rhs, raw):
        lhs_type = self.type_of(lhs if isinstance(lhs, Name) else Name(lhs.name))
        sym = self.unit.symbols.get(getattr(lhs, "name", ""))
        if isinstance(lhs, Name) and sym is not None and sym.rank:
            if sym.rank == 1 and not (isinstance(rhs, Name) and self.unit.symbols.get(rhs.name, None) and self.unit.symbols[rhs.name].rank):
                self.headers.add("<algorithm>")
                tgt = self.name_ref(lhs.name)
                self.line(f"std::fill({tgt}.begin(), {tgt}.end(), {self.ex(rhs)[0]});")
                return
            raise FortranParseError("whole-array assignment is not translated to C++")
        text = self.convert(self.ex(rhs)[0], rhs, lhs_type)
        self.line(f"{self.lvalue(lhs)} = {text};")

    def do_call(self, callee, args, raw):
        cu = self.units.get(callee)
        pieces = []
        temps = []
        for pos, a in enumerate(args):
            if isinstance(a, Slice):
                raise FortranParseError("array section as CALL argument")
            text = self.ex(a)[0]
            if cu is not None and pos < len(cu.params):
                formal = cu.params[pos]
                fsym = cu.symbols[formal]
                if formal in cu.modified_scalars and not fsym.rank:
                    tgt = self.lvalue_or_none(a)
                    if tgt is None or self.type_of(a) != fsym.type:
                        self.tmp_counter += 1
                        tmp = f"tmp{self.tmp_counter}_"
                        temps.append(f"{_CPPTYPE[fsym.type]} {tmp} = {text};")
                        text = tmp
            pieces.append(text)
        if cu is not None:
            pieces += [blk_var(b) for b in sorted(cu.commons)]
        call = f"{cppname(callee)}({', '.join(pieces)});"
        if temps:
            self.line("{")
            self.level += 1
            for t in temps:
                self.line(t)
            self.line(call)
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
        outs = " << \" \" << ".join(self.ex(i)[0] for i in items)
        self.line(f"std::cout << {outs + ' << ' if outs else ''}std::endl;")

    def do_comment(self, text):
        self.line(f"// {text}")

    def do_label(self, label):
        self.line(f"label_{label}: ;")

    def do_unsupported(self, why, raw):
        self.headers.add("<stdexcept>")
        self.line(f"throw std::logic_error({_cstr('fortranBridge: ' + why + ': ' + raw.strip())});")


def translate_fortran_to_cpp(source_text: str) -> str:
    r"""Translate entire Fortran source into modern C++17/20."""
    stmts, units = lex_and_split(source_text)
    blocks = analyze_common_blocks(stmts)
    by_name = {u.name: u for u in units}
    func_types = function_result_types(units)

    headers: Set[str] = {"<algorithm>", "<array>", "<cmath>", "<cstdlib>", "<iostream>", "<stdexcept>", "<string>", "<vector>"}
    body: List[str] = []
    helper_needed = False
    for u in units:
        em = CppUnitEmitter(u, by_name, func_types, blocks, headers)
        body.extend(em.run())
        helper_needed = helper_needed or em.uses_untranslated_helper

    out: List[str] = [
        "// ============================================================================",
        "// Translated from FORTRAN to Modern C++17/20 by fortranBridge",
        "// Scaffold: review every `throw std::logic_error` (constructs that were not",
        "// translated) and verify numerically against the Fortran reference before use.",
        "// ============================================================================",
        "",
    ]
    for h in sorted(headers):
        out.append(f"#include {h}")
    out.append("")
    if helper_needed:
        out += ['inline bool fortran_untranslated(const char* text) { throw std::logic_error(text); }', ""]
    for blk in blocks.values():
        out.append(blk.emit_cpp_struct())
        out.append("")
    out.extend(body)
    return "\n".join(out).rstrip() + "\n"
