#!/usr/bin/env python3
"""Static check that a translation has the same equations and the same algorithm as the Fortran source.

Output comparison shows that some numbers agree on one input. This compares the programs themselves, statement by statement:
same assignments with the same expression trees (operator order, parentheses, kind conversions), same loops (variable, bounds,
step, nesting), same branch conditions, same calls and I/O in the same order.

Independence: the Fortran side comes from the compiler (`gfortran -fdump-fortran-original`), the Python side from the standard
`ast` module. Nothing here imports the translator, so a translator bug cannot hide itself. The one shared piece of knowledge is
REWRITES below: each entry states what a construct of the translation means in Fortran. It is the trusted table; a construct
that is not in it is a failure, never a pass.

Exit status: 0 every statement matches, 1 a statement differs, 2 something could not be checked (not a pass).
Constant expressions are compared after folding, because gfortran folds them in the dump: `1.0/3.0` is one number on both sides.
A pass is structural identity of these programs; it is not a proof about the translator, nor about the libraries it calls.
"""

import argparse
import ast
import math
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verification_evidence as ve  # noqa: E402

CHECKER_VERSION = "1"
SUPPORTED_GFORTRAN = (13,)          # the dump is not a stable interface: other majors stop with exit 2 until checked


class Unchecked(Exception):
    """Something could not be read. Exit 2: this is not a pass."""


# --------------------------------------------------------------------------------------------------------------
# REWRITES: what each construct of the translation means in Fortran
# --------------------------------------------------------------------------------------------------------------

# Python call -> intrinsic. The Fortran name is the dump's, with the kind suffix removed by _intrinsic().
PY_CALLS = {
    "_fm.sqrt": "sqrt", "_fm.exp": "exp", "_fm.log": "log", "_fm.log10": "log10", "_fm.sin": "sin", "_fm.cos": "cos",
    "_fm.tan": "tan", "_fm.asin": "asin", "_fm.acos": "acos", "_fm.atan": "atan", "_fm.atan2": "atan2", "_fm.sinh": "sinh",
    "_fm.cosh": "cosh", "_fm.tanh": "tanh", "_fm.fmod": "mod", "_imod": "mod", "_fmodulo": "modulo", "_imodulo": "modulo",
    "np.sqrt": "sqrt", "np.exp": "exp", "np.log": "log", "np.sin": "sin", "np.cos": "cos", "np.abs": "abs", "abs": "abs",
    "_fsum": "sum", "_fprod": "product", "_fdot": "dot_product", "_fmatmul": "matmul", "np.max": "maxval", "np.min": "minval",
    "np.transpose": "transpose", "_ctrim": "trim", "min": "min", "max": "max", "np.size": "size", "np.conj": "conjg",
    "math.floor": "floor", "math.ceil": "ceiling", "math.trunc": "aint",
}
# dump intrinsic -> kind conversion to that kind. A conversion changes the rounding, so it is part of the equation.
_CONVERSION = re.compile(r"^__(?:convert_[a-z]\d+_([a-z]\d+)|dble_[a-z]\d+|real_(\d+)_[a-z]\d+|cmplx\d*_(\d+)_[a-z]\d+_[a-z]\d+)$")
# Python binary helpers -> Fortran operator: same operation, with IEEE or Fortran results where Python would raise or differ.
PY_OPERATORS = {"_fpowi": "**", "_fpow": "**", "_ipow": "**", "_fdiv": "/", "_idiv": "/", "_cdiv32": "/"}
# Python wrappers that do not change the value of a conforming expression, only keep it in range or in array form.
PY_TRANSPARENT = {"_i32", "_i64", "np.ravel", "np.array", "np.asarray"}
PY_CONVERSIONS = {"float": "r8", "np.float64": "r8", "np.float32": "r4", "complex": "c8", "np.complex128": "c8", "np.complex64": "c4",
                  "int": "i4", "np.int64": "i4", "_i32": None}


# --------------------------------------------------------------------------------------------------------------
# Canonical nodes (plain tuples, compared by match())
# --------------------------------------------------------------------------------------------------------------
#   ("num", value)   numpy scalar for reals (its dtype is the kind), int for integers
#   ("var", name)  ("idx", name, subs)  ("sec", lo, hi, step)  ("full",)
#   ("bin", op, l, r)  ("un", op, e)  ("conv", kind, e)  ("call", name, args)  ("str", s)  ("bool", b)  ("ctor", items)

def num(value) -> tuple:
    return ("num", value)


def fold(node: tuple) -> tuple:
    """Fold a binary or unary operation on two numbers, in their own kinds."""
    if node[0] == "bin" and node[2][0] == "num" and node[3][0] == "num":
        op, a, b = node[1], node[2][1], node[3][1]
        try:
            with np.errstate(all="ignore"):
                if op == "+":
                    return num(a + b)
                if op == "-":
                    return num(a - b)
                if op == "*":
                    return num(a * b)
                if op == "/":
                    if isinstance(a, int) and isinstance(b, int):
                        if b == 0:
                            return node
                        q = abs(a) // abs(b)
                        return num(q if (a < 0) == (b < 0) else -q)
                    return num(a / b)
                if op == "**" and isinstance(a, int) and isinstance(b, int) and b < 0:
                    return num(0 if abs(a) > 1 else (1 if a == 1 or b % 2 == 0 else -1))
                if op == "**" and isinstance(b, int) and b < 0:
                    r = type(a)(1)
                    for _ in range(-b):
                        r = r * a
                    return num(type(a)(1) / r)
                if op == "**" and isinstance(b, int) and b >= 0:
                    r = type(a)(1)
                    for _ in range(b):
                        r = r * a
                    return num(r)
        except (OverflowError, ZeroDivisionError, TypeError):
            return node
    if node[0] == "bin" and node[2][0] == "num" and node[3][0] == "num" and node[1] in ("<", "<=", ">", ">=", "==", "/="):
        a, b = node[2][1], node[3][1]
        return ("bool", {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b, "/=": a != b}[node[1]])
    if node[0] == "un" and node[1] == "-" and node[2][0] == "num":
        return num(-node[2][1])
    if node[0] == "conv" and node[2][0] == "num" and node[1] in ("r4", "r8"):
        return num((np.float32 if node[1] == "r4" else np.float64)(node[2][1]))
    if node[0] == "conv" and node[2][0] == "num" and node[1] in ("i4", "i8"):
        return num(int(node[2][1]))
    if node[0] == "conv" and node[1] in ("c4", "c8") and node[2][0] == "num":
        return fold(("conv", node[1], ("cnum", node[2][1], np.float64(0.0), node[1])))
    if node[0] == "bin" and node[2][0] == "cnum" and node[3][0] == "cnum" and node[1] in ("+", "-", "*", "/"):
        a, b = complex(node[2][1], node[2][2]), complex(node[3][1], node[3][2])
        r = {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else None}[node[1]]
        if r is not None:
            low = node[2][3] == "c4"
            wrap = np.float32 if low else np.float64
            return ("cnum", wrap(r.real), wrap(r.imag), node[2][3])
    if node[0] == "conv" and node[1] in ("c4", "c8") and node[2][0] == "cnum":
        low = node[1] == "c4"
        wrap = np.float32 if low else np.float64
        return ("cnum", wrap(node[2][1]), wrap(node[2][2]), node[1])
    return node


FOLDABLE = {
    "mod": lambda a, b: math.fmod(a, b) if isinstance(a, np.floating) else int(math.fmod(a, b)),
    "modulo": lambda a, b: a - math.floor(a / b) * b if isinstance(a, np.floating) else a % b,
    "floor": lambda a: int(math.floor(a)), "ceiling": lambda a: int(math.ceil(a)), "aint": lambda a: type(a)(math.trunc(a)),
    "nint": lambda a: int(math.copysign(math.floor(abs(a) + 0.5), a)), "abs": abs, "sqrt": np.sqrt,
    "anint": lambda a: type(a)(math.copysign(math.floor(abs(a) + 0.5), a)), "exp": np.exp, "log": np.log, "log10": np.log10, "sin": np.sin, "cos": np.cos, "tan": np.tan, "atan": np.arctan,
    "min": min, "max": max, "sign": lambda a, b: type(a)(math.copysign(abs(a), b)) if isinstance(a, np.floating) else (abs(a) if b >= 0 else -abs(a)),
}


def fold_call(name: str, args: Sequence[tuple]) -> tuple:
    """gfortran folds intrinsics of constants, so the other side must too."""
    if name in ("sum", "product") and len(args) == 1 and args[0][0] == "ctor" and args[0][1] and all(a[0] == "num" for a in args[0][1]):
        total = args[0][1][0][1]
        for item in args[0][1][1:]:
            total = total + item[1] if name == "sum" else total * item[1]
        return num(total)
    if name in FOLDABLE and args and all(a[0] == "num" for a in args):
        with np.errstate(all="ignore"):
            return num(FOLDABLE[name](*[a[1] for a in args]))
    return ("call", name, tuple(args))


def same_number(a, b) -> bool:
    if a == 0 and b == 0 and (isinstance(a, int) or isinstance(b, int)):
        return True                         # the dump prints a real zero as `0`, without its kind
    if isinstance(a, (int, np.integer)) and isinstance(b, (int, np.integer)) and not isinstance(a, bool):
        return int(a) == int(b)
    if isinstance(a, (float, np.floating)) and isinstance(b, (float, np.floating)):
        wide_a, wide_b = np.float64(a), np.float64(b)
        kinds_agree = (isinstance(a, np.float32) == isinstance(b, np.float32))
        return bool(kinds_agree and (wide_a == wide_b or (math.isnan(wide_a) and math.isnan(wide_b)))
                    and math.copysign(1.0, wide_a) == math.copysign(1.0, wide_b))
    return False


# --------------------------------------------------------------------------------------------------------------
# The Fortran side: gfortran's own parse tree
# --------------------------------------------------------------------------------------------------------------

@dataclass
class Sym:
    kind: str = ""                  # r4 r8 i4 i8 c4 c8 l4 ch dt ""
    lower: List[Any] = field(default_factory=list)        # lower bound per dimension: int or tree
    upper: List[Any] = field(default_factory=list)        # upper bound per dimension (None when assumed)
    shape_kind: str = ""            # EXPLICIT, ASSUMED_SIZE, ASSUMED_SHAPE, DEFERRED
    value: Optional[tuple] = None   # PARAMETER value
    parameter: bool = False
    dummy: bool = False
    derived: bool = False
    length: Optional[int] = None    # declared CHARACTER length
    common: bool = False
    pointer: bool = False
    allocatable: bool = False
    saved: bool = False
    equivalenced: bool = False


@dataclass
class Proc:
    name: str
    symbols: Dict[str, Sym] = field(default_factory=dict)
    commons: Dict[str, List[str]] = field(default_factory=dict)             # block -> this procedure's names, by position
    code: List[Tuple[Optional[str], str]] = field(default_factory=list)     # (label, statement text)
    module_parameters: Dict[str, "Sym"] = field(default_factory=dict)       # <module>_<name> -> its PARAMETER
    module_level: Dict[str, Any] = field(default_factory=dict)              # names the translated module defines
    host_symbols: Dict[str, "Sym"] = field(default_factory=dict)            # every symbol of the program, for use association
    formats: Dict[str, str] = field(default_factory=dict)                   # FORMAT label -> text (the dump leaves them out)
    is_main: bool = False
    host: Optional[str] = None                                              # the module or procedure that CONTAINS this one
    uses: Set[str] = field(default_factory=set)                             # namespaces its symbols come from
    declares: Dict[str, str] = field(default_factory=dict)                  # name -> subroutine, function or module
    initial: Dict[str, List[Any]] = field(default_factory=dict)             # DATA and initialised declarations, by position
    jumps: Optional[bool] = None                                            # the procedure has labels that matter
    equivalences: List[List[tuple]] = field(default_factory=list)           # each line: the objects made to share storage
    allocated_lower: Dict[str, List[Any]] = field(default_factory=dict)     # ALLOCATE (lo:hi): the lower bounds of a deferred array
    events: List[tuple] = field(default_factory=list)


class _Reader:
    """Recursive descent over one line of the dump's prefix notation."""

    NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?(?:_\d+)?", re.IGNORECASE)
    NAME = re.compile(r"[A-Za-z_$][\w$.]*(?::[A-Za-z_$][\w$.]*)?")
    OPERATORS = ("**", "//", "/=", "==", "<=", ">=", "<", ">", "+", "-", "*", "/")
    WORDS = {"AND": "and", "OR": "or", "EQV": "eqv", "NEQV": "neqv"}

    def __init__(self, text: str):
        self.text, self.i = text, 0

    def spaces(self) -> None:
        while self.i < len(self.text) and self.text[self.i] == " ":
            self.i += 1

    def peek(self, token: str) -> bool:
        self.spaces()
        return self.text.startswith(token, self.i)

    def take(self, token: str) -> bool:
        if self.peek(token):
            self.i += len(token)
            return True
        return False

    def need(self, token: str) -> None:
        if not self.take(token):
            raise Unchecked(f"dump: expected {token!r} at {self.i} in: {self.text}")

    def at_end(self) -> bool:
        self.spaces()
        return self.i >= len(self.text)

    def expr(self) -> tuple:
        self.spaces()
        text, i = self.text, self.i
        if text.startswith("(", i):
            return self.compound()
        if text.startswith("'", i):
            j = i + 1
            while j < len(text) and not (text[j] == "'" and text[j + 1:j + 2] != "'"):
                j += 2 if text[j] == "'" else 1
            self.i = j + 1
            return ("str", text[i + 1:j].replace("''", "'"))
        for word, value in ((".true.", True), (".false.", False)):
            if text.lower().startswith(word, i):
                self.i += len(word)
                self.skip_kind()
                return ("bool", value)
        match = self.NUMBER.match(text, i)
        if match:
            self.i = match.end()
            return self.number(match.group())
        match = self.NAME.match(text, i)
        if match:
            self.i = match.end()
            return self.designator(match.group())
        raise Unchecked(f"dump: cannot read at {i} in: {text}")

    def skip_kind(self) -> None:
        match = re.compile(r"_\d+").match(self.text, self.i)
        if match:
            self.i = match.end()

    @staticmethod
    def number(text: str) -> tuple:
        body, _, kind = text.partition("_")
        if re.search(r"[.e]", body, re.IGNORECASE):
            return num(np.float64(float(body)) if kind == "8" else np.float32(float(body)))
        return num(int(body))

    def compound(self) -> tuple:
        self.need("(")
        if self.peek("/ ") or self.peek("/)"):
            saved = self.i
            self.need("/")
            try:
                items = [] if self.peek("/)") else self.ctor_items()
            except Unchecked:
                items = None
            if items is not None and self.take("/)"):
                return ("ctor", tuple(items))
            self.i = saved
        if self.take("complex "):
            real, imag = self.expr(), self.expr()
            self.need(")")
            kind = "c8" if isinstance(real[1], np.float64) else "c4"
            return ("cnum", real[1], imag[1], kind)
        if self.take("parens "):
            inner = self.expr()
            self.need(")")
            return inner
        for word, op in (("U-", "-"), ("U+", "+"), ("NOT", "not")):
            if self.take(word + " "):
                inner = self.expr()
                self.need(")")
                return inner if op == "+" else fold(("un", op, inner))
        op = None
        for word, value in self.WORDS.items():
            if self.take(word + " "):
                op = value
                break
        if op is None:
            for token in self.OPERATORS:
                if self.take(token + " "):
                    op = token
                    break
        if op is None:
            inner = self.expr()                 # a bare (expr) around an argument
            self.need(")")
            return inner
        left = self.expr()
        right = self.expr()
        self.need(")")
        return fold(("bin", op, left, right))

    def ctor_items(self) -> List[tuple]:
        items = [self.ctor_item()]
        while self.take(","):
            items.append(self.ctor_item())
        return items

    def ctor_item(self) -> tuple:
        """An item, or the implied DO `((/ e , ... /) i=1,n,1)`."""
        if self.peek("((/"):
            self.need("(")
            body = self.expr()
            if not self.peek(")"):
                variable = self.expr()
                self.need("=")
                start = self.expr()
                self.need(",")
                stop = self.expr()
                self.need(",")
                step = self.expr()
                self.need(")")
                return ("impdo", body[1], variable, start, stop, step)
            self.need(")")
            return body
        return self.expr()

    def designator(self, name: str) -> tuple:
        if self.text.startswith("[[(", self.i):
            self.i += 3
            args = self.call_args()
            self.need(")]]")
            return _intrinsic(name.split(":")[-1], args)
        if self.text.startswith("[(", self.i):
            self.i += 2
            args = self.call_args()
            self.need(")]")
            return _intrinsic(name.split(":")[-1], args)
        short = name.split(":")[-1]
        node: tuple = ("var", short)
        if self.text.startswith("(", self.i):
            self.i += 1
            subs = self.subscripts()
            self.need(")")
            node = node if subs == [("full",)] else ("idx", short, tuple(subs))
        while self.peek("%"):
            self.need("%")
            self.spaces()
            match = self.NAME.match(self.text, self.i)
            if not match:
                raise Unchecked(f"dump: component expected in: {self.text}")
            self.i = match.end()
            node = ("comp", node, match.group())
            if self.text.startswith("(", self.i):
                self.i += 1
                subs = self.subscripts()
                self.need(")")
                if subs != [("full",)]:
                    node = ("idxc", node, tuple(subs))
        return node

    def subscripts(self) -> List[tuple]:
        subs = [self.subscript()]
        while self.take(","):
            subs.append(self.subscript())
        return subs

    def subscript(self) -> tuple:
        if self.take("FULL"):
            return ("full",)
        lo = None if self.peek(":") else self.expr()
        if not self.take(":"):
            return lo
        hi = None if self.peek(":") or self.peek(",") or self.peek(")") else self.expr()
        step = self.expr() if self.take(":") else None
        return ("full",) if lo is None and hi is None and step is None else ("sec", lo, hi, step)

    def call_args(self) -> List[Optional[tuple]]:
        args: List[Optional[tuple]] = []
        while self.take("("):
            if self.take("(arg not-present)"):
                args.append(None)
            else:
                args.append(self.expr())
            self.need(")")
        return args


def _intrinsic(name: str, args: Sequence[Optional[tuple]]) -> tuple:
    """A dump call to its canonical form: conversions become ("conv", ...), kind suffixes are removed."""
    present = [a for a in args if a is not None]
    match = _CONVERSION.match(name)
    if match and name.startswith("__cmplx"):
        return ("call", "cmplx", tuple(present))
    if match:
        if name.startswith("__dble"):
            return fold(("conv", "r8", present[0]))
        if match.group(1):
            return fold(("conv", match.group(1), present[0]))
        return fold(("conv", "r" + match.group(2), present[0]))
    if name in ("_gfortran_idint", "_gfortran_ifix") or re.fullmatch(r"__int_4_[a-z]\d+", name) or re.fullmatch(r"__convert_[a-z]\d+_i4", name):
        return fold(("conv", "i4", present[0]))
    if name == "_gfortran_allocated":
        return ("call", "associated", tuple(present))     # both mean "has a target": `x is not None` in Python
    bare = re.sub(r"^(?:__|_gfortran_)", "", name)
    if name.startswith(("__", "_gfortran_")):
        bare = re.sub(r"_\d+$", "", re.sub(r"_[a-z]\d+$", "", bare))
        bare = re.sub(r"^(floor|ceiling|nint|len_trim|trim)\d$", r"\1", bare)
    return fold_call(bare, present)


_SYMTREE = re.compile(r"^\s*symtree: '([^']+)'\s*\|\| symbol: '([^']+)'(.*)$")
_TYPE = re.compile(r"type spec\s*:\s*\((\w+)(?:\s+(\d+))?(?:\s+(\d+))?")


def parse_dump(text: str) -> Dict[str, Proc]:
    procs: Dict[str, Proc] = {}
    current: Optional[Proc] = None
    sym: Optional[Sym] = None
    in_code = False
    nesting: List[Tuple[int, str]] = []         # (indent, name) of the procedures around the current one
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r"^\s*procedure name = (\S+)", line)
        if match:
            current = Proc(match.group(1))
            procs[current.name] = current
            sym, in_code = None, False
            indent = len(line) - len(line.lstrip())
            while nesting and nesting[-1][0] >= indent:
                nesting.pop()
            current.host = nesting[-1][1] if nesting else None
            nesting.append((indent, current.name))
            continue
        if current is None:
            continue
        if in_code:
            if stripped.startswith("-----") or stripped == "CONTAINS" or stripped.startswith("Namespace:"):
                in_code = False
            elif stripped:
                label = re.match(r"^\s*(\d+)\s+(.*)$", line)
                text = label.group(2).strip() if label else stripped
                first, *rest = re.split(r"\s{2,}(?=END DO$)", text)       # the dump can run END DO into the line before
                current.code.append((label.group(1) if label else None, first))
                current.code.extend((None, more) for more in rest)
            continue
        if stripped == "code:":
            in_code = True
            continue
        match = re.match(r"^\s*Equivalence: (.*)$", line)
        if match and current is not None:
            current.equivalences.append([_Reader(item.strip()).expr() for item in match.group(1).split(", ")])
            continue
        match = re.match(r"^\s*common: /(\w*)/ (.*)$", line)
        if match:
            current.commons[match.group(1).lower()] = [n.strip() for n in match.group(2).split(",")]
            continue
        match = _SYMTREE.match(line)
        if match:
            sym = Sym()
            symbol_name = match.group(1)
            origin = re.search(r"from namespace '(\w+)'", match.group(3))
            if origin:
                current.uses.add(origin.group(1))
            else:
                current.symbols[symbol_name] = sym
            continue
        if sym is None:
            continue
        match = _TYPE.search(line)
        if match:
            letter = {"REAL": "r", "INTEGER": "i", "COMPLEX": "c", "LOGICAL": "l"}.get(match.group(1))
            if match.group(1) == "CHARACTER":
                sym.length = int(match.group(2) or 1)
            sym.kind = letter + (match.group(2) or "") if letter else ("" if match.group(1) == "UNKNOWN" else match.group(1).lower())
        elif stripped.startswith("attributes:"):
            sym.parameter = "PARAMETER" in stripped
            current.is_main = current.is_main or "IS-MAIN-PROGRAM" in stripped
            flags = stripped[len("attributes:"):].strip(" ()").split()
            for word in ("SUBROUTINE", "FUNCTION", "MODULE"):
                if word in flags:
                    current.declares[symbol_name] = word.lower()
            if "USE-ASSOC" in stripped or "USE ASSOC" in stripped:
                origin = re.search(r"USE-?ASSOC\((\w+)\)", stripped)
                if origin:
                    current.uses.add(origin.group(1))
            sym.dummy = "DUMMY" in stripped
            sym.common = "IN-COMMON" in stripped
            sym.pointer = "POINTER" in stripped
            sym.allocatable = "ALLOCATABLE" in stripped
            sym.saved = "SAVE" in stripped or "DATA" in stripped
            sym.equivalenced = "EQUIVALENCE" in stripped
        elif stripped.startswith("value:") and sym.parameter:
            sym.value = _Reader(stripped[len("value:"):].strip()).expr()
        elif stripped.startswith("Array spec:"):
            sym.lower, sym.upper, sym.shape_kind = _array_bounds(stripped)
    return procs


def _array_bounds(line: str) -> Tuple[List[Any], List[Any], str]:
    match = re.match(r"Array spec:\((\d+) \[\d+\] AS_(\w+) (.*)\)$", line)
    if not match:
        return [], [], ""
    rank, reader = int(match.group(1)), _Reader(match.group(3))
    lower, upper = [], []
    for _ in range(rank):
        lo = None if reader.take("()") else reader.expr()
        hi = None if reader.at_end() or reader.take("()") else reader.expr()
        lower.append(1 if lo is None else (lo[1] if lo[0] == "num" else lo))
        upper.append(hi)
    return lower, upper, match.group(2)


def _part_of(proc: Proc, code: List) -> Proc:
    """A CASE body, read as part of its procedure."""
    jumps = proc.jumps if proc.jumps is not None else any(text.startswith("GOTO") for _, text in proc.code)
    return Proc(proc.name, proc.symbols, proc.commons, code, jumps=jumps, formats=proc.formats)


def _case_test(selector: tuple, ranges: List[tuple]) -> Optional[tuple]:
    """CASE (lo hi) ... as the test it means. None for CASE DEFAULT."""
    test = None
    for lo, hi in ranges:
        if lo is None and hi is None:
            return None
        if lo is None:
            one = ("bin", "<=", selector, hi)
        elif hi is None:
            one = ("bin", ">=", selector, lo)
        elif lo == hi:
            one = ("bin", "==", selector, lo)
        else:
            one = ("bin", "and", ("bin", ">=", selector, lo), ("bin", "<=", selector, hi))
        test = one if test is None else ("bin", "or", test, one)
    return test


def _select(code, k: int, selector: tuple, events: List[tuple], proc: Proc) -> int:
    """SELECT CASE is the IF chain it means; returns the index after END SELECT. CASE DEFAULT runs last wherever it stands."""
    arms: List[Tuple[Optional[tuple], List]] = []
    depth = 0
    while k < len(code):
        label, text = code[k]
        k += 1
        if depth == 0 and text.startswith("CASE "):
            reader = _Reader(text[len("CASE "):])
            ranges = []
            while reader.take("("):
                lo = None if reader.take("()") else reader.expr()
                hi = None if reader.take("()") else reader.expr()
                reader.need(")")
                ranges.append((lo, hi))
            arms.append((_case_test(selector, ranges), []))
        elif depth == 0 and text == "END SELECT":
            break
        else:
            depth += text.startswith("SELECT ") - (text == "END SELECT")
            if arms:
                arms[-1][1].append((label, text))
    default = [body for test, body in arms if test is None]
    first = True
    for test, body in arms:
        if test is None:
            continue
        events.append(("if" if first else "elif", test))
        first = False
        events.extend(fortran_events(_part_of(proc, body)))
    if default:
        if first:
            events.extend(fortran_events(_part_of(proc, default[0])))
            return k
        events.append(("else",))
        events.extend(fortran_events(_part_of(proc, default[0])))
    if not first:
        events.append(("end",))
    return k


def _io_fields(body: str) -> Dict[str, tuple]:
    """UNIT=10 FMT='(A)' IOSTAT=x as {"UNIT": tree, ...}."""
    fields = {}
    for piece in re.split(r"\s+(?=[A-Z]+=)", body.strip()):
        name, _, value = piece.partition("=")
        fields[name] = _Reader(value).expr()
    return fields


def _io_format(node: tuple, proc: Proc) -> Optional[tuple]:
    """None for list-directed; a label (the dump shows it as a number) is the text of its FORMAT."""
    if node == num(-1):
        return None
    return ("str", proc.formats.get(str(node[1]), "")) if node[0] == "num" else node


def _statement_end(code: List, k: int) -> Tuple[int, List[tuple]]:
    """Consume DT_END. END= and ERR= are jumps taken after the statement; the dump may run the next statement into this line."""
    ends: List[tuple] = []
    if k >= len(code) or not code[k][1].startswith("DT_END"):
        return k, ends
    for name, target in re.findall(r"(END|ERR)=(\d+)", code[k][1]):
        test = ("bin", "<" if name == "END" else ">", ("var", "_ios"), num(0))
        ends += [("if", test), ("goto", target), ("end",)]
    return k + 1, ends


def _allocation(obj: tuple, proc: Proc) -> tuple:
    """ALLOCATE a(lo:hi, n): the object, its extents. The lower bounds are kept, because the subscripts depend on them."""
    if obj[0] != "idx":
        return ("alloc", obj, ())
    lows = [sub[1] if sub[0] == "sec" else num(1) for sub in obj[2]]
    highs = [sub[2] if sub[0] == "sec" else sub for sub in obj[2]]
    proc.allocated_lower[obj[1]] = [low[1] if low[0] == "num" else low for low in lows]
    return ("alloc", ("var", obj[1]), tuple(fold(_extent(low[1] if low[0] == "num" else low, high)) for low, high in zip(lows, highs)))


def _transfers(code, k: int) -> Tuple[List[tuple], int]:
    """The items of an I/O statement; DO ... TRANSFER ... END DO is an implied DO. Returns (items, next index)."""
    items: List[tuple] = []
    while k < len(code):
        text = code[k][1]
        if text.startswith("TRANSFER "):
            items.append(_Reader(text[len("TRANSFER "):]).expr())
            k += 1
        elif text.startswith("DO "):
            reader = _Reader(re.sub(r"^DO\s+", "", text).replace("=", " ", 1))
            header = (reader.expr(), reader.expr(), reader.expr(), reader.expr())
            body, k = _transfers(code, k + 1)
            items.append(("impdo", tuple(body), *header))
            if k < len(code) and code[k][1] == "END DO":
                k += 1
        else:
            break
    return items, k


def fortran_events(proc: Proc) -> List[tuple]:
    """The procedure as a flat list of events."""
    events: List[tuple] = []
    code = proc.code
    open_labels: List[str] = []
    jumps = proc.jumps if proc.jumps is not None else any(
        text.startswith("GOTO") or re.match(r"IF .* \d+, *\d+", text) or re.search(r"\b(?:END|ERR)=\d+", text) for _, text in code)
    code = list(code)
    k = 0
    while k < len(code):
        label, text = code[k]
        k += 1
        if label and jumps:
            events.append(("label", label))
        head = text.split(" ", 1)[0]
        body = text[len(head):].strip()
        if head == "ASSIGN":
            reader = _Reader(body)
            target = reader.expr()
            events.append(("assign", target, reader.expr()))
        elif head == "DO":
            match = re.match(r"^(?:WHILE\s*)?(.*)$", body)
            if body.startswith("WHILE"):
                events.append(("while", _Reader(body[5:].strip()).expr()))
            else:
                terminal = re.match(r"^(\d+)\s+", match.group(1))       # DO 10 i=...: the loop ends at label 10
                if terminal:
                    open_labels.append(terminal.group(1))
                reader = _Reader(match.group(1)[terminal.end() if terminal else 0:].replace("=", " ", 1))
                events.append(("do", reader.expr(), reader.expr(), reader.expr(), reader.expr()))
        elif text == "END DO":
            events.append(("end",))
        elif head == "IF":
            reader = _Reader(body)
            cond = reader.expr()
            if not reader.at_end():                  # arithmetic IF: negative, zero, positive
                negative, zero, positive = re.findall(r"\d+", reader.text[reader.i:])
                events += [("if", ("bin", "<", cond, num(0))), ("goto", negative), ("elif", ("bin", "==", cond, num(0))),
                           ("goto", zero), ("else",), ("goto", positive), ("end",)]
            else:
                events.append(("if", cond))
        elif text == "ELSE":
            events.append(("else",))
        elif head == "ELSE" and body.startswith("IF"):
            events.append(("elif", _Reader(body[2:].strip()).expr()))
        elif text == "ENDIF":
            events.append(("end",))
        elif head == "ALLOCATE":
            reader = _Reader(body)
            while not reader.at_end():
                events.append(_allocation(reader.expr(), proc))
        elif head == "DEALLOCATE":
            reader = _Reader(body)
            while not reader.at_end():
                events.append(("free", reader.expr()))
        elif head == "POINTER":
            reader = _Reader(body[len("ASSIGN"):].strip())
            target = reader.expr()
            events.append(("free", target) if re.search(r"NULL\(.*\)\s*$", body) else ("ptr", target, reader.expr()))
        elif head == "SELECT":
            k = _select(code, k, _Reader(body[len("CASE"):].strip()).expr(), events, proc)
        elif text in ("CONTINUE",):
            pass
        elif text == "EXIT" or head == "EXIT":
            events.append(("exit",))
        elif text == "CYCLE" or head == "CYCLE":
            events.append(("cycle",))
        elif head == "RETURN":
            events.append(("return",))
        elif head == "GOTO":
            events.append(("goto", body))
        elif head == "READ":
            items, k = _transfers(code, k)
            k, ends = _statement_end(code, k)
            fields = _io_fields(body)
            events.append(("read", fields["UNIT"], _io_format(fields["FMT"], proc), tuple(items), fields.get("IOSTAT"),
                           bool(ends) or "IOSTAT" in fields))
            events.extend(ends)
        elif head in ("OPEN", "CLOSE"):
            fields = _io_fields(body)
            unit, iostat = fields.pop("UNIT"), fields.pop("IOSTAT", None)
            events.append((head.lower(), unit, tuple(sorted((k2.lower(), v) for k2, v in fields.items())), iostat))
        elif head == "REWIND":
            events.append(("rewind", _io_fields(body)["UNIT"]))
        elif head == "INQUIRE":
            fields = _io_fields(body)
            events.append(("inquire", fields.get("FILE"), fields.get("EXIST")))
        elif head == "CALL":
            match = re.match(r"^(\S+)\s*(.*)$", body)
            reader = _Reader(match.group(2))
            reader.need("(")
            args = reader.call_args()
            reader.need(")")
            events.append(("call", match.group(1).split(":")[-1], tuple(args)))
        elif head == "WRITE":
            items, k = _transfers(code, k)
            k, ends = _statement_end(code, k)
            fields = _io_fields(body)
            if ends or fields["UNIT"] != num(6) or "IOSTAT" in fields:
                events.append(("fwrite", fields["UNIT"], _io_format(fields["FMT"], proc), tuple(items), fields.get("IOSTAT")))
                events.extend(ends)
            elif fields["FMT"] == num(-1):
                events.append(("write", tuple(items)))
            else:
                events.append(("write", _io_format(fields["FMT"], proc), tuple(items)))
        else:
            events.append(("unknown", text))
        while label and open_labels and open_labels[-1] == label:     # loops may share one terminal statement
            open_labels.pop()
            events.append(("end",))
    return events


_STATIC = re.compile(r"^\s*static\s+(integer|real|logical)\(kind=(\d+)\)\s+(\w+)(?:\[(\d+)\])?\s*=\s*(.*);\s*$")


def initial_values(paths: Sequence[Path]) -> Dict[str, Dict[str, List[Any]]]:
    """procedure -> variable -> its initial values, in column order, zeros filled in. -fdump-tree-original holds them by position
    (`{[1]=20, [3]=40}`), which the parse-tree dump does not."""
    with tempfile.TemporaryDirectory() as work:
        done = subprocess.run(["gfortran", "-c", "-w", "-fdump-tree-original", "-o", "/dev/null"]
                              + [str(Path(p).resolve()) for p in paths], capture_output=True, text=True, cwd=work)
        if done.returncode != 0:
            raise Unchecked(f"gfortran could not read the sources: {done.stderr.strip()[:300]}")
        text = "\n".join(path.read_text(errors="replace") for path in sorted(Path(work).glob("*.original")))
    table: Dict[str, Dict[str, List[Any]]] = {}
    current = None
    for line in text.splitlines():
        header = re.match(r"^(?:\S.*\s)?([A-Za-z_][\w.]*) \(.*\)\s*$", line) if line[:1].strip() and not line.startswith("__attribute__") else None
        if header:
            current = re.sub(r"^__\w+_MOD_", "", header.group(1)).lower()
            continue
        match = _STATIC.match(line)
        if not match or current is None or match.group(3).startswith("options"):
            continue
        family, kind, name, size, body = match.groups()
        def convert(item: str) -> Any:
            item = item.strip()
            if family == "real":
                return (np.float64 if kind == "8" else np.float32)(float(item))
            return int(item)
        if size is None:
            table.setdefault(current, {})[name.lower()] = [convert(body)]
            continue
        values: List[Any] = [convert("0")] * int(size)
        position = 0
        for first, last, item in re.findall(r"(?:\[(\d+)(?: \.\.\. (\d+))?\]=)?([^,{}\s\[][^,{}]*)", body):
            position = int(first) if first else position
            for at in range(position, (int(last) if last else position) + 1):     # [0 ... 2]=1.5: a repeat count
                values[at] = convert(item)
            position = (int(last) if last else position) + 1
        table.setdefault(current, {})[name.lower()] = values
    return table


def dump_fortran(paths: Sequence[Path]) -> str:
    """gfortran's parse tree of every source, in order."""
    version = subprocess.run(["gfortran", "-dumpversion"], capture_output=True, text=True)
    if version.returncode != 0:
        raise Unchecked("gfortran is not available")
    major = int(version.stdout.strip().split(".")[0])
    if major not in SUPPORTED_GFORTRAN:
        raise Unchecked(f"gfortran {major}: the dump format has only been checked for {SUPPORTED_GFORTRAN}")
    with tempfile.TemporaryDirectory() as work:
        done = subprocess.run(["gfortran", "-c", "-w", "-fdump-fortran-original", "-o", "/dev/null"]
                              + [str(Path(p).resolve()) for p in paths], capture_output=True, text=True, cwd=work)
    if done.returncode != 0:
        raise Unchecked(f"gfortran could not read the sources: {done.stderr.strip()[:300]}")
    return done.stdout


# --------------------------------------------------------------------------------------------------------------
# The Python side
# --------------------------------------------------------------------------------------------------------------

_BIN = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Pow: "**"}
_CMP = {ast.Eq: "==", ast.NotEq: "/=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted(node.value) + "." + node.attr
    return ""


class PythonReader:
    def __init__(self, proc: Proc, classes: Optional[Dict[str, List[str]]] = None):
        self.proc = proc
        self.classes = classes or {}
        self.used_parameters: set = set()
        self.machine = False                # inside the GO TO state machine
        self.loop_depth = 0                 # real Python loops around the statement
        self.machine_ranges: Dict[str, ast.AST] = {}
        self.machine_counters: set = set()
        self.block_end = False              # walking the top level of a block of the machine
        self.next_block: Optional[int] = None

    def member(self, node: ast.Attribute) -> tuple:
        """`acc_common.total` is the member at that position of /acc/, whatever this procedure calls it."""
        owner = dotted(node.value).lower()
        if not owner.endswith("_common"):
            return ("comp", self.expr(node.value), node.attr.lower())
        if owner.endswith("_common"):
            block = owner[:-len("_common")]
            fields = self.classes.get(block + "block")
            locals_ = self.proc.commons.get(block)
            if fields and locals_ and node.attr in fields:
                return ("var", locals_[fields.index(node.attr)])
            return ("var", node.attr.lower())
        return ("var", node.attr.lower())

    def expr(self, node: ast.AST) -> tuple:
        if isinstance(node, ast.Constant):
            value = node.value
            if value is Ellipsis:
                return ("full",)
            if isinstance(value, bool):
                return ("bool", value)
            if isinstance(value, int):
                return num(value)
            if isinstance(value, float):
                return num(np.float64(value))
            if isinstance(value, str):
                return ("str", value)
            raise Unchecked(f"python constant {value!r}")
        if isinstance(node, ast.Name):
            return self.name(node.id)
        if isinstance(node, ast.Attribute) and node.attr == "T":
            return ("call", "transpose", (self.expr(node.value),))
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call) and dotted(node.value.func) == "np.finfo":
            wrap = {"np.float64": np.float64, "np.float32": np.float32}.get(dotted(node.value.args[0]))
            if wrap is None or node.attr not in ("eps", "tiny", "max"):
                raise Unchecked(f"python expression not in the table: {ast.unparse(node)}")
            return num(wrap(getattr(np.finfo(wrap), node.attr)))
        if isinstance(node, ast.Attribute) and node.attr in ("real", "imag"):
            inner = self.expr(node.value)
            if node.attr == "imag":
                return ("call", "aimag", (inner,))
            kind = kind_of(inner, self.proc)
            return ("conv", "r" + kind[1:] if kind else "r8", inner)
        if isinstance(node, ast.IfExp):
            return self.sign(node)
        if isinstance(node, ast.Attribute):
            return self.member(node)
        if isinstance(node, ast.UnaryOp):
            inner = self.expr(node.operand)
            if isinstance(node.op, ast.USub):
                return fold(("un", "-", inner))
            if isinstance(node.op, ast.UAdd):
                return inner
            if isinstance(node.op, ast.Not):
                return ("un", "not", inner)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return fold_call("modulo", (self.expr(node.left), self.expr(node.right)))    # Python's % takes the sign of the divisor
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            return fold(("bin", _BIN[type(node.op)], self.expr(node.left), self.expr(node.right)))
        if isinstance(node, ast.BoolOp):
            op = "and" if isinstance(node.op, ast.And) else "or"
            out = self.expr(node.values[0])
            for value in node.values[1:]:
                out = ("bin", op, out, self.expr(value))
            return out
        if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.IsNot) and getattr(node.comparators[0], "value", 0) is None:
            return ("call", "associated", (self.expr(node.left),))
        if isinstance(node, ast.Compare) and re.fullmatch(r"_doG\d+_i < len\(_doG\d+\)", ast.unparse(node)):
            return ("dotest",)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP:
            left, right = self.expr(node.left), self.expr(node.comparators[0])
            if left[0] == "ccmp" and right == num(0):
                return ("bin", _CMP[type(node.ops[0])], left[1], left[2])
            return fold(("bin", _CMP[type(node.ops[0])], left, right))
        if isinstance(node, ast.Subscript):
            return self.subscript(node)
        if isinstance(node, ast.Call):
            return self.call(node)
        if isinstance(node, ast.ListComp):
            return self.implied_do(node)
        if isinstance(node, (ast.List, ast.Tuple)):
            items = []
            for element in node.elts:
                inner = self.expr(element.value if isinstance(element, ast.Starred) else element)
                items.extend(inner[1] if inner[0] == "ctor" and isinstance(element, ast.Starred) else (inner,))
            return ("ctor", tuple(items))
        raise Unchecked(f"python expression not in the table: {ast.unparse(node)}")

    def implied_do(self, node: ast.ListComp) -> tuple:
        """[e for i in range(a, b + 1, s)], the implied DO of a constructor or an I/O list."""
        generator = node.generators[0]
        if len(node.generators) != 1 or generator.ifs or dotted(generator.iter.func) != "range":
            raise Unchecked(f"python expression not in the table: {ast.unparse(node)}")
        args = [self.expr(a) for a in generator.iter.args]
        start, stop = (num(0), args[0]) if len(args) == 1 else (args[0], args[1])
        step = args[2] if len(args) == 3 else num(1)
        return ("impdo", (self.expr(node.elt),), self.expr(generator.target), start, _plus(stop, -1), step)

    def sign(self, node: ast.IfExp) -> tuple:
        """SIGN(a, b) of integers is written `abs(a) if b >= 0 else -abs(a)`."""
        test = node.test
        if (isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.GtE) and isinstance(node.orelse, ast.UnaryOp)
                and ast.dump(node.body) == ast.dump(node.orelse.operand) and isinstance(node.body, ast.Call)
                and dotted(node.body.func) == "abs"):
            a, b = self.expr(node.body.args[0]), self.expr(test.left)
            if a[0] == "num" and b[0] == "num":
                return num(abs(a[1]) if b[1] >= 0 else -abs(a[1]))
            return ("call", "sign", (a, b))
        raise Unchecked(f"python expression not in the table: {ast.unparse(node)}")

    def name(self, name: str) -> tuple:
        low = name.lower()
        sym = self.proc.symbols.get(low.rstrip("_")) or self.proc.symbols.get(low) or self.proc.module_parameters.get(low)
        if sym is None and low in self.proc.host_symbols and self.proc.host_symbols[low].parameter:
            sym = self.proc.host_symbols[low]           # a host's PARAMETER, repeated in its internal procedure
        if sym is None:                     # a module's PARAMETER is <module>_<name>
            sym = next((s for n, s in self.proc.symbols.items() if s.parameter and low.endswith("_" + n)), None)
        if sym is not None and sym.parameter and sym.value is not None:
            self.used_parameters.add(low)
            return sym.value
        return ("var", low)

    def subscript(self, node: ast.Subscript) -> tuple:
        if isinstance(node.value, ast.Call) and dotted(node.value.func) == "np.shape":
            dimension = self.expr(node.slice)
            return fold_call("size", (self.expr(node.value.args[0]), _plus(dimension, 1)))
        if isinstance(node.value, ast.Call) and dotted(node.value.func) == "np.reshape" and isinstance(node.slice, ast.Slice):
            return ("seq", self.expr(node.value.args[0]), self.expr(node.slice.lower))      # storage sequence from that offset
        base = self.expr(node.value)
        if base[0] == "comp":
            raw = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            subs = [self.fortran_subscript(sub, 1) for sub in raw]
            return base if all(s == ("full",) for s in subs) else ("idxc", base, tuple(subs))
        if base[0] == "idx":
            raise Unchecked(f"python subscript of an element: {ast.unparse(node)}")
        if base[0] != "var":
            raise Unchecked(f"python subscript of an expression: {ast.unparse(node)}")
        sym = self.proc.symbols.get(base[1])
        if sym is not None and not sym.upper and isinstance(node.slice, ast.Constant) and node.slice.value == 0:
            return base                     # a TARGET scalar lives in a one-element array, so a pointer can share it
        lower = self.proc.allocated_lower.get(base[1]) or (sym.lower if sym else [])
        raw = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        subs = []
        for dim, sub in enumerate(raw):
            low = lower[dim] if dim < len(lower) else 1
            subs.append(self.fortran_subscript(sub, low))
        if all(s == ("full",) for s in subs):
            return base
        return ("idx", base[1], tuple(subs))

    def fortran_subscript(self, sub: ast.AST, low: Any) -> tuple:
        """Undo the 0-based shift: Python index s is Fortran index s + lower bound (a slice's stop is exclusive)."""
        if isinstance(sub, ast.Constant) and sub.value is Ellipsis:
            return ("full",)
        if isinstance(sub, ast.Slice):
            if sub.lower is None and sub.upper is None and sub.step is None:
                return ("full",)
            lo = None if sub.lower is None else _plus(self.expr(sub.lower), low)
            hi = None if sub.upper is None else _plus(_plus(self.expr(sub.upper), low), -1)
            step = None if sub.step is None else self.expr(sub.step)
            return ("sec", lo, hi, step)
        return _plus(self.expr(sub), low)

    def call(self, node: ast.Call) -> tuple:
        name = dotted(node.func)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "conjugate":
            return ("call", "conjg", (self.expr(node.func.value),))
        if isinstance(node.func, ast.Attribute) and node.func.attr == "rstrip":
            return ("call", "trim", (self.expr(node.func.value),))
        if name == "len" and isinstance(node.args[0], ast.Call) and isinstance(node.args[0].func, ast.Attribute) \
                and node.args[0].func.attr == "rstrip":
            return ("call", "len_trim", (self.expr(node.args[0].func.value),))
        if name == "math.copysign":
            a, b = self.expr(node.args[0]), self.expr(node.args[1])
            if a[0] == "num" and b[0] == "num":
                return num(type(a[1])(math.copysign(a[1], b[1])))
            return ("call", "sign", (a, b))
        args = [self.expr(a) for a in node.args if not isinstance(a, ast.Starred) and not _common_object(a)]
        if name in PY_OPERATORS and len(args) == 2:
            return fold(("bin", PY_OPERATORS[name], args[0], args[1]))
        if name in PY_TRANSPARENT and args:
            return args[0]
        if name in PY_CONVERSIONS and len(args) == 1:
            return fold(("conv", PY_CONVERSIONS[name], args[0]))
        if name == "complex" and len(args) == 2 and args[0][0] == "num" and args[1][0] == "num":
            return ("cnum", args[0][1], args[1][1], "c8" if isinstance(args[0][1], np.float64) else "c4")
        if name == "complex" and len(args) == 2:
            return ("call", "cmplx", tuple(args))
        if name == "_cfix" and len(args) == 2:
            return ("cfix", args[0], args[1])       # blank padding or truncation to a length
        if name == "len" and len(args) == 1:
            return ("call", "len", tuple(args))
        if name == "_ccmp" and len(args) == 2:
            return ("ccmp", args[0], args[1])
        if name in PY_CALLS:
            return fold_call(PY_CALLS[name], args)
        if name.startswith(("_", "np.", "math.")):
            raise Unchecked(f"python call not in the table: {name}")
        if name.endswith("_t"):
            return ("idx", name[:-2].lower(), tuple(args))      # a structure constructor reads like an array reference in the dump
        return ("call", name.lower(), tuple(args))


def _common_object(node: ast.AST) -> bool:
    """The COMMON block object passed along to a procedure: it is how the storage travels, not an argument of the Fortran."""
    return isinstance(node, ast.Name) and node.id.endswith("_common")


def _plus(node: tuple, amount: Any) -> tuple:
    """node + amount, simplifying `e - k + k`, which is every shifted subscript."""
    if not isinstance(amount, int):
        return ("bin", "+", node, amount)
    if node[0] == "num":
        return num(node[1] + amount)
    if node[0] == "bin" and node[1] in "+-" and node[3][0] == "num":
        shift = node[3][1] if node[1] == "+" else -node[3][1]
        total = shift + amount
        if total == 0:
            return node[2]
        return ("bin", "+" if total > 0 else "-", node[2], num(abs(total)))
    if amount == 0:
        return node
    return ("bin", "+" if amount > 0 else "-", node, num(abs(amount)))


def python_events(fn: ast.FunctionDef, proc: Proc, classes: Optional[Dict[str, List[str]]] = None) -> List[tuple]:
    reader = PythonReader(proc, classes or {})
    events: List[tuple] = []
    body = [stmt for stmt in fn.body if not _doc(stmt)]
    final = body.pop() if body and isinstance(body[-1], ast.Return) else None     # the end of the procedure, not RETURN
    machine = next((stmt for stmt in body if isinstance(stmt, ast.While) and isinstance(stmt.test, ast.Constant)), None)
    if machine is None:
        _walk(body, reader, events, {})
    else:
        at = body.index(machine)
        _walk(body[:at - 1], reader, events, {})
        _machine(machine, body[at - 1], reader, events)
    events.extend(("unknown", problem) for problem in (_declaration_problems(body, proc, reader) + _result_problems(fn, final, proc)
                                                       + _equivalence_problems(body, proc, reader)))
    return events


_DTYPES = {"r8": "np.float64", "r4": "np.float32", "i4": "np.int64", "i8": "np.int64", "c8": "np.complex128", "c4": "np.complex64",
           "l4": "np.bool_"}


def _declaration_problems(body: Sequence[ast.stmt], proc: Proc, reader: PythonReader) -> List[str]:
    """Every local array of known shape is declared with the Fortran extents, its kind, and column order above rank 1.
    Every PARAMETER used is defined: its uses resolve to the Fortran value, so a missing definition would not show otherwise."""
    declared = {stmt.targets[0].id.lower(): stmt.value for stmt in body
                if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name) and _declaration(stmt)}
    defined = {stmt.targets[0].id.lower().rstrip("_") for stmt in body
               if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name)}
    problems = [f"PARAMETER {name} is used but not defined" for name in sorted(reader.used_parameters)
                if name.rstrip("_") not in defined and name not in proc.module_level]
    for name, sym in proc.symbols.items():
        if (not sym.upper or sym.dummy or sym.parameter or sym.shape_kind != "EXPLICIT" or sym.common or sym.saved or sym.equivalenced
                or proc.equivalences or any(name in fields for fields in reader.classes.values())):
            continue
        call = declared.get(name) or declared.get(name + "_")
        if isinstance(call, ast.ListComp):              # [t() for _ in range(extent)]: an array of a derived type
            extent = reader.expr(call.generators[0].iter.args[0])
            if len(sym.upper) != 1 or not Matcher(proc).tree(fold(_extent(sym.lower[0], sym.upper[0])), extent):
                problems.append(f"array {name}: {ast.unparse(call)} is not its extent")
            continue
        if call is None:
            problems.append(f"array {name} is not declared")
            continue
        if not call.args:
            problems.append(f"array {name}: {ast.unparse(call)}")
            continue
        extents = call.args[0].elts if isinstance(call.args[0], ast.Tuple) else [call.args[0]]
        expected = [fold(_extent(lo, hi)) for lo, hi in zip(sym.lower, sym.upper)]
        got = [reader.expr(e) for e in extents]
        keywords = {k.arg: k.value for k in call.keywords}
        if len(expected) != len(got) or not all(Matcher(proc).tree(a, b) for a, b in zip(expected, got)):
            problems.append(f"array {name}: extents {ast.unparse(call)} are not the Fortran ones")
        if sym.kind in _DTYPES and dotted(keywords.get("dtype", ast.Name("?"))) != _DTYPES[sym.kind]:
            problems.append(f"array {name}: dtype in {ast.unparse(call)} is not kind {sym.kind}")
        if len(expected) > 1 and getattr(keywords.get("order"), "value", None) != "F":
            problems.append(f"array {name}: rank {len(expected)} without column order")
    return problems


def _equivalence_problems(body: Sequence[ast.stmt], proc: Proc, reader: PythonReader) -> List[str]:
    """Every array laid over shared storage starts at its Fortran position, with its Fortran extents; the storage holds them all."""
    if not proc.equivalences:
        return []
    layout, problems, storage = equivalence_layout(proc), [], {}
    arrays = {name: sym for name, sym in proc.symbols.items() if name in layout and sym.upper}
    for stmt in body:
        if not isinstance(stmt, ast.Assign) or not isinstance(stmt.targets[0], ast.Name):
            continue
        name = stmt.targets[0].id.lower()
        if name.startswith("_eq") and _declaration(stmt):
            storage[name] = reader.expr(stmt.value.args[0])
        if not _equivalence_view(stmt):
            continue
        view = stmt.value.func.value                        # _eq0[start:stop] of _eq0[start:stop].reshape(shape)
        start, stop = reader.expr(view.slice.lower), reader.expr(view.slice.upper)
        sym = arrays.pop(name, None)
        if sym is None or start[0] != "num" or stop[0] != "num":
            problems.append(f"view {name}: {ast.unparse(stmt.value)}")
            continue
        extents = [fold(_extent(lo, hi))[1] for lo, hi in zip(sym.lower, sym.upper)]
        shape = stmt.value.args[0]
        got = [reader.expr(e)[1] for e in (shape.elts if isinstance(shape, ast.Tuple) else [shape])]
        if start[1] != layout[name][1] or stop[1] - start[1] != int(np.prod(extents)) or got != extents:
            problems.append(f"view {name} is not at position {layout[name][1]} with extents {extents}: {ast.unparse(stmt.value)}")
        last = storage.get(ast.unparse(view.value).lower())
        if last is None or last[0] != "num" or last[1] < stop[1]:
            problems.append(f"view {name} runs past its storage: {ast.unparse(stmt.value)}")
    problems += [f"equivalenced array {name} has no view" for name in arrays]
    return problems


def _extent(lo: Any, hi: tuple) -> tuple:
    if isinstance(lo, int):
        return _plus(hi, 1 - lo)
    return ("bin", "+", ("bin", "-", hi, lo), num(1))


def _result_problems(fn: ast.FunctionDef, final: Optional[ast.Return], proc: Proc) -> List[str]:
    """What the procedure hands back: a function its result; a subroutine each scalar dummy it assigns."""
    returned = final.value if final is not None else None
    names = [e for e in returned.elts] if isinstance(returned, ast.Tuple) else ([returned] if returned is not None else [])
    returned_names = {n.id.lower() for n in names if isinstance(n, ast.Name)}
    problems = []
    result = {proc.name.lower(), proc.name.lower() + "_result", proc.name.lower() + "_"}
    assigned = {t.id.lower() for stmt in ast.walk(fn) if isinstance(stmt, ast.Assign) for t in stmt.targets if isinstance(t, ast.Name)}
    if result & assigned and not result & returned_names:
        return [f"function {proc.name} does not return its result"]
    declared_global = {n.lower() for stmt in ast.walk(fn) if isinstance(stmt, ast.Global) for n in stmt.names}
    # without `global` the assignment would make a new local and leave the module variable as it was
    problems += [f"module variable {name} is assigned without global" for name in sorted(assigned)
                 if name in proc.module_level and name not in declared_global]
    for event in fortran_events(proc):
        if event[0] == "assign" and event[1][0] == "var":
            sym = proc.symbols.get(event[1][1])
            if sym is not None and sym.dummy and not sym.upper and sym.kind not in ("dt", "derived") and event[1][1] not in returned_names:
                problems.append(f"scalar dummy {event[1][1]} is assigned but not returned")
    return sorted(set(problems))


def _python_allocation(stmt: ast.Assign, reader: PythonReader) -> Optional[tuple]:
    """Storage for an ALLOCATABLE or POINTER is a statement of the program; for anything else it is a declaration."""
    target = stmt.targets[0]
    if not isinstance(target, ast.Name):
        return None
    sym = reader.proc.symbols.get(target.id.rstrip("_").lower())
    if sym is None or not (sym.pointer or sym.allocatable):
        return None
    call = stmt.value
    if not dotted(call.func).startswith("np."):
        return ("alloc", ("var", target.id.lower()), ())
    extents = call.args[0].elts if isinstance(call.args[0], ast.Tuple) else [call.args[0]]
    return ("alloc", ("var", target.id.lower()), tuple(reader.expr(e) for e in extents))


def _parameter_of(stmt: ast.Assign, reader: PythonReader) -> Optional[Sym]:
    """The PARAMETER this statement defines. Uses resolve to the Fortran value, so the definition has to be checked here."""
    target = stmt.targets[0]
    if isinstance(target, ast.Name):
        name = target.id.rstrip("_").lower()
        sym = reader.proc.symbols.get(name) or reader.proc.host_symbols.get(name)
        if sym is not None and sym.parameter:
            return sym
    return None


def _machine(machine: ast.While, start: ast.stmt, reader: PythonReader, events: List[tuple]) -> None:
    """while True: if _pc == 0: ... if _pc == 1: ... break. Blocks are numbered in order from 0, where the counter starts. A block
    leaves by a jump or by break; falling out of the last one is the loop's final break."""
    if ast.unparse(start) != "_pc = 0":
        events.append(("unknown", ast.unparse(start)))
    reader.machine = True
    blocks = machine.body[:-1]
    if not (machine.body and isinstance(machine.body[-1], ast.Break)):
        events.append(("unknown", "the machine does not end with break"))
    for number, block in enumerate(blocks):
        if not (isinstance(block, ast.If) and ast.unparse(block.test) == f"_pc == {number}" and not block.orelse):
            events.append(("unknown", f"block {number}: {ast.unparse(block)[:60]}"))
            continue
        if not _leaves(block.body[-1]):
            events.append(("unknown", f"block {number} has no way out"))
        events.append(("label", f"pc{number}"))
        reader.next_block = number + 1
        reader.block_end = True
        _walk(block.body, reader, events, {})
    events.append(("halt",))


def _leaves(stmt: ast.stmt) -> bool:
    """Control cannot run past this statement of a block: otherwise the stale counter would select no block and the machine ends."""
    if isinstance(stmt, (ast.Break, ast.Continue)):
        return True
    if isinstance(stmt, ast.If):
        return bool(stmt.orelse) and _leaves(stmt.body[-1]) and _leaves(stmt.orelse[-1])
    return isinstance(stmt, ast.Assign) and dotted(stmt.targets[0]) == "_pc"


def _doc(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _declaration(stmt: ast.stmt) -> bool:
    """Storage for a declared variable, or the COMMON block object, not a statement of the program."""
    if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.ListComp) and isinstance(stmt.value.elt, ast.Call):
        return dotted(stmt.value.elt.func).endswith("_t")       # an array of a derived type
    if not (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)):
        return False
    name = dotted(stmt.value.func)
    return name in ("np.zeros", "np.full", "np.empty") or name.endswith("Block") or name.endswith("_t")


def _equivalence_view(stmt: ast.stmt) -> bool:
    """a = _eq0[0:4].reshape(...): an array laid over the shared storage (checked by _equivalence_problems)."""
    if not (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute)):
        return False
    return stmt.value.func.attr == "reshape" and dotted(getattr(stmt.value.func.value, "value", ast.Name("?"))).startswith("_eq")


def _null_pointer(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) and stmt.value.value is None


def _walk(body: Sequence[ast.stmt], reader: PythonReader, events: List[tuple], ranges: Dict[str, ast.AST]) -> None:
    resume = 0
    for position, stmt in enumerate(body):
        if _doc(stmt) or isinstance(stmt, (ast.Global, ast.Nonlocal, ast.Pass)) or _equivalence_view(stmt):
            continue
        if position < resume:
            continue
        if reader.machine and _machine_statement(stmt, body, position, reader, events):
            continue
        if _read_start(stmt):
            event, resume = _read_statement(body, position, reader)
            events.append(event)
            continue
        statement = _file_statement(stmt, reader)
        if statement:
            events.append(statement)
            continue
        if _null_pointer(stmt):
            if events:                      # ahead of the first statement it only says the pointer starts disassociated
                events.append(("free", reader.expr(stmt.targets[0])))
            continue
        if _declaration(stmt):
            allocation = _python_allocation(stmt, reader)
            if allocation:
                events.append(allocation)
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and dotted(stmt.value.func) == "_dassign":
            events.append(("assign", reader.expr(stmt.value.args[0]), reader.expr(stmt.value.args[1])))
            continue
        if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id.startswith("_do"):
            ranges[stmt.targets[0].id] = stmt.value
        elif isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Tuple) and dotted(stmt.value.func) == "_fdo":
            ranges[stmt.targets[0].elts[0].id] = stmt.value
        elif isinstance(stmt, ast.Assign) and _parameter_of(stmt, reader):
            sym = _parameter_of(stmt, reader)
            if not Matcher(reader.proc).tree(sym.value, reader.expr(stmt.value)):
                events.append(("unknown", f"PARAMETER value differs: {ast.unparse(stmt)}"))
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            events.append(("assign", reader.expr(stmt.targets[0]), reader.expr(stmt.value)))
        elif isinstance(stmt, ast.Expr) and _formatted(stmt):
            call = stmt.value.args[0]
            items = [e.value if isinstance(e, ast.Starred) else e for e in call.args[1].elts]
            events.append(("write", reader.expr(call.args[0]), tuple(reader.expr(e) for e in items)))
        elif isinstance(stmt, ast.AugAssign) and type(stmt.op) in _BIN:
            target = reader.expr(stmt.target)
            events.append(("assign", target, fold(("bin", _BIN[type(stmt.op)], target, reader.expr(stmt.value)))))
        elif isinstance(stmt, ast.For):
            problem = _loop_form(stmt, body[position - 1] if position else None)
            if problem:
                events.append(("unknown", problem))
            events.append(_loop(stmt, reader, ranges))
            reader.loop_depth += 1
            _walk(stmt.body, reader, events, ranges)
            reader.loop_depth -= 1
            events.append(("end",))
        elif isinstance(stmt, ast.Try):
            events.append(("unknown", ast.unparse(stmt)))
        elif isinstance(stmt, ast.While):
            events.append(("while", reader.expr(stmt.test)))
            reader.loop_depth += 1
            _walk(stmt.body, reader, events, ranges)
            reader.loop_depth -= 1
            events.append(("end",))
        elif isinstance(stmt, ast.If):
            at_end, reader.block_end = reader.block_end, reader.block_end and position == len(body) - 1
            _branch(stmt, reader, events, ranges, "if")
            reader.block_end = at_end
            events.append(("end",))
        elif isinstance(stmt, ast.Break):
            events.append(("exit",))
        elif isinstance(stmt, ast.Continue):
            events.append(("cycle",))
        elif isinstance(stmt, ast.Return):
            events.append(("return",))
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            name = dotted(stmt.value.func)
            if name == "print":
                items = [reader.expr(a.value if isinstance(a, ast.Starred) else a) for a in stmt.value.args]
                events.append(("write", tuple(items)))
            elif name.startswith(("_", "np.", "math.")):
                events.append(("unknown", ast.unparse(stmt)))
            else:
                events.append(("call", name.lower(), tuple(reader.expr(a) for a in stmt.value.args if not _common_object(a))))
        else:
            events.append(("unknown", ast.unparse(stmt)))


def _read_start(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call) and dotted(stmt.value.func) == "_freadstart"


_READ_KINDS = {"i": "i", "r": "r", "l": "l", "a": "c"}


def _read_target(guard: ast.If, kind: str, width: Optional[int], reader: PythonReader) -> tuple:
    """`if _v is not None: x = float(_v)`: the variable read; the conversion has to give its kind."""
    store = guard.body[0]
    target = reader.expr(store.targets[0])
    wanted = kind_of(target, reader.proc)
    sym = reader.proc.symbols.get(target[1]) if target[0] in ("var", "idx") else None
    if sym is not None and sym.length is not None:
        stored = reader.expr(store.value)
        if stored[0] != "cfix" or stored[2] != num(sym.length) or width != sym.length:
            raise Unchecked(f"read of {ast.unparse(store.targets[0])} is not of length {sym.length}")
    convert = dotted(store.value.func) if isinstance(store.value, ast.Call) else ""
    if wanted in ("r4", "r8") and convert != {"r4": "np.float32", "r8": "float"}[wanted]:
        raise Unchecked(f"read of {ast.unparse(store.targets[0])} is not converted to kind {wanted}")
    if wanted and wanted[0] in "ilr" and _READ_KINDS[kind] != wanted[0]:
        raise Unchecked(f"read of {ast.unparse(store.targets[0])} with get({kind!r})")
    return target


def _read_items(body: Sequence[ast.stmt], reader: PythonReader, name: str) -> List[tuple]:
    items: List[tuple] = []
    k = 0
    while k < len(body):
        stmt = body[k]
        k += 1
        if isinstance(stmt, ast.For):
            start = stmt.iter.args
            bounds = [reader.expr(a) for a in start]
            first, stop = (num(0), bounds[0]) if len(bounds) == 1 else (bounds[0], bounds[1])
            step = bounds[2] if len(bounds) == 3 else num(1)
            items.append(("impdo", tuple(_read_items(stmt.body, reader, name)), reader.expr(stmt.target), first, _plus(stop, -1), step))
        elif isinstance(stmt, ast.Assign) and dotted(stmt.value.func if isinstance(stmt.value, ast.Call) else ast.Name("?")) == name + ".get":
            arguments = [a.value for a in stmt.value.args]
            items.append(_read_target(body[k], arguments[0], arguments[1] if len(arguments) > 1 else None, reader))
            k += 1
        elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.ListComp):     # a whole array, in column order
            items.append(reader.expr(body[k].body[0].targets[0]))
            k += 1
        else:
            raise Unchecked(f"python statement in a READ: {ast.unparse(stmt)}")
    return items


def _read_statement(body: Sequence[ast.stmt], position: int, reader: PythonReader) -> Tuple[tuple, int]:
    """_rd = _freadstart(unit, format, die) ... _rd.finish() as one READ. Returns (event, index after it)."""
    start = body[position]
    name = start.targets[0].id
    end = position + 1
    while end < len(body):
        value = body[end].value if isinstance(body[end], (ast.Assign, ast.Expr)) else None
        if isinstance(value, ast.Call) and dotted(value.func) == name + ".finish":
            break
        end += 1
    if end == len(body):
        raise Unchecked(f"READ {name} has no finish()")
    unit, fmt, die = start.value.args
    finish = body[end]
    iostat = finish.targets[0] if isinstance(finish, ast.Assign) else None
    iostat_tree = None if iostat is None or dotted(iostat) == "_ios" else reader.expr(iostat)
    format_tree = None if isinstance(fmt, ast.Constant) and fmt.value is None else reader.expr(fmt)
    return (("read", reader.expr(unit), format_tree, tuple(_read_items(body[position + 1:end], reader, name)), iostat_tree,
             not die.value), end + 1)


def _file_statement(stmt: ast.stmt, reader: PythonReader) -> Optional[tuple]:
    value = stmt.value if isinstance(stmt, (ast.Assign, ast.Expr)) else None
    if not isinstance(value, ast.Call):
        return None
    name = dotted(value.func)
    target = stmt.targets[0] if isinstance(stmt, ast.Assign) else None
    iostat = None if target is None or dotted(target) == "_ios" else reader.expr(target)
    if name in ("_fopen", "_fclose"):
        keywords = tuple(sorted((k.arg, reader.expr(k.value)) for k in value.keywords))
        return ("open" if name == "_fopen" else "close", reader.expr(value.args[0]), keywords, iostat)
    if name == "_frewind":
        return ("rewind", reader.expr(value.args[0]))
    if name == "_fexists":
        return ("inquire", reader.expr(value.args[0]), iostat)
    if name == "_fwriteunit":
        inner = value.args[1]
        items = inner.args[-1]
        fmt = reader.expr(inner.args[0]) if dotted(inner.func) == "_fwrite" else None
        return ("fwrite", reader.expr(value.args[0]), fmt, _io_items(items, reader), iostat)
    return None


def _io_items(items: ast.List, reader: PythonReader) -> tuple:
    out: List[tuple] = []
    for element in items.elts:
        inner = reader.expr(element.value if isinstance(element, ast.Starred) else element)
        out.extend(inner[1] if inner[0] == "ctor" else (inner,))
    return tuple(out)


def _machine_statement(stmt: ast.stmt, body: Sequence[ast.stmt], position: int, reader: PythonReader, events: List[tuple]) -> bool:
    """The state machine's own statements (see the note at lower()). True when `stmt` was one of them. Each has one exact form:
    a program counter that starts elsewhere, or a loop counter that steps by two, is a different program."""
    if isinstance(stmt, ast.Continue):
        if reader.loop_depth:
            return False
        before = body[position - 1] if position else None
        if not (isinstance(before, ast.Assign) and dotted(before.targets[0]) == "_pc"):
            events.append(("unknown", "continue with no jump before it"))
        return True
    if isinstance(stmt, ast.Break) and reader.loop_depth == 0:
        events.append(("halt",))
        return True
    if isinstance(stmt, ast.AugAssign) and dotted(stmt.target).startswith("_doG"):
        if ast.unparse(stmt) != f"{dotted(stmt.target)} += 1":
            events.append(("unknown", ast.unparse(stmt)))
        events.append(("doincr",))
        return True
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return False
    target, value = stmt.targets[0], stmt.value
    if dotted(target) == "_pc":
        last = position == len(body) - 1
        follows = position + 1 < len(body) and isinstance(body[position + 1], ast.Continue)
        at_block_end = last and reader.block_end and value.value == reader.next_block
        if not (follows or at_block_end):
            events.append(("unknown", f"jump to block {value.value} is not followed by continue"))
        events.append(("jump", f"pc{value.value}"))
        return True
    name = dotted(target)
    if name.startswith("_doG"):
        if isinstance(value, ast.Call):
            reader.machine_ranges[name] = value
        elif ast.unparse(stmt) == f"{name} = 0":
            reader.machine_counters.add(name)
        else:
            events.append(("unknown", ast.unparse(stmt)))
        return True
    text = ast.unparse(value)
    match = re.fullmatch(r"(_doG\d+)\.start", text)
    if match:
        if match.group(1) + "_i" not in reader.machine_counters:
            events.append(("unknown", f"counter of {match.group(1)} is not set to 0"))
        loop = _loop(ast.For(target=target, iter=ast.Name(match.group(1)), body=[], orelse=[]), reader, reader.machine_ranges)
        events.append(("doinit",) + loop[1:])
        return True
    match = re.fullmatch(r"(_doG\d+)\[\1_i\]", text)
    if match:
        events.append(("dobody", reader.expr(target)))     # the loop variable takes the next value
        return True
    match = re.fullmatch(r"(_doG\d+)\.start \+ len\(\1\) \* \1\.step", text)
    if match:
        events.append(("dofinal", reader.expr(target)))
        return True
    return False


def _loop_form(stmt: ast.For, before: Optional[ast.stmt]) -> Optional[str]:
    """A DO evaluates its bounds once, on entry, and leaves its variable one step past the last value (F2003 8.1.6.6):
    the bounds statement stands right before the loop, and the else clause sets exactly that value."""
    if not isinstance(stmt.iter, ast.Name):
        return None
    name = stmt.iter.id
    source = before.targets[0] if isinstance(before, ast.Assign) else None
    bound = {source.id} if isinstance(source, ast.Name) else {e.id for e in source.elts} if isinstance(source, ast.Tuple) else set()
    if name not in bound:
        return f"the bounds of loop {name} are not evaluated right before it"
    target = ast.unparse(stmt.target)
    expected = (f"{target} = {name[:-len('_values')]}_after" if name.endswith("_values")
                else f"{target} = {name}.start + len({name}) * {name}.step")
    if len(stmt.orelse) != 1 or ast.unparse(stmt.orelse[0]) != expected:
        return f"loop {name}: its variable is not left at the DO's final value"
    return None


def _formatted(stmt: ast.Expr) -> bool:
    """print(_fwrite(format, [items]), end="")"""
    value = stmt.value
    return (isinstance(value, ast.Call) and dotted(value.func) == "print" and len(value.args) == 1
            and isinstance(value.args[0], ast.Call) and dotted(value.args[0].func) == "_fwrite")


def _branch(stmt: ast.If, reader, events, ranges, kind: str) -> None:
    events.append((kind, reader.expr(stmt.test)))
    _walk(stmt.body, reader, events, ranges)
    orelse = stmt.orelse
    if len(orelse) == 1 and isinstance(orelse[0], ast.If):
        _branch(orelse[0], reader, events, ranges, "elif")
    elif orelse:
        events.append(("else",))
        _walk(orelse, reader, events, ranges)


def _loop(stmt: ast.For, reader: PythonReader, ranges: Dict[str, ast.AST]) -> tuple:
    source = stmt.iter
    if isinstance(source, ast.Name) and source.id in ranges:
        source = ranges[source.id]
    if isinstance(source, ast.Call) and dotted(source.func) == "_fdo":
        return ("do", reader.expr(stmt.target), *[reader.expr(a) for a in source.args])
    if not (isinstance(source, ast.Call) and dotted(source.func) == "range"):
        raise Unchecked(f"python loop not in the table: {ast.unparse(stmt.iter)}")
    args = [reader.expr(a) for a in source.args]
    start, stop = (num(0), args[0]) if len(args) == 1 else (args[0], args[1])
    step = args[2] if len(args) == 3 else num(1)
    if step[0] != "num":
        raise Unchecked(f"python loop step: {ast.unparse(source)}")
    return ("do", reader.expr(stmt.target), start, _plus(stop, -1 if step[1] > 0 else 1), step)


# --------------------------------------------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------------------------------------------

def kind_of(node: Optional[tuple], proc: Proc) -> Optional[str]:
    """The Fortran kind of a tree, or None when it cannot be told."""
    if node is None:
        return None
    tag = node[0]
    if tag == "num":
        value = node[1]
        return "r4" if isinstance(value, np.float32) else "r8" if isinstance(value, np.floating) else "i4"
    if tag == "cnum":
        return node[3]
    if tag in ("var", "idx"):
        sym = proc.symbols.get(node[1]) or proc.host_symbols.get(node[1])
        return sym.kind if sym else None
    if tag == "conv":
        return node[1]
    if tag == "un":
        return kind_of(node[2], proc)
    if tag == "bin":
        if node[1] in ("+", "-", "*", "/", "**"):
            left, right = kind_of(node[2], proc), kind_of(node[3], proc)
            if node[1] == "**" and right and right[0] == "i":
                return left
            if left is None or right is None:
                return None
            return max(left, right, key=lambda k: ("ilrc".index(k[0]), int(k[1:] or 0)))
        return "l4"
    if tag == "call":
        if node[1] in ("floor", "ceiling", "nint", "size", "len", "len_trim"):
            return "i4"
        if node[1] in ("sqrt", "exp", "log", "log10", "sin", "cos", "tan", "abs", "aint", "mod", "modulo", "sum", "product"):
            return kind_of(node[2][0], proc) if node[2] else None
        sym = proc.symbols.get(node[1])
        return sym.kind if sym else None
    return None


def _sequence_offset(element: tuple, proc: Proc) -> Optional[int]:
    """The element's place in column order, or None when a subscript is not a constant."""
    sym = proc.symbols.get(element[1])
    if sym is None or not sym.upper or len(sym.upper) != len(element[2]):
        return None
    offset, stride = 0, 1
    for sub, low, high in zip(element[2], sym.lower, sym.upper):
        if sub[0] != "num" or high is None or high[0] != "num" or not isinstance(low, int):
            return None
        offset += (sub[1] - low) * stride
        stride *= high[1] - low + 1
    return offset


def _rename(node: Any, names: Dict[str, str]) -> Any:
    if isinstance(node, tuple):
        if len(node) == 2 and node[0] == "var" and node[1] in names:
            return ("var", names[node[1]])
        return tuple(_rename(item, names) for item in node)
    return node


def equivalence_layout(proc: Proc) -> Dict[str, Tuple[int, int]]:
    """variable -> (group, element position in the shared storage), from the EQUIVALENCE lines. Raises for mixed kinds: a position
    counted in elements only means something within one kind."""
    position: Dict[str, Tuple[int, int]] = {}
    group_of: Dict[str, int] = {}

    def offset_of(item: tuple) -> Tuple[str, int]:
        if item[0] == "var":
            return item[1], 0
        offset = _sequence_offset(item, proc)
        if offset is None:
            raise Unchecked(f"EQUIVALENCE object {item}")
        return item[1], offset

    next_group = 0
    for line in proc.equivalences:
        names = [offset_of(item) for item in line]
        kinds = {proc.symbols[name].kind for name, _ in names}
        if len(kinds) != 1:
            raise Unchecked(f"EQUIVALENCE of mixed kinds {sorted(kinds)}")
        known = next(((name, off) for name, off in names if name in group_of), None)
        if known is None:
            group, anchor = next_group, names[0][1]
            next_group += 1
            group_of[names[0][0]] = group
            position[names[0][0]] = (group, 0)
            known = names[0]
        group = group_of[known[0]]
        base = position[known[0]][1] + known[1]
        for name, off in names:
            if name in group_of and position[name][1] + off != base:
                raise Unchecked(f"EQUIVALENCE of {name} conflicts")
            group_of[name] = group
            position[name] = (group, base - off)
    lowest: Dict[int, int] = {}
    for group, at in position.values():
        lowest[group] = min(lowest.get(group, at), at)
    return {name: (group, at - lowest[group]) for name, (group, at) in position.items()}


def same_expr(a: tuple, b: tuple) -> bool:
    """Two trees of the same language."""
    return repr(a) == repr(b)


def _linear(node: tuple) -> Optional[Dict[str, int]]:
    """An integer index or extent as {atom: coefficient, "": constant}, so that 1 + k and k + 1 are one value. None if not linear.
    Indices are exact integers here: their order of evaluation is not part of an equation."""
    if node[0] == "num" and isinstance(node[1], (int, np.integer)):
        return {"": int(node[1])}
    if node[0] == "conv" and node[1] in ("i4", "i8"):
        return _linear(node[2])
    if node[0] == "bin" and node[1] in ("+", "-"):
        left, right = _linear(node[2]), _linear(node[3])
        if left is None or right is None:
            return None
        sign = 1 if node[1] == "+" else -1
        out = dict(left)
        for key, value in right.items():
            out[key] = out.get(key, 0) + sign * value
        return {k: v for k, v in out.items() if v or k == ""}
    if node[0] in ("var", "call", "idx"):
        return {repr(node): 1, "": 0}
    return None


def same_index(a: tuple, b: tuple, matcher: "Matcher") -> bool:
    left, right = _linear(a), _linear(b)
    if left is None or right is None:
        return matcher.tree(a, b)
    left, right = {k: v for k, v in left.items() if v}, {k: v for k, v in right.items() if v}
    if left == right:
        return True
    if set(k for k in left if k) == set(k for k in right if k):
        return left == right
    return False


def _format_text(node: tuple) -> Optional[str]:
    return re.sub(r"\s+", "", node[1]).upper() if node[0] == "str" else None


class Matcher:
    """Tree comparison for one procedure. Names must map one to one: two Fortran variables cannot share a translated name."""

    def __init__(self, proc: Proc):
        self.proc = proc
        self.to_python: Dict[str, str] = {}
        self.to_fortran: Dict[str, str] = {}
        self.layout = equivalence_layout(proc) if proc.equivalences else {}

    def storage(self, name: str, python: str, offset: int) -> bool:
        """The one Fortran variable `name` is element `offset` of the shared array `python`."""
        group, at = self.layout[name]
        return at == offset and self.to_python.setdefault(f"#{group}", python) == python

    def name(self, fortran: str, python: str) -> bool:
        if not same_name(fortran, python):
            return False
        if self.to_python.setdefault(fortran, python) != python:
            return False
        return self.to_fortran.setdefault(python, fortran) == fortran

    def tree(self, f: Any, p: Any, written: bool = False) -> bool:
        if f is None or p is None:
            return f is p
        if not isinstance(f, tuple) or not isinstance(p, tuple):
            return f == p
        if not f or not p:
            return f == p
        if f[0] == "num" and p[0] == "num":
            return same_number(f[1], p[1])
        if p[0] == "arrayvalue":
            return self.loops_cover(f, p[1]) and self.element(f, p[2], p[1], written)
        if f[0] == "cnum" and p[0] == "cnum":
            return same_number(f[1], p[1]) and same_number(f[2], p[2])
        if f[:2] == ("call", "cmplx") and p[0] == "conv" and p[2][:2] == ("call", "cmplx"):
            # single-precision parts travel through Python's double-precision complex() and back, exactly
            return len(f[2]) == len(p[2][2]) and all(self.widened(a, b) for a, b in zip(f[2], p[2][2]))
        if f[0] == "conv" and p[0] != "conv":
            # Python converts an integer operand of a double expression exactly as Fortran does; nothing else is implicit
            source = kind_of(f[2], self.proc)
            return bool(source and source[0] == "i" and f[1] == "r8") and self.tree(f[2], p, written)
        if p[0] == "conv" and f[0] != "conv":
            # a conversion to the kind the value has already, or one made only so print shows it, changes no number
            return (written or kind_of(f, self.proc) == p[1]) and self.tree(f, p[2])
        if f[:2] == ("bin", "**") and f[3] == ("num", 2) and p[:2] == ("bin", "*"):
            return self.tree(f[2], p[2], written) and self.tree(f[2], p[3], written)    # x**2 is the one multiplication x*x
        if f[:2] == ("bin", "**") and f[3] == ("num", 3) and p[:2] == ("bin", "*") and p[2][:2] == ("bin", "*"):
            # x**3 is x*x, then x: the same two multiplications in libgcc's powi, whichever operand it writes first
            return all(self.tree(f[2], q, written) for q in (p[2][2], p[2][3], p[3]))
        if f[:2] == ("call", "size") and p[:2] == ("call", "size") and len(f[2]) == 1 and len(p[2]) == 2 and p[2][1] == num(1):
            sym = self.proc.symbols.get(f[2][0][1]) if f[2][0][0] == "var" else None
            return sym is not None and len(sym.upper) == 1 and self.tree(f[2][0], p[2][0])     # SIZE of a rank-1 array
        if f[:2] == ("call", "dim") and p[:2] == ("call", "max") and len(p[2]) == 2 and p[2][0][0] == "num" and p[2][0][1] == 0 and p[2][1][:2] == ("bin", "-"):
            return self.tree(f[2][0], p[2][1][2], written) and self.tree(f[2][1], p[2][1][3], written)      # DIM(a, b) is max(0, a - b)
        if f[0] == "num" and p[:2] == ("call", "len") and p[2][0][0] == "var":
            sym = self.proc.symbols.get(p[2][0][1])
            return sym is not None and sym.length == f[1]       # gfortran folds LEN of a declared length
        if p[0] == "seq" and f[0] == "idx":
            return self.name(f[1], p[1][1]) and p[2][0] == "num" and _sequence_offset(f, self.proc) == p[2][1]
        if f[:2] == ("bin", "//") and p[:2] == ("bin", "+"):
            return self.tree(f[2], p[2], written) and self.tree(f[3], p[3], written)    # character concatenation
        if f[0] == "var" and p[0] == "idx" and f[1] in self.layout and p[1].startswith("_eq") and p[2][0][0] == "num":
            return self.storage(f[1], p[1], p[2][0][1] - 1)
        if f[0] == "var" and p[0] == "var":
            return self.name(f[1], p[1])
        if f[0] == "idx" and p[0] == "idx":
            return self.name(f[1], p[1]) and self.tree(f[2], p[2], written)
        return len(f) == len(p) and all(self.tree(a, b, written) for a, b in zip(f, p))

    def extents(self, f: tuple) -> Optional[List[tuple]]:
        """The element count of each free dimension of an array or section, or None for a scalar."""
        if f[0] == "var":
            sym = self.proc.symbols.get(f[1]) or self.proc.host_symbols.get(f[1])
            if sym is None or not sym.upper:
                return None
            if sym.shape_kind == "EXPLICIT":
                return [fold(_extent(lo, hi)) for lo, hi in zip(sym.lower, sym.upper)]
            return [("call", "size", (f, num(d + 1))) for d in range(len(sym.upper))]
        if f[0] == "idx":
            sym = self.proc.symbols.get(f[1]) or self.proc.host_symbols.get(f[1])
            out = []
            for d, sub in enumerate(f[2]):
                if sub == ("full",):
                    out.append(self.extents(("var", f[1]))[d])
                elif sub[0] == "sec":
                    if sub[3] is not None:
                        return None
                    low = sub[1] if sub[1] is not None else num(sym.lower[d])
                    high = sub[2] if sub[2] is not None else sym.upper[d]
                    out.append(fold(("bin", "+", ("bin", "-", high, low), num(1))))
            return out or None
        return None

    def shape(self, f: tuple) -> Optional[List[tuple]]:
        """The extents of an array expression: those of its first array operand."""
        direct = self.extents(f) if f[0] in ("var", "idx") else None
        if direct is not None:
            return direct
        for part in (f[1:] if f[0] != "call" else f[2]) if f else ():
            if isinstance(part, tuple) and part and isinstance(part[0], str):
                found = self.shape(part)
                if found is not None:
                    return found
        return None

    def element(self, f: tuple, p: tuple, loops: Sequence[Tuple[str, tuple]], written: bool = False) -> bool:
        """p is one element of the array expression f, with the array statement's loop indices in `loops`."""
        if p[0] == "elemof":
            return self.tree(f, p[1], written)
        if p[0] == "arrayvalue" and len(p[1]) == len(loops):
            renamed = {a[0]: b[0] for a, b in zip(p[1], loops)}
            return self.element(f, _rename(p[2], renamed), loops, written)
        if f[0] in ("var", "idx") and self.extents(f) is not None:
            if p[0] != "idx" or not self.name(f[1], p[1]):
                return self.tree(f, p, written)         # the whole value of a temporary
            sym = self.proc.symbols.get(f[1]) or self.proc.host_symbols.get(f[1])
            lower = self.proc.allocated_lower.get(f[1]) or sym.lower
            subs = f[2] if f[0] == "idx" else tuple(("full",) for _ in sym.upper)
            if len(subs) != len(p[2]):
                return False
            free = iter(loops)
            for d, (sub, got) in enumerate(zip(subs, p[2])):
                if sub == ("full",) or sub[0] == "sec":
                    low = num(lower[d]) if sub == ("full",) or sub[1] is None else sub[1]
                    index = ("var", next(free)[0])
                    if not same_index(("bin", "+", low, index), got, self):
                        return False
                elif not same_index(sub, got, self):
                    return False
            return True
        if f[0] == "bin" and p[0] == "bin" and f[1] == p[1]:
            return self.element(f[2], p[2], loops, written) and self.element(f[3], p[3], loops, written)
        if f[:2] == ("bin", "**") and f[3] == ("num", 2) and p[:2] == ("bin", "*"):
            return self.element(f[2], p[2], loops) and self.element(f[2], p[3], loops)
        if f[0] == "un" and p[0] == "un" and f[1] == p[1]:
            return self.element(f[2], p[2], loops, written)
        if f[0] == "conv" and p[0] == "conv" and f[1] == p[1]:
            return self.element(f[2], p[2], loops, written)
        if p[0] == "conv" and f[0] != "conv" and (written or kind_of(f, self.proc) == p[1]):
            return self.element(f, p[2], loops)
        if f[0] == "call" and p[0] == "call" and f[1] == p[1] and len(f[2]) == len(p[2]):
            return all(self.element(a, b, loops, written) for a, b in zip(f[2], p[2]))
        return self.tree(f, p, written)

    def loops_cover(self, f: tuple, loops: Sequence[Tuple[str, tuple]]) -> bool:
        wanted = self.shape(f)
        return wanted is not None and len(wanted) == len(loops) and all(same_index(a, b[1], self) for a, b in zip(wanted, loops))

    def widened(self, f: tuple, p: tuple) -> bool:
        while f[0] == "conv":
            f = f[2]
        while p[0] == "conv":
            p = p[2]
        if f[0] == "num" and p[0] == "num":
            return float(f[1]) == float(p[1])
        return self.tree(f, p)

    def event(self, f: tuple, p: tuple) -> bool:
        if f[0] == "call" and p[0] == "assign" and p[2][0] == "call":
            # a subroutine's output arguments come back as return values: each target must be one of the arguments
            targets = p[1][1] if p[1][0] == "ctor" else (p[1],)
            return (self.name(f[1], p[2][1]) and self.tree(f[2], p[2][2])
                    and all(any(self.tree(a, t) for a in f[2]) for t in targets))
        if f[0] == "write" and p[0] == "write" and len(f) == len(p):
            # print shows a single-precision value through float(); that applies to the item itself, never inside it
            if len(f[-1]) != len(p[-1]) or not all(self.item(a, b) for a, b in zip(f[-1], p[-1])):
                return False
            return len(f) == 2 or _format_text(f[1]) == _format_text(p[1])
        if f[0] == "assign" and p[0] == "assign" and p[2][0] == "cfix":
            return self.fixed_length(f[1], p[2][2]) and self.tree(f[:2], p[:2]) and self.tree(f[2], p[2][1])
        if f[0] == "assign" and p[0] == "assign":
            return self.tree(f, p) and self.stored_kind(f[1], p[2])
        if f[0] == "assign" and p[0] == "elem":
            return self.loops_cover(f[1], p[1]) and self.element(f[1], p[2], p[1]) and self.element(f[2], p[3], p[1])
        if f[0] == "ptr" and p[0] == "assign":
            return self.tree(f[1], p[1]) and self.tree(f[2], p[2])
        if f[0] == "free" and p[0] == "free":
            return self.tree(f[1], p[1])
        if f[0] == "do" and p[0] == "do":
            return self.tree(f, p) and self.stored_kind(f[1], p[2])
        if f[0] == "doinit" and p[0] == "doinit":
            return self.tree(f, p) and self.stored_kind(f[1], p[2])
        if f[0] in ("read", "fwrite") and p[0] == f[0]:
            same_format = _format_text(f[2]) == _format_text(p[2]) if f[2] and p[2] else f[2] is p[2]
            same_items = len(f[3]) == len(p[3]) and all(self.item(a, b) for a, b in zip(f[3], p[3]))
            return same_format and self.tree(f[1], p[1]) and same_items and self.tree(f[4:], p[4:])
        if f[0] == "call" and p[0] == "call" and any(a is not None and a[0] == "seqsub" for a in p[2]):
            return f[1] == p[1] and len(f[2]) == len(p[2]) and all(
                self.tree(a[2], b[2]) and self.name(a[1], b[1][1]) if b[0] == "seqsub" else self.tree(a, b) for a, b in zip(f[2], p[2]))
        return self.tree(f, p)

    def fixed_length(self, target: tuple, length: tuple) -> bool:
        """A character assignment pads or cuts to the length of its target."""
        sym = self.proc.symbols.get(target[1]) if target[0] in ("var", "idx") else None
        return sym is not None and length == num(sym.length)

    def item(self, f: tuple, p: tuple) -> bool:
        """One output item. An array can be written element by element."""
        if p[0] == "arrayvalue":
            return self.loops_cover(f, p[1]) and self.element(f, p[2], p[1], written=True)
        if f[0] == "impdo" and p[0] == "impdo" and len(f[1]) == len(p[1]):
            return all(self.item(a, b) for a, b in zip(f[1], p[1])) and self.tree(f[2:], p[2:])
        return self.tree(f, p, written=True)

    def stored_kind(self, target: tuple, value: tuple) -> bool:
        """A scalar has no dtype in Python: `x = 0.0` would make a single-precision x double from there on."""
        if target[0] != "var" or value[0] != "num" or isinstance(value[1], int):
            return True
        kind = kind_of(target, self.proc)
        return kind not in ("r4", "r8") or kind_of(value, self.proc) == kind


def same_name(fortran: str, python: str) -> bool:
    """The translation's naming: a keyword gets a trailing _, a function result _result, a module variable its module's name,
    a saved variable _save_<procedure>_."""
    if python in (fortran, fortran + "_", fortran + "_result"):
        return True
    return re.fullmatch(r"(?:_save_\w+_|[a-z]\w*_)" + re.escape(fortran), python) is not None


def _without_local_zeros(fortran: List[tuple], python: List[tuple], matcher: Matcher) -> List[tuple]:
    """A local that is first set by a call is given a zero in Python, so it exists to be passed. Reading it before that call
    is not conforming Fortran, so this initial zero can't change a result. It is skipped only ahead of the first statement."""
    k = 0
    while (k < len(python) and python[k][0] == "assign" and python[k][1][0] == "var" and python[k][2][0] == "num"
           and python[k][2][1] == 0 and (k >= len(fortran) or not Matcher(matcher.proc).event(fortran[0], python[k]))):
        k += 1
    return python[k:]


def lower(events: List[tuple]) -> Tuple[List[tuple], Dict[str, int]]:
    """Events to flat code with explicit branches: ("op", event), ("br", test, then, else), ("jmp", label), ("halt",).

    A translated GO TO cannot keep the Fortran's shape: Python has no jump, so the program becomes a machine of blocks chosen by a
    program counter, and DO becomes explicit steps. Lowering both programs to this form makes them the same kind of object, and
    lockstep() then compares every path. A DO is doinit, test, body, doincr, back to the test, then dofinal."""
    code: List[tuple] = []
    labels: Dict[str, int] = {}
    stack: List[Dict[str, Any]] = []
    fresh = iter(range(1 << 30))

    def new(prefix: str) -> str:
        return f".{prefix}{next(fresh)}"

    def mark(label: str) -> None:
        labels[label] = len(code)

    def loop() -> Dict[str, Any]:
        for entry in reversed(stack):
            if entry["kind"] in ("do", "while"):
                return entry
        raise Unchecked("EXIT or CYCLE outside a loop")

    for event in events:
        tag = event[0]
        if tag in ("if", "elif"):
            if tag == "if":
                stack.append({"kind": "if", "end": new("end"), "else": None})
            else:
                code.append(("jmp", stack[-1]["end"]))
                mark(stack[-1]["else"])
            entry = stack[-1]
            body, entry["else"] = new("then"), new("else")
            code.append(("br", event[1], body, entry["else"]))
            mark(body)
        elif tag == "else":
            code.append(("jmp", stack[-1]["end"]))
            mark(stack[-1]["else"])
            stack[-1]["else"] = None
        elif tag == "do":
            entry = {"kind": "do", "test": new("test"), "next": new("next"), "exit": new("exit"), "after": new("after"), "var": event[1]}
            stack.append(entry)
            code.append(("op", ("doinit",) + event[1:]))
            mark(entry["test"])
            body = new("body")
            code.append(("br", ("dotest",), body, entry["exit"]))
            mark(body)
            code.append(("op", ("dobody", event[1])))
        elif tag == "while":
            entry = {"kind": "while", "test": new("test"), "next": None, "after": new("after")}
            entry["next"] = entry["test"]
            stack.append(entry)
            mark(entry["test"])
            if event[1] != ("bool", True):
                body = new("body")
                code.append(("br", event[1], body, entry["after"]))
                mark(body)
        elif tag == "end":
            entry = stack.pop()
            if entry["kind"] == "if":
                if entry["else"]:
                    mark(entry["else"])
                mark(entry["end"])
            elif entry["kind"] == "do":
                mark(entry["next"])
                code.append(("op", ("doincr",)))
                code.append(("jmp", entry["test"]))
                mark(entry["exit"])
                code.append(("op", ("dofinal", entry["var"])))
                mark(entry["after"])
            else:
                code.append(("jmp", entry["test"]))
                mark(entry["after"])
        elif tag == "exit":
            code.append(("jmp", loop()["after"]))
        elif tag == "cycle":
            code.append(("jmp", loop()["next"]))
        elif tag == "label":
            mark(event[1])
        elif tag in ("goto", "jump"):
            code.append(("jmp", event[1]))
        elif tag in ("halt", "return"):
            code.append(("halt",))
        else:
            code.append(("op", event))
    code.append(("halt",))
    return code, labels


def lockstep(fortran: List[tuple], python: List[tuple], proc: Proc) -> List[str]:
    """Walk both flat programs together from the start: same operation at each step, same test at each branch, then both ways."""
    for side, events in (("fortran", fortran), ("python", python)):
        for event in events:
            if event[0] == "unknown":
                return [f"{side}: not established: {event[1]}"]
    f_code, f_labels = lower(fortran)
    p_code, p_labels = lower(python)
    matcher = Matcher(proc)

    def settle(code, labels, at: int) -> int:
        """Follow plain jumps; a loop made only of jumps is a program that hangs, which both sides must then do."""
        seen = set()
        while code[at][0] == "jmp" and at not in seen:
            seen.add(at)
            if code[at][1] not in labels:
                raise Unchecked(f"jump to an unknown label {code[at][1]}")
            at = labels[code[at][1]]
        return at

    pending, visited = [(0, 0)], set()
    while pending:
        f_at, p_at = pending.pop()
        f_at, p_at = settle(f_code, f_labels, f_at), settle(p_code, p_labels, p_at)
        if (f_at, p_at) in visited:
            continue
        visited.add((f_at, p_at))
        f, p = f_code[f_at], p_code[p_at]
        if f[0] != p[0]:
            return [f"step {f_at}/{p_at}:\n    fortran {show(f)}\n    python  {show(p)}"]
        if f[0] == "op":
            if f[1][0] == "unknown" or p[1][0] == "unknown" or not matcher.event(f[1], p[1]):
                return [f"step {f_at}/{p_at}:\n    fortran {show(f[1])}\n    python  {show(p[1])}"]
            pending.append((f_at + 1, p_at + 1))
        elif f[0] == "br":
            if not matcher.tree(f[1], p[1]):
                return [f"branch {f_at}/{p_at}:\n    fortran {show(f[1])}\n    python  {show(p[1])}"]
            pending.append((f_labels[f[2]], p_labels[p[2]]))
            pending.append((f_labels[f[3]], p_labels[p[3]]))
    # code no path reaches was compared with nothing, so it can't be called identical
    for side, code, reached in (("fortran", f_code, {f for f, _ in visited}), ("python", p_code, {p for _, p in visited})):
        for index, instruction in enumerate(code):
            if instruction[0] in ("op", "br") and index not in reached:
                return [f"{side}: unreachable: {show(instruction)}"]
    return []


def compare_events(fortran: List[tuple], python: List[tuple], proc: Proc) -> List[str]:
    problems = []
    matcher = Matcher(proc)
    python = _without_local_zeros(fortran, python, matcher)
    if any(e[0] in ("goto", "jump", "label", "halt") for e in fortran + python):
        return lockstep(fortran, python, proc)
    for index in range(max(len(fortran), len(python))):
        f = fortran[index] if index < len(fortran) else None
        p = python[index] if index < len(python) else None
        if f is None or p is None or f[0] == "unknown" or p[0] == "unknown" or not matcher.event(f, p):
            problems.append(f"statement {index + 1}:\n    fortran {show(f)}\n    python  {show(p)}")
            break                           # later statements only repeat the misalignment
    return problems


def show(event: Optional[tuple]) -> str:
    return "(nothing)" if event is None else repr(event)[:300]


@dataclass
class Report:
    checked: int = 0
    failures: Dict[str, List[str]] = field(default_factory=dict)
    unchecked: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.checked > 0 and not self.failures and not self.unchecked


def same_values(wanted: Sequence[Any], got: Sequence[Any]) -> bool:
    """Initial values: same count, same numbers. Logicals are 0 and 1 in the tree dump."""
    def plain(value: Any) -> Any:
        return int(value) if isinstance(value, (bool, np.bool_)) else value
    return len(wanted) == len(got) and all(same_number(a, plain(b)) for a, b in zip(wanted, got))


def _python_values(node: ast.AST, reader: "PythonReader") -> Optional[List[Any]]:
    """The numbers of a module-level initial value, in column order."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "reshape":
        keywords = {k.arg: k.value for k in node.keywords}
        if getattr(keywords.get("order"), "value", None) != "F":
            return None
        return _python_values(node.func.value, reader)
    if isinstance(node, ast.Call) and dotted(node.func) == "np.array":
        elements = node.args[0].elts if isinstance(node.args[0], ast.List) else []
        kinds = {"np.float32": np.float32, "np.float64": np.float64}
        wrap = kinds.get(dotted(next((k.value for k in node.keywords if k.arg == "dtype"), ast.Name("?"))))
        out = [reader.expr(e) for e in elements]
        if not all(item[0] in ("num", "bool") for item in out):
            return None
        return [wrap(item[1]) if wrap else item[1] for item in out]
    value = reader.expr(node)
    return [value[1]] if value[0] in ("num", "bool") else None


def check_initial(proc: Proc, module_level: Dict[str, ast.AST]) -> Optional[str]:
    """Every DATA or initialised variable holds its Fortran values, position by position."""
    reader = PythonReader(proc)
    for name, wanted in proc.initial.items():
        value = module_level.get(f"_save_{proc.name}_{name}".lower())
        if value is None:
            continue
        got = _python_values(value, reader)
        if got is None or not same_values(wanted, got):
            return f"initial value of {name}: {ast.unparse(value)} is not the Fortran value"
    return None


def _module_parameters(proc: Proc, module_level: Dict[str, ast.AST]) -> Optional[str]:
    """A module's PARAMETER is the module-level <module>_<name> = value, and it must hold the Fortran value."""
    reader = PythonReader(proc)
    for name, sym in proc.symbols.items():
        value = module_level.get(f"{proc.name}_{name}".lower())
        if sym.parameter and sym.value is not None and value is not None and not Matcher(proc).tree(sym.value, reader.expr(value)):
            return f"PARAMETER {name}: {ast.unparse(value)} is not the Fortran value"
    return None


def check_python(originals: Sequence[Path], translated: Sequence[Path], dump: Optional[str] = None) -> Report:
    procs = parse_dump(dump if dump is not None else dump_fortran(originals))
    functions: Dict[str, ast.FunctionDef] = {}
    classes: Dict[str, List[str]] = {}
    module_level: Dict[str, ast.AST] = {}
    for path in translated:
        for stmt in ast.parse(Path(path).read_text(encoding="utf-8")).body:
            if isinstance(stmt, ast.FunctionDef) and not stmt.name.startswith("_"):
                functions[stmt.name.lower()] = stmt
            elif isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name):
                module_level[stmt.targets[0].id.lower()] = stmt.value
            elif isinstance(stmt, ast.ClassDef):
                classes[stmt.name.lower()] = [s.target.id for s in stmt.body if isinstance(s, ast.AnnAssign)]
    return compare_all(procs, functions, classes, originals, lambda fn, proc, known: python_events(fn, proc, known), module_level)


def compare_all(procs: Dict[str, Proc], functions: Dict[str, Any], classes: Dict[str, List[str]], originals: Sequence[Path],
                events_of, module_level: Optional[Dict[str, ast.AST]] = None) -> Report:
    """Each Fortran procedure against its translated function; `events_of` reads one function of the target language."""
    python_side = module_level is not None
    module_level = module_level if module_level is not None else {}
    report = Report()
    formats = {}
    for path in originals:
        formats.update(re.findall(r"(?im)^\s*(\d+)\s+format\s*(\(.*\))\s*$", Path(path).read_text(encoding="utf-8", errors="replace")))
    parameters = {f"{m.name}_{n}".lower(): sym for m in procs.values() for n, sym in m.symbols.items() if sym.parameter}
    every = {n: sym for m in procs.values() for n, sym in m.symbols.items() if sym.kind}
    initial = initial_values(originals)
    for proc in procs.values():
        proc.module_parameters, proc.module_level, proc.host_symbols = parameters, module_level, every
        proc.formats = formats
        proc.initial = initial.get(proc.name.lower(), {})
    for name, proc in procs.items():
        if name.startswith("__") or (not proc.code and not proc.symbols):       # __copy_...: gfortran's own helpers
            continue
        key = name.lower()
        is_main = key not in functions and "main" in functions and any(s for s in proc.code)
        fn = functions.get("main" if is_main else key)
        if fn is None:
            if proc.code:
                report.failures[name] = ["no translated procedure"]
            continue
        report.checked += 1
        problem = _module_parameters(proc, module_level) or (check_initial(proc, module_level) if python_side else None)
        if problem:
            report.failures[name] = [problem]
            continue
        try:
            problems = compare_events(fortran_events(proc), events_of(fn, proc, classes), proc)
        except Unchecked as exc:
            report.unchecked[name] = str(exc)
            continue
        if problems:
            report.failures[name] = problems
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--original", nargs="+", required=True, help="Fortran sources, in compilation order")
    parser.add_argument("--translated", nargs="+", required=True, help="translated Python or C++ files")
    parser.add_argument("--target", choices=["python", "cpp"], default=None, help="default: from the suffix of the translated files")
    ve.add_cli_options(parser)
    args = parser.parse_args(argv)
    try:
        cpp = args.target == "cpp" or (args.target is None and not all(p.endswith(".py") for p in args.translated))
        if cpp:
            import identity_cpp
            report = identity_cpp.check_cpp([Path(p) for p in args.original], [Path(p) for p in args.translated])
        else:
            report = check_python([Path(p) for p in args.original], [Path(p) for p in args.translated])
    except (Unchecked, OSError, SyntaxError) as exc:
        print(f"identity_checker: nothing was established: {exc}", file=sys.stderr)
        return 2
    for name, problems in report.failures.items():
        print(f"DIFFERENT {name}")
        for problem in problems:
            print("  " + problem)
    for name, why in report.unchecked.items():
        print(f"UNCHECKED {name}: {why}")
    print(f"{report.checked} procedure(s) checked, {len(report.failures)} different, {len(report.unchecked)} unchecked")
    passed = report.ok
    base = ve.log_path(args.evidence_dir).parent.parent
    evidence = ve.record_cli(args, tool="identity_checker", plugin="fortranbridge", kind="identity", name="fortran_vs_translation",
                             key="identity:" + "|".join(sorted(args.translated)), passed=passed, tolerance={},
                             subject_paths=list(args.original) + list(args.translated) + [__file__],
                             detail={"checker_version": CHECKER_VERSION, "procedures": report.checked,
                                     "originals": [ve.subject(p, base)["path"] for p in args.original],
                                     "translated": [ve.subject(p, base)["path"] for p in args.translated]})
    if evidence and evidence.get("error"):
        print(evidence["error"], file=sys.stderr)
        return 2
    if report.unchecked or report.checked == 0:
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
