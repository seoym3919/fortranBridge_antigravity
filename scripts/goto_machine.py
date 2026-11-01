#!/usr/bin/env python3
r"""
goto_machine.py - GO TO, computed GO TO and arithmetic IF for the Python emitter.

Python has no goto. A unit that uses jumps is compiled to a small state machine: its executable statements are lowered
to labelled blocks (structured IF / DO / DO WHILE / SELECT CASE become conditional jumps), and the generated function
runs `while True:` over `if _pc == k:` blocks, where a jump sets `_pc` and continues. Declarations stay in the prologue.

Semantics kept from the structured translation: a DO loop's trip count is fixed on entry, the DO variable ends at
start + trips*step after normal completion and keeps its current value after EXIT, and CYCLE / EXIT are jumps to the
loop's continue / exit points.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from fortran_lexer import FortranStatement, classify_statement
from fortran_semantics import (
    INT, REAL, Binary, FortranParseError, Logical, Name, Num, Unary, io_statement,
)

_DECLARATIVE = {"TYPE_DECL", "DIMENSION", "PARAMETER", "IMPLICIT", "COMMON", "EQUIVALENCE", "NONEXEC", "MODULE", "END_MODULE",
                "USE", "CONTAINS", "SUBROUTINE", "FUNCTION", "PROGRAM", "END"}


class GotoMachineMixin:
    """Methods added to a UnitEmitter whose target language has no goto."""

    # ------------------------------------------------------------------ lowering
    def _new_label(self) -> str:
        self._machine_labels += 1
        return f"G{self._machine_labels}"

    def _lower(self, stmts: List[FortranStatement]) -> List[tuple]:
        """Instruction list: ("label", name) | ("emit", fn) | ("jump", name) | ("branch", cond_fn, true, false)
        | ("switch", selector_fn, [labels]) | ("arith", expr_fn, neg, zero, pos)."""
        out: List[tuple] = []
        ifs: List[dict] = []                    # open IF / SELECT constructs
        loops: List[dict] = []                  # open DO / DO WHILE: {"cont", "exit", "label"}
        selects: List[dict] = []

        def label(name: str) -> None:
            out.append(("label", name))

        def jump(name: str) -> None:
            out.append(("jump", name))

        def branch(cond_fn: Callable[[], str], t: str, f: str) -> None:
            out.append(("branch", cond_fn, t, f))

        def cond_of(text: str, raw: str) -> Callable[[], str]:
            return lambda: self._cond_text(self._cond(text), raw)

        def simple(kind: str, det: dict, raw: str) -> None:
            if kind == "GOTO":
                jump("L" + det["target_label"])
            elif kind == "EXIT":
                jump(loops[-1]["exit"]) if loops else out.append(("emit", lambda: self.do_unsupported("EXIT outside a loop", raw)))
            elif kind == "CYCLE":
                jump(loops[-1]["cont"]) if loops else out.append(("emit", lambda: self.do_unsupported("CYCLE outside a loop", raw)))
            elif kind == "COMPUTED_GOTO":
                out.append(("switch", lambda: self.ex(self.parse(det["variable"]))[0], ["L" + x for x in det["labels"]]))
            elif kind == "ARITHMETIC_IF":
                out.append(("arith", lambda: self.ex(self.parse(det["condition"]))[0],
                            "L" + det["label_neg"], "L" + det["label_zero"], "L" + det["label_pos"]))
            else:
                out.append(("emit", lambda kind=kind, det=det, raw=raw: self._machine_statement(kind, det, raw)))
                if kind == "IO":
                    io = io_statement(det["statement"])
                    controls = io["controls"] if io else {}
                    if controls.get("end", "").isdigit() or controls.get("err", "").isdigit():
                        # the READ left its IOSTAT in _ios: a negative value is end of file, a positive one an error
                        after = self._new_label()
                        if controls.get("end", "").isdigit():
                            mid = self._new_label()
                            branch(lambda: "_ios < 0", "L" + controls["end"], mid)
                            label(mid)
                        if controls.get("err", "").isdigit():
                            branch(lambda: "_ios > 0", "L" + controls["err"], after)
                        label(after)

        for s in stmts:
            kind, det, raw = s.stmt_type, s.details, s.raw
            if s.label:
                label("L" + s.label)
            if kind == "DO":
                self._lower_do(s, loops, out, label, jump, branch)
            elif kind in ("DO_WHILE", "DO_FOREVER"):
                top, body, after = (self._new_label() for _ in range(3))
                label(top)
                if kind == "DO_WHILE":
                    branch(cond_of(det["condition"], raw), body, after)
                label(body)
                # CYCLE re-tests the condition: it jumps to the top
                loops.append({"cont": top, "exit": after, "label": det.get("loop_label"),
                              "close": (lambda top=top, after=after: (jump(top), label(after)))})
            elif kind == "END_DO":
                if loops:
                    loops[-1]["close"]()
                    loops.pop()
            elif kind == "IF_THEN":
                then, other, end = self._new_label(), self._new_label(), self._new_label()
                branch(cond_of(det["condition"], raw), then, other)
                label(then)
                ifs.append({"else": other, "end": end})
            elif kind == "ELSE_IF":
                frame = ifs[-1]
                jump(frame["end"])
                label(frame["else"])
                then, other = self._new_label(), self._new_label()
                branch(cond_of(det["condition"], raw), then, other)
                label(then)
                frame["else"] = other
            elif kind == "ELSE":
                frame = ifs[-1]
                jump(frame["end"])
                label(frame["else"])
                frame["else"] = None
            elif kind == "END_IF":
                frame = ifs.pop()
                if frame["else"]:
                    label(frame["else"])
                label(frame["end"])
            elif kind == "IF_STMT":
                inner_kind, inner_det = classify_statement(det["statement"])
                nxt = self._new_label()
                if inner_kind == "GOTO":
                    branch(cond_of(det["condition"], raw), "L" + inner_det["target_label"], nxt)
                else:
                    body = self._new_label()
                    branch(cond_of(det["condition"], raw), body, nxt)
                    label(body)
                    simple(inner_kind, inner_det, det["statement"])
                label(nxt)
            elif kind == "SELECT_CASE":
                selects.append({"expr": self.parse(det["expr"]), "cases": 0, "else": None, "end": self._new_label()})
            elif kind == "CASE":
                sel = selects[-1]
                if sel["cases"]:
                    jump(sel["end"])
                    label(sel["else"])
                    sel["else"] = None
                if det["default"]:
                    sel["else"] = None
                else:
                    try:
                        cond = self._case_condition(sel["expr"], det["selectors"])
                    except FortranParseError:
                        cond = None
                    then, other = self._new_label(), self._new_label()
                    branch(lambda cond=cond, raw=raw: self._cond_text(cond, raw), then, other)
                    label(then)
                    sel["else"] = other
                sel["cases"] += 1
            elif kind == "END_SELECT":
                sel = selects.pop()
                if sel["else"]:
                    label(sel["else"])
                label(sel["end"])
            elif kind in ("CONTINUE", "END"):
                pass
            else:
                simple(kind, det, raw)
            # a labelled statement ends every DO that is waiting for its label
            if s.label:
                while loops and loops[-1]["label"] == s.label:
                    loops[-1]["close"]()
                    loops.pop()
        return out

    def _lower_do(self, s, loops, out, label, jump, branch) -> None:
        d = s.details
        n = self._new_label()
        tmp = f"_do{n}"
        top, body, done, after, cont = (self._new_label() for _ in range(5))
        var = self.parse(d["var"])
        start, end, step = self.parse(d["start"]), self.parse(d["end"]), self.parse(d["step"])
        v = lambda: self.ex(var)[0]                                         # noqa: E731
        if self.type_of(var) == REAL:
            width = self.target_bits(var)

            def setup():
                args = ", ".join(self.convert(self.ex(x)[0], x, REAL, width) for x in (start, end, step))
                self.helpers.add("_fdo")
                self.line(f"{tmp}_v, {tmp}_after = _fdo({args})")
                self.line(f"{tmp}_i = 0")

            test = lambda: f"{tmp}_i < len({tmp}_v)"                         # noqa: E731
            enter = lambda: self.line(f"{v()} = {tmp}_v[{tmp}_i]")           # noqa: E731
            advance = lambda: self.line(f"{tmp}_i += 1")                     # noqa: E731
            finish = lambda: self.line(f"{v()} = {tmp}_after")               # noqa: E731
        else:
            def setup():
                self.line(f"{tmp} = {self.range_text(start, end, step)}")
                self.line(f"{tmp}_i = 0")
                self.line(f"{v()} = {tmp}.start")

            test = lambda: f"{tmp}_i < len({tmp})"                           # noqa: E731
            enter = lambda: self.line(f"{v()} = {tmp}[{tmp}_i]")             # noqa: E731
            advance = lambda: self.line(f"{tmp}_i += 1")                     # noqa: E731
            finish = lambda: self.line(f"{v()} = {tmp}.start + len({tmp}) * {tmp}.step")  # noqa: E731
        out.append(("emit", setup))
        label(top)
        branch(test, body, done)
        label(body)
        out.append(("emit", enter))

        def close():
            out.append(("emit", advance))
            jump(top)
            label(done)
            out.append(("emit", finish))
            label(after)

        loops.append({"cont": cont, "exit": after, "label": d.get("loop_label"), "close": close})
        # CYCLE jumps to the advance step, which is emitted when the loop closes: put its label there
        frame = loops[-1]
        inner = frame["close"]

        def close_with_cont():
            label(cont)
            inner()
        frame["close"] = close_with_cont

    # ------------------------------------------------------------------ emission
    def _machine_statement(self, kind: str, det: dict, raw: str) -> None:
        try:
            self.dispatch(kind, det, raw)
        except FortranParseError as exc:
            self.do_unsupported(f"could not parse expression ({exc})", raw)

    def _run_machine(self) -> List[str]:
        stmts = self.unit.stmts
        header = stmts[0] if stmts and stmts[0].stmt_type in ("SUBROUTINE", "FUNCTION", "PROGRAM") else None
        self._machine_labels = 0
        self.begin_unit()
        body: List[FortranStatement] = []
        for s in stmts[1:] if header is not None else stmts:
            if s.stmt_type in _DECLARATIVE:
                self.statement(s)
            else:
                body.append(s)
        instrs = self._lower(body)

        # split into blocks at labels and after terminators
        blocks: List[List[tuple]] = [[]]
        names = {}
        for ins in instrs:
            if ins[0] == "label":
                if blocks[-1]:
                    blocks.append([])
                names[ins[1]] = len(blocks) - 1
                continue
            blocks[-1].append(ins)
            if ins[0] in ("jump", "branch", "switch", "arith"):
                blocks.append([])
        missing = {t for ins in instrs for t in self._targets(ins) if t not in names}
        self.line("_pc = 0")
        self.line("while True:")
        self.level += 1
        for k, block in enumerate(blocks):
            self.line(f"if _pc == {k}:")
            self.level += 1
            start = len(self.lines)
            fell_through = True
            for ins in block:
                op = ins[0]
                if op == "emit":
                    ins[1]()
                elif op == "jump":
                    self._goto(names, ins[1], missing)
                    fell_through = False
                elif op == "branch":
                    self.line(f"if {ins[1]()}:")
                    self.level += 1
                    self._goto(names, ins[2], missing)
                    self.level -= 1
                    self.line("else:")
                    self.level += 1
                    self._goto(names, ins[3], missing)
                    self.level -= 1
                    fell_through = False
                elif op == "switch":
                    sel = ins[1]()
                    for pos, target in enumerate(ins[2], 1):
                        self.line(f"if {sel} == {pos}:")
                        self.level += 1
                        self._goto(names, target, missing)
                        self.level -= 1
                elif op == "arith":
                    expr = ins[1]()
                    self.line(f"if {expr} < 0:")
                    self.level += 1
                    self._goto(names, ins[2], missing)
                    self.level -= 1
                    self.line(f"elif {expr} == 0:")
                    self.level += 1
                    self._goto(names, ins[3], missing)
                    self.level -= 1
                    self.line("else:")
                    self.level += 1
                    self._goto(names, ins[4], missing)
                    self.level -= 1
                    fell_through = False
            if fell_through and k + 1 < len(blocks):
                self.line(f"_pc = {k + 1}")
            elif fell_through:
                self.line("break")
            if len(self.lines) == start:
                self.line("pass")
            self.level -= 1
        self.line("break")
        self.level -= 1
        self.end_unit()
        return self.lines

    @staticmethod
    def _targets(ins: tuple) -> List[str]:
        if ins[0] == "jump":
            return [ins[1]]
        if ins[0] == "branch":
            return [ins[2], ins[3]]
        if ins[0] == "switch":
            return list(ins[2])
        if ins[0] == "arith":
            return [ins[2], ins[3], ins[4]]
        return []

    def _goto(self, names: dict, target: str, missing: set) -> None:
        if target in missing:
            self.do_unsupported(f"GO TO {target[1:]}: no statement carries that label", target)
            return
        self.line(f"_pc = {names[target]}")
        self.line("continue")
