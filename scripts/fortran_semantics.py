#!/usr/bin/env python3
r"""
fortran_semantics.py - Shared Fortran front-end for the Python and C++ emitters.

Provides:
- A tokenizer and precedence-climbing parser for Fortran expressions (AST).
- Program-unit splitting (SUBROUTINE / FUNCTION / PROGRAM) with a per-unit symbol table:
  types (explicit and implicit), array bounds, INTENT, PARAMETER constants, COMMON membership.
- Static type inference (integer / real / logical / character) so integer division can be
  translated with Fortran (truncating) semantics.
- Whole-file analysis: scalar dummy arguments that a unit modifies, and which COMMON blocks each
  unit needs (including through the units it calls).

Supported subset: structured control flow (DO / DO WHILE-free loops, IF / ELSE IF / ELSE),
assignments, CALL, RETURN, STOP, PRINT, PARAMETER, COMMON, DIMENSION and typed declarations.
Anything outside that subset is reported by the emitters instead of being silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from fortran_intrinsics import FORTRAN_INTRINSICS
from fortran_lexer import FortranStatement, lex_fortran_source

# ---------------------------------------------------------------------------------------------
# Expression AST
# ---------------------------------------------------------------------------------------------


@dataclass
class Num:
    text: str          # normalized literal text, e.g. "1.0e-12"
    is_int: bool


@dataclass
class Str:
    value: str


@dataclass
class Logical:
    value: bool


@dataclass
class Name:
    name: str          # lower-case


@dataclass
class Ref:
    """name(args): array element, intrinsic call or user function call (decided later)."""
    name: str
    args: List["Expr"]


@dataclass
class Slice:
    """lo:hi inside a subscript (either bound may be omitted)."""
    lo: Optional["Expr"]
    hi: Optional["Expr"]


@dataclass
class Unary:
    op: str            # '-', '+', 'not'
    operand: "Expr"


@dataclass
class Binary:
    op: str            # + - * / ** == != < <= > >= and or eqv neqv
    left: "Expr"
    right: "Expr"


Expr = object  # union of the node classes above


class FortranParseError(ValueError):
    pass


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
  | (?P<dotop>\.(?:eq|ne|lt|le|gt|ge|and|or|not|eqv|neqv|true|false)\.)
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eEdD][+-]?\d+)?(?:_\w+)?)
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>\*\*|//|==|/=|<=|>=|[-+*/<>(),=:%])
    """,
    re.VERBOSE | re.IGNORECASE,
)

_DOT_TO_OP = {
    ".eq.": "==", ".ne.": "!=", ".lt.": "<", ".le.": "<=", ".gt.": ">", ".ge.": ">=",
    ".and.": "and", ".or.": "or", ".eqv.": "eqv", ".neqv.": "neqv",
}

# binary operator -> (precedence, right_associative)
_BINARY = {
    "or": (1, False), "eqv": (1, False), "neqv": (1, False),
    "and": (2, False),
    "==": (4, False), "!=": (4, False), "<": (4, False), "<=": (4, False), ">": (4, False), ">=": (4, False),
    "+": (5, False), "-": (5, False),
    "*": (6, False), "/": (6, False),
    "**": (8, True),
}
_PREC_NOT = 3
_PREC_UNARY = 7   # unary +/- : binds looser than '**' and tighter than '*' '/'... see _parse_unary


def _tokenize(text: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise FortranParseError(f"cannot tokenize near {text[pos:pos + 12]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        tokens.append((kind, m.group()))
    return tokens


def _normalize_number(tok: str) -> Tuple[str, bool]:
    tok = re.sub(r"_\w+$", "", tok)            # kind suffix 1.0_8
    is_int = bool(re.fullmatch(r"\d+", tok))
    tok = re.sub(r"[dD]", "e", tok)
    tok = re.sub(r"[eE][+-]?0+$", "", tok)            # 1.0d0 -> 1.0
    if re.fullmatch(r"\d+\.", tok):
        tok += "0"
    if tok.startswith("."):
        tok = "0" + tok
    tok = re.sub(r"\.([eE])", r".0\1", tok)
    return tok, is_int


class _Parser:
    def __init__(self, tokens: List[Tuple[str, str]]):
        self.t = tokens
        self.i = 0

    def peek(self) -> Optional[Tuple[str, str]]:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> Tuple[str, str]:
        tok = self.t[self.i]
        self.i += 1
        return tok

    def _binary_op_at(self) -> Optional[str]:
        tok = self.peek()
        if not tok:
            return None
        kind, val = tok
        if kind == "dotop":
            return _DOT_TO_OP.get(val.lower())
        if kind == "op":
            return {"/=": "!="}.get(val, val) if val in _BINARY or val == "/=" else None
        return None

    def parse_expr(self, min_prec: int = 0) -> Expr:
        left = self._parse_unary()
        while True:
            op = self._binary_op_at()
            if op is None or op not in _BINARY:
                break
            prec, right_assoc = _BINARY[op]
            if prec < min_prec:
                break
            self.take()
            right = self.parse_expr(prec if right_assoc else prec + 1)
            left = Binary(op, left, right)
        return left

    def _parse_unary(self) -> Expr:
        tok = self.peek()
        if tok and tok[0] == "dotop" and tok[1].lower() == ".not.":
            self.take()
            return Unary("not", self.parse_expr(_PREC_NOT))
        if tok and tok[0] == "op" and tok[1] in "+-":
            self.take()
            # Fortran: -a*b == -(a*b) and -a**2 == -(a**2); the operand extends over '*' and '/'
            operand = self.parse_expr(_BINARY["*"][0])
            return Unary(tok[1], operand)
        return self._parse_primary()

    def _parse_primary(self) -> Expr:
        if self.peek() is None:
            raise FortranParseError("unexpected end of expression")
        kind, val = self.take()
        if kind == "num":
            text, is_int = _normalize_number(val)
            return Num(text, is_int)
        if kind == "str":
            return Str(val[1:-1].replace(val[0] * 2, val[0]))
        if kind == "dotop":
            low = val.lower()
            if low == ".true.":
                return Logical(True)
            if low == ".false.":
                return Logical(False)
            raise FortranParseError(f"unexpected operator {val}")
        if kind == "id":
            name = val.lower()
            nxt = self.peek()
            if nxt and nxt == ("op", "("):
                self.take()
                args = self._parse_args()
                return Ref(name, args)
            return Name(name)
        if (kind, val) == ("op", "("):
            inner = self.parse_expr()
            if self.peek() != ("op", ")"):
                raise FortranParseError("missing ')'")
            self.take()
            return inner
        raise FortranParseError(f"unexpected token {val!r}")

    def _parse_subscript(self) -> Expr:
        lo = None if self.peek() == ("op", ":") else self.parse_expr()
        if self.peek() == ("op", ":"):
            self.take()
            nxt = self.peek()
            hi = None if nxt in (("op", ","), ("op", ")")) else self.parse_expr()
            return Slice(lo, hi)
        return lo

    def _parse_args(self) -> List[Expr]:
        args: List[Expr] = []
        if self.peek() == ("op", ")"):
            self.take()
            return args
        while True:
            args.append(self._parse_subscript())
            nxt = self.peek()
            if nxt == ("op", ","):
                self.take()
                continue
            if nxt == ("op", ")"):
                self.take()
                return args
            raise FortranParseError("malformed argument list")


def parse_expression(text: str) -> Expr:
    r"""Parse a Fortran expression string into an AST."""
    parser = _Parser(_tokenize(text))
    expr = parser.parse_expr()
    if parser.peek() is not None:
        raise FortranParseError(f"trailing tokens in expression: {text!r}")
    return expr


def split_top_level(text: str, sep: str = ",") -> List[str]:
    r"""Split on `sep` outside parentheses and string literals."""
    parts, depth, cur, quote = [], 0, [], None
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


# ---------------------------------------------------------------------------------------------
# Symbols and program units
# ---------------------------------------------------------------------------------------------

INT, REAL, LOGICAL, CHAR, COMPLEX = "integer", "real", "logical", "character", "complex"


def base_type(f_type: str) -> str:
    t = f_type.upper()
    if t.startswith("INTEGER"):
        return INT
    if t.startswith("LOGICAL"):
        return LOGICAL
    if t.startswith("CHARACTER"):
        return CHAR
    if t.startswith("COMPLEX"):
        return COMPLEX
    return REAL


@dataclass
class Symbol:
    name: str
    type: str = REAL
    explicit_type: bool = False
    dims: List[Tuple[str, str]] = field(default_factory=list)   # (lower, upper) bound expression texts
    intent: Optional[str] = None                                # 'in' | 'out' | 'inout'
    is_dummy: bool = False
    const_value: Optional[str] = None                           # PARAMETER constant
    common_block: Optional[str] = None

    @property
    def rank(self) -> int:
        return len(self.dims)


@dataclass
class Unit:
    kind: str                       # 'SUBROUTINE' | 'FUNCTION' | 'PROGRAM'
    name: str
    params: List[str]
    stmts: List[FortranStatement]
    result_type: str = REAL
    symbols: Dict[str, Symbol] = field(default_factory=dict)
    implicit_none: bool = False
    implicit_rules: List[Tuple[str, str, str]] = field(default_factory=list)   # (type, first, last)
    modified_scalars: List[str] = field(default_factory=list)                   # dummy scalars assigned in the body
    out_only: List[str] = field(default_factory=list)                           # scalar dummies declared INTENT(OUT)
    commons: Set[str] = field(default_factory=set)                              # COMMON blocks needed (transitively)
    calls: Set[str] = field(default_factory=set)

    def sym(self, name: str) -> Symbol:
        name = name.lower()
        if name not in self.symbols:
            self.symbols[name] = Symbol(name, type=self.implicit_type(name))
        return self.symbols[name]

    def implicit_type(self, name: str) -> str:
        c = name[0].upper()
        for t, lo, hi in self.implicit_rules:
            if lo <= c <= hi:
                return base_type(t)
        return INT if "I" <= c <= "N" else REAL


def _parse_dim_spec(spec: str) -> List[Tuple[str, str]]:
    dims = []
    for d in split_top_level(spec):
        if ":" in d:
            lo, hi = d.split(":", 1)
            dims.append((lo.strip().lower() or "1", hi.strip().lower()))
        else:
            dims.append(("1", d.strip().lower()))
    return dims


def _split_decl_item(item: str) -> Tuple[str, Optional[str], Optional[str]]:
    """'a(n, m) = 3' -> ('a', 'n, m', '3')."""
    item = item.strip()
    init = None
    if "=" in item:
        depth = 0
        for k, ch in enumerate(item):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "=" and depth == 0:
                item, init = item[:k].strip(), item[k + 1:].strip()
                break
    m = re.match(r"^([A-Za-z_]\w*)\s*(?:\((.*)\))?$", item)
    if not m:
        raise FortranParseError(f"bad declarator {item!r}")
    return m.group(1).lower(), m.group(2), init


def split_units(statements: List[FortranStatement]) -> List[Unit]:
    r"""Group statements into program units and build each unit's symbol table."""
    units: List[Unit] = []
    cur: Optional[Unit] = None
    for s in statements:
        if s.stmt_type in ("SUBROUTINE", "FUNCTION", "PROGRAM"):
            if cur is not None:
                units.append(cur)
            d = s.details
            cur = Unit(
                kind=s.stmt_type, name=d["name"].lower(),
                params=[p.lower() for p in d.get("params", [])], stmts=[s],
            )
            if s.stmt_type == "FUNCTION" and d.get("result_type"):
                cur.result_type = base_type(d["result_type"])
            continue
        if cur is None:
            cur = Unit(kind="PROGRAM", name="main", params=[], stmts=[])
        cur.stmts.append(s)
        if s.stmt_type == "END":
            units.append(cur)
            cur = None
    if cur is not None and cur.stmts:
        units.append(cur)

    for u in units:
        _build_symbols(u)
    _analyze_program(units)
    return units


def _build_symbols(u: Unit) -> None:
    for p in u.params:
        sym = u.sym(p)
        sym.is_dummy = True
    if u.kind == "FUNCTION":
        u.sym(u.name).type = u.result_type

    # IMPLICIT statements first: they decide the default types of everything else.
    for s in u.stmts:
        if s.stmt_type == "IMPLICIT":
            raw = s.details["raw"]
            if re.match(r"^IMPLICIT\s+NONE", raw, re.IGNORECASE):
                u.implicit_none = True
                continue
            for m in re.finditer(r"(DOUBLE\s+PRECISION|REAL(?:\*\d+)?|INTEGER(?:\*\d+)?|LOGICAL|COMPLEX(?:\*\d+)?|CHARACTER(?:\*\d+)?)\s*\(([^)]*)\)", raw[8:], re.IGNORECASE):
                for rng in m.group(2).split(","):
                    rng = rng.strip().upper()
                    if "-" in rng:
                        lo, hi = [x.strip() for x in rng.split("-", 1)]
                    else:
                        lo = hi = rng
                    if lo and hi:
                        u.implicit_rules.append((m.group(1), lo[0], hi[0]))
    # re-type symbols created before implicit rules were known
    for sym in u.symbols.values():
        if not sym.explicit_type:
            sym.type = u.implicit_type(sym.name) if not (u.kind == "FUNCTION" and sym.name == u.name) else u.result_type

    for s in u.stmts:
        st, d = s.stmt_type, s.details
        if st == "TYPE_DECL":
            t = base_type(d["type"])
            attr_dims = d.get("attr_dimension")
            for item in split_top_level(d["declarations"]):
                try:
                    name, dim, init = _split_decl_item(item)
                except FortranParseError:
                    continue
                sym = u.sym(name)
                sym.type, sym.explicit_type = t, True
                if dim:
                    sym.dims = _parse_dim_spec(dim)
                elif attr_dims:
                    sym.dims = _parse_dim_spec(attr_dims)
                if d.get("intent"):
                    sym.intent = d["intent"]
                if d.get("is_parameter") and init is not None:
                    sym.const_value = init
            if u.kind == "FUNCTION" and u.name in u.symbols and u.symbols[u.name].explicit_type:
                u.result_type = u.symbols[u.name].type
        elif st == "DIMENSION":
            for item in split_top_level(d["raw"]):
                try:
                    name, dim, _ = _split_decl_item(item)
                except FortranParseError:
                    continue
                if dim:
                    u.sym(name).dims = _parse_dim_spec(dim)
        elif st == "PARAMETER":
            for k, v in d["constants"].items():
                sym = u.sym(k)
                sym.const_value = v
        elif st == "COMMON":
            for v in d["variables"]:
                try:
                    name, dim, _ = _split_decl_item(v)
                except FortranParseError:
                    continue
                sym = u.sym(name)
                sym.common_block = d["block"].upper()
                if dim:
                    sym.dims = _parse_dim_spec(dim)


# ---------------------------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------------------------

_INT_RESULT_INTRINSICS = {"INT", "IFIX", "IDINT", "NINT", "IDNINT", "IABS", "ISIGN", "IDIM", "MAX0", "MIN0",
                          "MAX1", "MIN1", "SIZE", "LEN"}
_REAL_RESULT_INTRINSICS = {"REAL", "FLOAT", "DFLOAT", "DBLE", "SNGL", "AMAX0", "AMIN0"}


def infer_type(expr: Expr, u: Unit, func_types: Optional[Dict[str, str]] = None) -> str:
    r"""Static type of an expression (integer / real / logical / character / complex)."""
    func_types = func_types or {}
    if isinstance(expr, Num):
        return INT if expr.is_int else REAL
    if isinstance(expr, Logical):
        return LOGICAL
    if isinstance(expr, Str):
        return CHAR
    if isinstance(expr, Name):
        return u.sym(expr.name).type
    if isinstance(expr, Ref):
        sym = u.symbols.get(expr.name)
        if sym is not None and sym.rank:
            return sym.type
        if sym is not None and sym.name in func_types:
            return func_types[sym.name]
        key = expr.name.upper()
        if key in FORTRAN_INTRINSICS:
            if key in _INT_RESULT_INTRINSICS:
                return INT
            if key in _REAL_RESULT_INTRINSICS:
                return REAL
            if key in ("MOD", "MAX", "MIN", "ABS", "SIGN", "DIM", "SUM", "PRODUCT", "MAXVAL", "MINVAL"):
                kinds = [infer_type(a, u, func_types) for a in expr.args] or [REAL]
                return INT if all(k == INT for k in kinds) else REAL
            return REAL
        if expr.name in func_types:
            return func_types[expr.name]
        return u.implicit_type(expr.name)
    if isinstance(expr, Unary):
        return LOGICAL if expr.op == "not" else infer_type(expr.operand, u, func_types)
    if isinstance(expr, Binary):
        if expr.op in ("==", "!=", "<", "<=", ">", ">=", "and", "or", "eqv", "neqv"):
            return LOGICAL
        a, b = infer_type(expr.left, u, func_types), infer_type(expr.right, u, func_types)
        if COMPLEX in (a, b):
            return COMPLEX
        if a == INT and b == INT:
            return INT
        return REAL
    return REAL


# ---------------------------------------------------------------------------------------------
# Whole-program analysis
# ---------------------------------------------------------------------------------------------


def _names_in(expr: Expr, out: Set[str]) -> None:
    if isinstance(expr, Name):
        out.add(expr.name)
    elif isinstance(expr, Ref):
        out.add(expr.name)
        for a in expr.args:
            _names_in(a, out)
    elif isinstance(expr, Slice):
        for part in (expr.lo, expr.hi):
            if part is not None:
                _names_in(part, out)
    elif isinstance(expr, Unary):
        _names_in(expr.operand, out)
    elif isinstance(expr, Binary):
        _names_in(expr.left, out)
        _names_in(expr.right, out)


def statement_expressions(s: FortranStatement) -> List[str]:
    """Raw expression strings that a statement evaluates (used for name scanning)."""
    d = s.details
    st = s.stmt_type
    if st == "ASSIGNMENT":
        return [d["lhs"], d["rhs"]]
    if st == "DO":
        return [d["var"], d["start"], d["end"], d["step"]]
    if st == "DO_WHILE":
        return [d["condition"]]
    if st == "IO":
        return list(io_items(d["statement"])[1])
    if st in ("IF_THEN", "ELSE_IF", "ARITHMETIC_IF"):
        return [d["condition"]]
    if st == "IF_STMT":
        return [d["condition"], d["statement"]]
    if st == "CALL":
        return list(d["args"])
    if st == "PARAMETER":
        return list(d["constants"].values())
    return []


def io_items(raw: str) -> Tuple[str, List[str]]:
    r"""Classify an I/O statement. Returns (kind, items): kind is 'print' for list-directed output to
    the default unit (PRINT *, ... / WRITE(*,*) ... / WRITE(6,*) ...), else 'other' (items empty)."""
    m = re.match(r"^PRINT\s*\*\s*(?:,\s*(.*))?$", raw, re.IGNORECASE)
    if m:
        return "print", split_top_level(m.group(1) or "")
    m = re.match(r"^WRITE\s*\(\s*(?:\*|6)\s*,\s*\*\s*\)\s*(.*)$", raw, re.IGNORECASE)
    if m:
        return "print", split_top_level(m.group(1))
    return "other", []


def add_const(expr: Expr, c: int) -> Expr:
    r"""expr + c with integer constant folding (used for 1-based -> 0-based index conversion)."""
    if c == 0:
        return expr
    if isinstance(expr, Num) and expr.is_int:
        return Num(str(int(expr.text) + c), True)
    if isinstance(expr, Binary) and expr.op in ("+", "-") and isinstance(expr.right, Num) and expr.right.is_int:
        k = int(expr.right.text) * (1 if expr.op == "+" else -1) + c
        if k == 0:
            return expr.left
        return Binary("+" if k > 0 else "-", expr.left, Num(str(abs(k)), True))
    return Binary("+" if c > 0 else "-", expr, Num(str(abs(c)), True))


def iter_calls(u: Unit) -> List[Tuple[str, List[str]]]:
    r"""(callee, actual-argument texts) for every CALL in the unit, including `IF (c) CALL f(...)`."""
    from fortran_lexer import classify_statement
    calls = []
    for s in u.stmts:
        if s.stmt_type == "CALL":
            calls.append((s.details["subroutine"].lower(), list(s.details["args"])))
        elif s.stmt_type == "IF_STMT":
            kind, det = classify_statement(s.details["statement"])
            if kind == "CALL":
                calls.append((det["subroutine"].lower(), list(det["args"])))
    return calls


def _register_names(u: Unit) -> None:
    r"""Create (implicitly typed) symbols for every variable that is used but never declared."""
    for s in u.stmts:
        texts = statement_expressions(s)
        if s.stmt_type == "IF_STMT":
            from fortran_lexer import classify_statement
            kind, det = classify_statement(s.details["statement"])
            fake = FortranStatement(s.details["statement"], s.details["statement"], s.line_no, None, kind, det)
            texts = [s.details["condition"]] + statement_expressions(fake)
        for text in texts:
            try:
                tree = parse_expression(text)
            except FortranParseError:
                continue
            _register_tree(u, tree)


def _register_tree(u: Unit, tree: Expr) -> None:
    if isinstance(tree, Name):
        if tree.name.upper() not in FORTRAN_INTRINSICS or tree.name in u.symbols:
            u.sym(tree.name)
    elif isinstance(tree, Ref):
        for a in tree.args:
            _register_tree(u, a)
    elif isinstance(tree, Slice):
        for part in (tree.lo, tree.hi):
            if part is not None:
                _register_tree(u, part)
    elif isinstance(tree, Unary):
        _register_tree(u, tree.operand)
    elif isinstance(tree, Binary):
        _register_tree(u, tree.left)
        _register_tree(u, tree.right)


def _analyze_program(units: List[Unit]) -> None:
    by_name = {u.name: u for u in units}
    for u in units:
        _register_names(u)
    for u in units:
        direct: Set[str] = set()
        modified: List[str] = []
        for s in u.stmts:
            if s.stmt_type == "CALL":
                u.calls.add(s.details["subroutine"].lower())
            if s.stmt_type == "IF_STMT":
                m = re.match(r"^CALL\s+(\w+)", s.details["statement"], re.IGNORECASE)
                if m:
                    u.calls.add(m.group(1).lower())
            for text in statement_expressions(s):
                names: Set[str] = set()
                try:
                    _names_in(parse_expression(text), names)
                except FortranParseError:
                    for m in re.finditer(r"[A-Za-z_]\w*", text):
                        names.add(m.group().lower())
                for n in names:
                    sym = u.symbols.get(n)
                    if sym is not None and sym.common_block:
                        direct.add(sym.common_block)
            # scalar dummies assigned in the body (call-by-reference outputs)
            targets: List[str] = []
            if s.stmt_type == "ASSIGNMENT":
                targets.append(s.details["lhs"])
            elif s.stmt_type == "IF_STMT":
                m = re.match(r"^([A-Za-z_]\w*)\s*=[^=]", s.details["statement"])
                if m:
                    targets.append(m.group(1))
            for tgt in targets:
                m = re.match(r"^([A-Za-z_]\w*)\s*$", tgt.strip())
                if m:
                    n = m.group(1).lower()
                    sym = u.symbols.get(n)
                    if sym and sym.is_dummy and not sym.rank and n not in modified and sym.intent != "in":
                        modified.append(n)
        for p in u.params:
            sym = u.symbols[p]
            if sym.is_dummy and not sym.rank and sym.intent in ("out", "inout") and p not in modified:
                modified.append(p)
        u.modified_scalars = [p for p in u.params if p in modified]
        u.out_only = [p for p in u.params if u.symbols[p].intent == "out" and not u.symbols[p].rank]
        u.commons = direct

    # a dummy scalar passed on to a callee that modifies it is itself modified
    changed = True
    while changed:
        changed = False
        for u in units:
            for callee_name, actuals in iter_calls(u):
                cu = by_name.get(callee_name)
                if cu is None:
                    continue
                for pos, formal in enumerate(cu.params):
                    if formal not in cu.modified_scalars or pos >= len(actuals):
                        continue
                    m = re.match(r"^([A-Za-z_]\w*)$", actuals[pos].strip())
                    if not m:
                        continue
                    n = m.group(1).lower()
                    sym = u.symbols.get(n)
                    if sym and sym.is_dummy and not sym.rank and sym.intent != "in" and n not in u.modified_scalars:
                        u.modified_scalars = [p for p in u.params if p in set(u.modified_scalars) | {n}]
                        changed = True

    changed = True
    while changed:
        changed = False
        for u in units:
            for callee in u.calls:
                cu = by_name.get(callee)
                if cu and not cu.commons <= u.commons:
                    u.commons |= cu.commons
                    changed = True


def function_result_types(units: List[Unit]) -> Dict[str, str]:
    return {u.name: u.result_type for u in units if u.kind == "FUNCTION"}


def lex_and_split(source_text: str) -> Tuple[List[FortranStatement], List[Unit]]:
    stmts = lex_fortran_source(source_text)
    return stmts, split_units(stmts)
