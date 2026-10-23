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
from fortran_semantics import (
    Expr, FortranParseError, Unit, Symbol, infer_type, io_items, parse_expression,
)


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
        else:
            self.do_unsupported("formatted/file I/O is not translated (see the decompile-format skill)", text)

    def on_NONEXEC(self, d, raw):
        text = d["text"]
        if re.match(r"^DATA\b", text, re.IGNORECASE):
            self.do_unsupported("DATA initialisation is not translated", text)
        else:
            self.do_comment(f"fortran: {text}")

    def on_IMPLICIT(self, d, raw):
        pass

    def on_COMMON(self, d, raw):
        pass

    def on_EQUIVALENCE(self, d, raw):
        self.do_unsupported("EQUIVALENCE storage association is not translated", raw)

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
