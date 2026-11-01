#!/usr/bin/env python3
r"""
fortran_emit_base.py - Language-independent statement walker shared by the Python and C++ emitters.

The walker owns everything that is the same in every target language: block structure (DO / IF /
labelled DO termination), one-line IF recursion, GO TO bookkeeping, and the policy for statements
that cannot be translated. Subclasses supply the syntax through the `do_*` / `open_*` hooks.

Policy: a construct that is not translated is NEVER dropped silently. The emitter writes an explicit
failure (`raise NotImplementedError` in Python, `throw std::logic_error` in C++) at that point, so
the generated code fails loudly instead of computing a wrong answer.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from fortran_lexer import FortranStatement, classify_statement
from fortran_intrinsics import FORTRAN_INTRINSICS
from fortran_semantics import (
    ArrayCtor, Binary, ComplexLit, Expr, FortranParseError, ImpliedDo, Logical, Name, Ref, Slice, Unary, Unit, Symbol,
    classify_format, format_io, infer_type, io_items, io_statement, parse_expression,
)

# intrinsics that reduce an array to a scalar (their array arguments stay whole in an element-wise expression)
ARRAY_REDUCERS = {"SUM", "PRODUCT", "MAXVAL", "MINVAL", "DOT_PRODUCT", "SIZE", "MAXLOC", "MINLOC", "LBOUND", "UBOUND",
                  "ALL", "ANY", "COUNT", "SHAPE", "NORM2", "ALLOCATED", "ASSOCIATED"}
# intrinsics whose result is an array that cannot be rewritten element by element
ARRAY_RESULT = {"MATMUL": lambda r: r[0] + r[1] - 2, "TRANSPOSE": lambda r: 2}


class UnitEmitter:
    INDENT = "    "

    def __init__(self, unit: Unit, units: Dict[str, Unit], func_types: Dict[str, str]):
        self.unit = unit
        self.units = units
        self.func_types = func_types
        self.lines: List[str] = []
        self.level = 0
        self.blocks: List[Tuple[str, Optional[str]]] = []     # ('do'|'while'|'if', label)
        self.block_marks: List[int] = []                      # len(self.lines) when a block body opened
        self.goto_targets: Set[str] = self._collect_goto_targets()
        self.selects: List[dict] = []                          # open SELECT CASE constructs
        self.formats: Dict[str, str] = {}                      # statement label -> text of a FORMAT statement
        for st in unit.stmts:
            if st.label and st.stmt_type == "IO" and re.match(r"^FORMAT\b", st.details["statement"], re.IGNORECASE):
                self.formats[st.label] = st.details["statement"][6:].strip()
        self.goto_targets |= self._io_jump_targets()

    # ------------------------------------------------------------------ plumbing
    def line(self, text: str = "") -> None:
        self.lines.append((self.INDENT * self.level + text) if text else "")

    def parse(self, text: str) -> Expr:
        return parse_expression(text)

    def type_of(self, expr: Expr) -> str:
        return infer_type(expr, self.unit, self.func_types)

    def _collect_goto_targets(self) -> Set[str]:
        targets: Set[str] = set()
        for s in self.unit.stmts:
            d = s.details
            if s.stmt_type == "GOTO":
                targets.add(d["target_label"])
            elif s.stmt_type == "COMPUTED_GOTO":
                targets.update(d["labels"])
            elif s.stmt_type == "ARITHMETIC_IF":
                targets.update((d["label_neg"], d["label_zero"], d["label_pos"]))
            elif s.stmt_type == "IF_STMT":
                m = re.match(r"^GO\s*TO\s*(\d+)$", d["statement"].strip(), re.IGNORECASE)
                if m:
                    targets.add(m.group(1))
        return targets

    def _io_jump_targets(self) -> Set[str]:
        """Labels named by END= and ERR= specifiers: those statements jump."""
        targets: Set[str] = set()
        for st in self.unit.stmts:
            if st.stmt_type == "IO":
                io = io_statement(st.details["statement"])
                if io:
                    targets.update(v for k, v in io["controls"].items() if k in ("end", "err") and v.isdigit())
        return targets

    # ------------------------------------------------------------------ driver
    def run(self) -> List[str]:
        stmts = self.unit.stmts
        header = stmts[0] if stmts and stmts[0].stmt_type in ("SUBROUTINE", "FUNCTION", "PROGRAM") else None
        self.begin_unit()
        for s in stmts[1:] if header is not None else stmts:
            self.statement(s)
        self.end_unit()
        return self.lines

    def statement(self, s: FortranStatement) -> None:
        if s.label and s.label in self.goto_targets:
            self.do_label(s.label)
        try:
            self.dispatch(s.stmt_type, s.details, s.raw)
        except FortranParseError as exc:
            self.do_unsupported(f"could not parse expression ({exc})", s.raw)
        # a labelled statement terminates every DO that is waiting for that label
        if s.label:
            while self.blocks and self.blocks[-1][0] in ("do", "while") and self.blocks[-1][1] == s.label:
                self.close_block()

    def dispatch(self, st: str, d: dict, raw: str) -> None:
        handler = getattr(self, f"on_{st}", None)
        if handler is None:
            self.do_unsupported(f"unsupported statement type {st}", raw)
            return
        handler(d, raw)

    # ------------------------------------------------------------------ blocks
    def push_block(self, kind: str, label: Optional[str] = None) -> None:
        self.blocks.append((kind, label))
        self.block_marks.append(len(self.lines))
        self.level += 1

    def close_block(self) -> None:
        if not self.blocks:
            return
        kind, _ = self.blocks.pop()
        mark = self.block_marks.pop()
        self.level -= 1
        self.finish_block(kind, empty=(len(self.lines) == mark))

    def switch_block(self) -> None:
        r"""Leave the current IF branch before an ELSE IF / ELSE."""
        mark = self.block_marks.pop()
        self.level -= 1
        self.finish_branch(empty=(len(self.lines) == mark))
        self.block_marks.append(len(self.lines) + 1)

    # ------------------------------------------------------------------ statement handlers
    def on_DO(self, d, raw):
        var = self.parse(d["var"])
        self.open_do(var, self.parse(d["start"]), self.parse(d["end"]), self.parse(d["step"]), raw)
        self.push_block("do", d.get("loop_label"))

    def on_DO_WHILE(self, d, raw):
        self.open_while(self._cond(d["condition"]), raw)
        self.push_block("while", d.get("loop_label"))

    def on_DO_FOREVER(self, d, raw):
        self.open_while(None, raw)
        self.push_block("while", None)

    def on_END_DO(self, d, raw):
        # the loop that is open and not waiting for a numeric label
        for k in range(len(self.blocks) - 1, -1, -1):
            if self.blocks[k][0] in ("do", "while"):
                while len(self.blocks) > k + 1:
                    self.close_block()
                self.close_block()
                return

    def on_CONTINUE(self, d, raw):
        pass   # loop terminator handled by label; a stray CONTINUE is a no-op

    def on_IF_THEN(self, d, raw):
        self.open_if(self._cond(d["condition"]), raw)
        self.push_block("if")

    def on_ELSE_IF(self, d, raw):
        self.switch_block()
        self.open_elif(self._cond(d["condition"]), raw)
        self.level += 1

    def on_ELSE(self, d, raw):
        self.switch_block()
        self.open_else()
        self.level += 1

    def on_END_IF(self, d, raw):
        while self.blocks and self.blocks[-1][0] != "if":
            self.close_block()
        self.close_block()

    def on_IF_STMT(self, d, raw):
        cond = self._cond(d["condition"])
        kind, det = classify_statement(d["statement"])
        self.open_if(cond, raw)
        self.push_block("if")
        try:
            self.dispatch(kind, det, d["statement"])
        except FortranParseError as exc:
            self.do_unsupported(f"could not parse expression ({exc})", d["statement"])
        self.close_block()

    # SELECT CASE is an if / else-if chain on the selector expression (EXIT and CYCLE still act on the enclosing DO).
    def on_SELECT_CASE(self, d, raw):
        self.selects.append({"expr": self.parse(d["expr"]), "cases": 0})

    @staticmethod
    def _range_colon(item: str) -> int:
        """Index of the top-level ':' of a CASE range such as 6:9, :-1 or 10: ; -1 for a single value."""
        depth, quote = 0, None
        for k, ch in enumerate(item):
            if quote:
                quote = None if ch == quote else quote
            elif ch in "'\"":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == ":" and depth == 0:
                return k
        return -1

    def _case_condition(self, sel: Expr, selectors: List[str]) -> Optional[Expr]:
        cond: Optional[Expr] = None
        for item in selectors:
            colon = self._range_colon(item)
            if colon < 0:
                test = Binary("==", sel, self.parse(item))
            else:
                lo, hi = item[:colon].strip(), item[colon + 1:].strip()
                bounds = ([Binary(">=", sel, self.parse(lo))] if lo else []) + \
                         ([Binary("<=", sel, self.parse(hi))] if hi else [])
                if not bounds:
                    raise FortranParseError("empty CASE range")
                test = bounds[0] if len(bounds) == 1 else Binary("and", bounds[0], bounds[1])
            cond = test if cond is None else Binary("or", cond, test)
        return cond

    def on_CASE(self, d, raw):
        if not self.selects:
            raise FortranParseError("CASE outside SELECT CASE")
        sel = self.selects[-1]
        default = d["default"]
        try:
            cond = None if default else self._case_condition(sel["expr"], d["selectors"])
        except FortranParseError:
            cond = None                       # an unparsable selector becomes an untranslated condition (fails loudly)
        if sel["cases"] == 0:
            self.open_if(Logical(True) if default else cond, raw)
            self.push_block("if")
        else:
            self.switch_block()
            if default:
                self.open_else()
            else:
                self.open_elif(cond, raw)
            self.level += 1
        sel["cases"] += 1

    def on_END_SELECT(self, d, raw):
        sel = self.selects.pop() if self.selects else {"cases": 0}
        if sel["cases"]:
            while self.blocks and self.blocks[-1][0] != "if":
                self.close_block()
            self.close_block()

    def on_ALLOCATE(self, d, raw):
        self.do_allocate([self.parse(o) for o in d["objects"]], d["options"], raw)

    def on_DEALLOCATE(self, d, raw):
        self.do_deallocate([self.parse(o) for o in d["objects"]], d["options"], raw)

    def on_NULLIFY(self, d, raw):
        self.do_deallocate([self.parse(o) for o in d["objects"]], [], raw, nullify=True)

    def on_ATTRIBUTE(self, d, raw):
        pass                                   # ALLOCATABLE / POINTER / TARGET are applied to the symbol table

    def on_POINTER_ASSIGN(self, d, raw):
        self.do_pointer_assign(self.parse(d["lhs"]), self.parse(d["rhs"]), raw)

    def on_ASSIGNMENT(self, d, raw):
        self.do_assign(self.parse(d["lhs"]), self.parse(d["rhs"]), raw)

    def on_CALL(self, d, raw):
        self.do_call(d["subroutine"].lower(), [self.parse(a) for a in d["args"]], raw)

    def on_RETURN(self, d, raw):
        self.do_return()

    def on_STOP(self, d, raw):
        self.do_stop(re.sub(r"^STOP\s*", "", d["text"], flags=re.IGNORECASE).strip(), raw)

    def on_EXIT(self, d, raw):
        self.do_exit()

    def on_CYCLE(self, d, raw):
        self.do_cycle()

    def on_GOTO(self, d, raw):
        self.do_goto([d["target_label"]], None, raw)

    def on_COMPUTED_GOTO(self, d, raw):
        self.do_goto(list(d["labels"]), self.parse(d["variable"]), raw)

    def on_ARITHMETIC_IF(self, d, raw):
        self.do_arith_if(self.parse(d["condition"]), d["label_neg"], d["label_zero"], d["label_pos"], raw)

    def on_IO(self, d, raw):
        text = d["statement"]
        if re.match(r"^FORMAT\b", text, re.IGNORECASE):
            self.do_comment(f"fortran: {text}")
            return
        kind, items = io_items(text)
        if kind == "print":
            self.do_print([self.parse(i) for i in items], raw)
            return
        formatted = format_io(text)
        if formatted is None:
            io = io_statement(text)
            if io is None or io["kind"] in ("BACKSPACE", "ENDFILE"):
                self.do_unsupported("this I/O statement is not translated (BACKSPACE, ENDFILE, direct or unformatted access)", text)
                return
            if io["kind"] == "FLUSH":
                return
            fmt = classify_format(io["fmt"]) if io["fmt"] is not None else None
            if fmt is not None and fmt[0] == "label":
                if fmt[1] not in self.formats:
                    self.do_unsupported(f"FORMAT {fmt[1]} not found", text)
                    return
                fmt = ("string", self.formats[fmt[1]])
            if fmt is not None and fmt[0] == "expr":
                fmt = ("expr", self.parse(fmt[1]))
            self.do_io(io, fmt, text)
            return
        (how, spec), out_items = formatted
        if how == "label":
            if spec not in self.formats:
                self.do_unsupported(f"FORMAT {spec} not found", text)
                return
            how, spec = "string", self.formats[spec]
        self.do_formatted((how, spec if how == "string" else self.parse(spec)), [self.parse(i) for i in out_items], raw)

    def on_NONEXEC(self, d, raw):
        text = d["text"]
        # DATA and SAVE are applied to the symbol table (static storage, initial values); problems are reported per unit
        self.do_comment(f"fortran: {text}")

    def on_IMPLICIT(self, d, raw):
        pass

    # MODULE / USE / CONTAINS are resolved in the semantic layer (module symbols are imported into each unit)
    def on_MODULE(self, d, raw):
        pass

    def on_END_MODULE(self, d, raw):
        pass

    def on_USE(self, d, raw):
        self.do_comment(f"fortran: {raw}")

    def on_CONTAINS(self, d, raw):
        pass

    def on_COMMON(self, d, raw):
        pass

    def on_EQUIVALENCE(self, d, raw):
        self.do_comment(f"fortran: {raw}")             # storage association is set up where the unit begins

    def on_END(self, d, raw):
        pass

    def on_OTHER(self, d, raw):
        self.do_unsupported("unrecognised statement", d.get("text", raw))

    def on_PARAMETER(self, d, raw):
        pass

    def on_DIMENSION(self, d, raw):
        pass

    def on_TYPE_DECL(self, d, raw):
        pass

    def _cond(self, text: str) -> Optional[Expr]:
        try:
            return self.parse(text)
        except FortranParseError:
            return None

    def array_valued(self, e: Expr) -> bool:
        """Does `e` denote an array (a whole array, a section, a constructor, MATMUL, an element-wise expression of those)?"""
        if isinstance(e, ArrayCtor):
            return True
        if isinstance(e, Name):
            sym = self.unit.symbols.get(e.name)
            return bool(sym is not None and sym.rank)
        if isinstance(e, Ref):
            sym = self.unit.symbols.get(e.name)
            if sym is not None and sym.rank:
                return any(isinstance(a, Slice) for a in e.args)
            key = e.name.upper()
            if e.name in self.units and self.units[e.name].kind == "FUNCTION":
                return False
            if key in ARRAY_REDUCERS:
                return False
            if key in ARRAY_RESULT:
                return True
            return key in FORTRAN_INTRINSICS and any(self.array_valued(a) for a in e.args)
        if isinstance(e, Unary):
            return self.array_valued(e.operand)
        if isinstance(e, Binary):
            return self.array_valued(e.left) or self.array_valued(e.right)
        return False

    # ------------------------------------------------------------------ hooks (language specific)
    def begin_unit(self): raise NotImplementedError
    def end_unit(self): raise NotImplementedError
    def open_do(self, var, start, end, step, raw): raise NotImplementedError
    def open_while(self, cond, raw): raise NotImplementedError
    def open_if(self, cond, raw): raise NotImplementedError
    def open_elif(self, cond, raw): raise NotImplementedError
    def open_else(self): raise NotImplementedError
    def finish_block(self, kind, empty): raise NotImplementedError
    def finish_branch(self, empty): raise NotImplementedError
    def do_assign(self, lhs, rhs, raw): raise NotImplementedError
    def do_formatted(self, fmt, items, raw): raise NotImplementedError
    def do_io(self, io, fmt, raw): raise NotImplementedError
    def do_allocate(self, objects, options, raw): raise NotImplementedError
    def do_deallocate(self, objects, options, raw, nullify=False): raise NotImplementedError
    def do_pointer_assign(self, lhs, rhs, raw): raise NotImplementedError
    def do_call(self, callee, args, raw): raise NotImplementedError
    def do_return(self): raise NotImplementedError
    def do_stop(self, text, raw): raise NotImplementedError
    def do_exit(self): raise NotImplementedError
    def do_cycle(self): raise NotImplementedError
    def do_goto(self, labels, selector, raw): raise NotImplementedError
    def do_arith_if(self, expr, neg, zero, pos, raw): raise NotImplementedError
    def do_print(self, items, raw): raise NotImplementedError
    def do_comment(self, text): raise NotImplementedError
    def do_label(self, label): raise NotImplementedError
    def do_unsupported(self, why, raw): raise NotImplementedError
