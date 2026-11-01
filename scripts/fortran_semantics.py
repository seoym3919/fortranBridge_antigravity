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
    suffix: Optional[str] = None    # kind suffix as written: 1.0_8 -> "8", 1.0_dp -> "dp"
    double: bool = False            # real literal written with a D exponent (1.0d0): double precision


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
class ArrayCtor:
    """(/ a, b, (f(i), i=1,n) /) or [a, b]: a rank-1 array value. Items may be ImpliedDo."""
    items: List["Expr"]


@dataclass
class ImpliedDo:
    """(item, item, ..., var = lo, hi [, step]) inside an array constructor or an I/O list."""
    items: List["Expr"]
    var: str
    lo: "Expr"
    hi: "Expr"
    step: Optional["Expr"] = None


@dataclass
class ComplexLit:
    """(re, im) -- a complex literal."""
    re: "Expr"
    im: "Expr"


@dataclass
class Component:
    """base%comp or base%comp(subscripts): a component of a derived-type value."""
    base: "Expr"
    comp: str
    args: Optional[List["Expr"]] = None


@dataclass
class TypeComponent:
    name: str
    type: str
    bits: int
    dims: List[Tuple[str, str]]
    derived: Optional[str] = None
    init: Optional[str] = None
    pointer: bool = False                      # a POINTER or ALLOCATABLE component


@dataclass
class TypeDef:
    name: str
    components: List[TypeComponent]

    def get(self, comp: str) -> Optional[TypeComponent]:
        return next((c for c in self.components if c.name == comp), None)


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
  | (?P<op>\*\*|//|==|/=|<=|>=|[-+*/<>(),=:%\[\]])
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
    "//": (5, False),
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
    return _number_info(tok)[:2]


def _number_info(tok: str) -> Tuple[str, bool, Optional[str], bool]:
    """(normalized text, is_int, kind suffix, written with a D exponent)."""
    suffix = None
    m_suffix = re.search(r"_(\w+)$", tok)
    if m_suffix:
        suffix = m_suffix.group(1)
    double = bool(re.search(r"\d[dD][+-]?\d", tok))
    tok = re.sub(r"_\w+$", "", tok)            # kind suffix 1.0_8
    is_int = bool(re.fullmatch(r"\d+", tok))
    tok = re.sub(r"[dD]", "e", tok)
    tok = re.sub(r"[eE][+-]?0+$", "", tok)            # 1.0d0 -> 1.0
    if re.fullmatch(r"\d+\.", tok):
        tok += "0"
    if tok.startswith("."):
        tok = "0" + tok
    tok = re.sub(r"\.([eE])", r".0\1", tok)
    return tok, is_int, suffix, double


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
            if val == "/" and self.i + 1 < len(self.t) and self.t[self.i + 1] == ("op", ")"):
                return None                       # the "/)" that closes an array constructor, never a division
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
            text, is_int, suffix, double = _number_info(val)
            return Num(text, is_int, suffix, double)
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
                node: Expr = Ref(name, args)
            else:
                node = Name(name)
            while self.peek() == ("op", "%"):               # base%component[(subscripts)]
                self.take()
                tok = self.take()
                if tok[0] != "id":
                    raise FortranParseError("a component name must follow %")
                subs = None
                if self.peek() == ("op", "("):
                    self.take()
                    subs = self._parse_args()
                node = Component(node, tok[1].lower(), subs)
            return node
        if (kind, val) == ("op", "["):
            return ArrayCtor(self._ctor_items(("op", "]")))
        if (kind, val) == ("op", "(") and self.peek() == ("op", "/"):
            self.take()
            items = self._ctor_items(("op", "/"))
            if self.peek() != ("op", ")"):
                raise FortranParseError("missing ')' after array constructor")
            self.take()
            return ArrayCtor(items)
        if (kind, val) == ("op", "("):
            group = self._implied_do_or_complex()
            if group is not None:
                return group
            inner = self.parse_expr()
            if self.peek() != ("op", ")"):
                raise FortranParseError("missing ')'")
            self.take()
            return inner
        raise FortranParseError(f"unexpected token {val!r}")

    def _ctor_items(self, close: Tuple[str, str]) -> List[Expr]:
        items: List[Expr] = []
        while True:
            if self.peek() == close:
                self.take()
                return items
            items.append(self.parse_expr())
            if self.peek() == ("op", ","):
                self.take()
                continue
            if self.peek() == close:
                self.take()
                return items
            raise FortranParseError("malformed array constructor")

    def _implied_do_or_complex(self) -> Optional[Expr]:
        """After '(' : an implied DO `(items, v = lo, hi[, step])` or a complex literal `(re, im)`; None if neither."""
        start = self.i
        try:
            items: List[Expr] = []
            while True:
                items.append(self.parse_expr())
                nxt = self.peek()
                if nxt != ("op", ","):
                    break
                self.take()
                a, b = self.peek(), (self.t[self.i + 1] if self.i + 1 < len(self.t) else None)
                if a and a[0] == "id" and b == ("op", "="):
                    var = self.take()[1].lower()
                    self.take()
                    lo = self.parse_expr()
                    if self.peek() != ("op", ","):
                        raise FortranParseError("implied DO needs an upper bound")
                    self.take()
                    hi = self.parse_expr()
                    step = None
                    if self.peek() == ("op", ","):
                        self.take()
                        step = self.parse_expr()
                    if self.peek() != ("op", ")"):
                        raise FortranParseError("missing ')' after implied DO")
                    self.take()
                    return ImpliedDo(items, var, lo, hi, step)
            if self.peek() == ("op", ")") and len(items) == 2:
                self.take()
                return ComplexLit(items[0], items[1])
        except FortranParseError:
            pass
        self.i = start
        return None

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
DERIVED = "derived"


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
    if t.startswith("TYPE("):
        return DERIVED
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
    common_field: Optional[str] = None                          # name of the member this occupies in the block's struct
    saved: bool = False                                         # static storage: DATA / SAVE / initialised declaration
    host_const: bool = False                                    # a host's PARAMETER seen from an internal procedure
    eq: Optional[Tuple[int, int]] = None                        # (EQUIVALENCE group, element offset in the group's storage)
    module: Optional[str] = None                                # defined in (or imported from) this MODULE
    char_len: Optional[int] = None                              # CHARACTER length (None: unknown or assumed)
    derived: Optional[str] = None                               # the derived type's name, for type(name) variables
    allocatable: bool = False                                   # ALLOCATABLE array: shape set by ALLOCATE
    pointer: bool = False                                       # POINTER: an alias set by p => target
    target: bool = False                                        # TARGET: may be pointed to (scalars live in a one-element box in Python)
    bits: int = 0                                               # INTEGER/REAL width: 32, 64; 0 = not set yet, -1 = unresolved kind

    @property
    def rank(self) -> int:
        return len(self.dims)


@dataclass
class Unit:
    kind: str                       # 'SUBROUTINE' | 'FUNCTION' | 'PROGRAM' | 'MODULE'
    name: str
    params: List[str]
    stmts: List[FortranStatement]
    result_type: str = REAL
    result_bits: int = 0
    host_module: Optional[str] = None        # module whose CONTAINS section holds this procedure
    host_unit: Optional[str] = None          # unit whose CONTAINS section holds this internal procedure
    owned_blocks: Set[str] = field(default_factory=set)   # COMMON-style blocks this unit creates itself (shared with its internals)
    symbols: Dict[str, Symbol] = field(default_factory=dict)
    implicit_none: bool = False
    implicit_rules: List[Tuple[str, str, str]] = field(default_factory=list)   # (type, first, last)
    modified_scalars: List[str] = field(default_factory=list)                   # dummy scalars assigned in the body
    out_only: List[str] = field(default_factory=list)                           # scalar dummies declared INTENT(OUT)
    commons: Set[str] = field(default_factory=set)                              # COMMON blocks needed (transitively)
    common_layout: Dict[str, List[str]] = field(default_factory=dict)           # block -> member names in storage order
    common_conflicts: List[str] = field(default_factory=list)                   # members that do not line up with the block
    data_values: Dict[str, List["Expr"]] = field(default_factory=dict)          # variable -> initial values, flat, array order
    static_problems: List[str] = field(default_factory=list)                    # DATA / SAVE forms that are not translated
    eq_groups: List[dict] = field(default_factory=list)                         # EQUIVALENCE storage: {type, bits, size, members}
    types: Dict[str, "TypeDef"] = field(default_factory=dict)                   # derived types of the translation (shared)
    calls: Set[str] = field(default_factory=set)

    def sym(self, name: str) -> Symbol:
        name = name.lower()
        if name not in self.symbols:
            self.symbols[name] = Symbol(name, type=self.implicit_type(name), bits=self.implicit_bits(name))
        return self.symbols[name]

    def passed_commons(self) -> List[str]:
        """The blocks a caller has to hand to this unit (not those it creates itself)."""
        return sorted(self.commons - self.owned_blocks)

    def implicit_bits(self, name: str) -> int:
        c = name[0].upper()
        for t, lo, hi in self.implicit_rules:
            if lo <= c <= hi:
                return kind_bits(t, self)
        return 32

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
    module: Optional[Unit] = None           # the MODULE whose specification part or CONTAINS section we are in
    in_spec = False
    interface: Optional[dict] = None        # an INTERFACE block being skipped
    host: Optional[Unit] = None             # a unit whose CONTAINS section we are in (its internal procedures follow)
    inner: Optional[Unit] = None            # the internal procedure being read
    types: Dict[str, TypeDef] = {}
    typedef: Optional[dict] = None          # a derived-type definition being read
    for s in statements:
        if s.stmt_type == "TYPE_DEF":
            typedef = {"name": s.details["name"], "stmts": [], "attrs": s.details["attrs"]}
            continue
        if typedef is not None:
            if s.stmt_type == "END_TYPE":
                types[typedef["name"]] = _build_typedef(typedef, types)
                typedef = None
            else:
                typedef["stmts"].append(s)
            continue
        if s.stmt_type == "INTERFACE":
            interface = {"generic": bool(s.details["name"]), "owner": cur if cur is not None else module}
            continue
        if interface is not None:
            if s.stmt_type == "END_INTERFACE":
                if interface["generic"] and interface["owner"] is not None:
                    interface["owner"].static_problems.append(
                        "a generic INTERFACE (overloading by argument type) is not translated")
                interface = None
            elif s.raw.upper().startswith("MODULE PROCEDURE"):
                interface["generic"] = True
            continue                       # an explicit interface only describes an external procedure: nothing to translate
        if s.stmt_type == "MODULE" and cur is None:
            module = Unit(kind="MODULE", name=s.details["name"], params=[], stmts=[])
            in_spec = True
            units.append(module)
            continue
        if module is not None and cur is None and s.stmt_type in ("END", "END_MODULE"):
            module, in_spec = None, False          # END MODULE (the lexer reports it as a plain END)
            continue
        if module is not None and in_spec:
            if s.stmt_type == "CONTAINS":
                in_spec = False
            else:
                module.stmts.append(s)
            continue
        if s.stmt_type == "CONTAINS" and cur is not None and host is None and cur.kind != "MODULE":
            host = cur                           # internal procedures follow, up to the host's own END
            continue
        if host is not None:
            if inner is None and s.stmt_type in ("SUBROUTINE", "FUNCTION"):
                d = s.details
                inner = Unit(kind=s.stmt_type, name=d["name"].lower(), params=[p.lower() for p in d.get("params", [])],
                             stmts=[s], host_unit=host.name, host_module=host.host_module)
                if s.stmt_type == "FUNCTION" and d.get("result_type"):
                    inner.result_type = base_type(d["result_type"])
                    inner.result_bits = kind_bits(d["result_type"], inner)
                continue
            if inner is not None:
                inner.stmts.append(s)
                if s.stmt_type == "END":
                    units.append(inner)
                    inner = None
                continue
            if s.stmt_type == "END":             # the host's END
                host.stmts.append(s)
                units.append(host)
                cur, host = None, None
            continue
        if s.stmt_type in ("SUBROUTINE", "FUNCTION", "PROGRAM"):
            if cur is not None:
                units.append(cur)
            d = s.details
            cur = Unit(
                kind=s.stmt_type, name=d["name"].lower(),
                params=[p.lower() for p in d.get("params", [])], stmts=[s],
                host_module=module.name if module is not None else None,
            )
            if s.stmt_type == "FUNCTION" and d.get("result_type"):
                cur.result_type = base_type(d["result_type"])
                cur.result_bits = kind_bits(d["result_type"], cur)
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
        u.types = types
    for u in units:
        _build_symbols(u)
    _link_internal_procedures(units)
    for u in units:
        _link_attributes(u)
    for u in units:
        _link_equivalence(u)
    for u in units:
        _collect_static(u)
    _import_modules(units)
    link_common_blocks(units)
    _analyze_program(units)
    return units


def _build_symbols(u: Unit) -> None:
    for p in u.params:
        sym = u.sym(p)
        sym.is_dummy = True
    if u.kind == "FUNCTION":
        u.sym(u.name).type = u.result_type
        u.sym(u.name).bits = u.result_bits or u.implicit_bits(u.name)

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
            is_result = u.kind == "FUNCTION" and sym.name == u.name
            sym.type = u.implicit_type(sym.name) if not is_result else u.result_type
            sym.bits = u.implicit_bits(sym.name) if not is_result else (u.result_bits or u.implicit_bits(sym.name))

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
                sym.bits = kind_bits(d["type"], u)
                sym.allocatable = "allocatable" in d.get("attrs", [])
                sym.pointer = "pointer" in d.get("attrs", [])
                sym.target = "target" in d.get("attrs", [])
                m_derived = re.match(r"^TYPE\((\w+)\)$", d["type"], re.IGNORECASE)
                sym.derived = m_derived.group(1).lower() if m_derived else None
                if t == CHAR:
                    m_len = re.match(r"^CHARACTER\*(\w+)$", d["type"], re.IGNORECASE)
                    sym.char_len = const_int(m_len.group(1), u) if m_len else None
                if dim:
                    sym.dims = _parse_dim_spec(dim)
                elif attr_dims:
                    sym.dims = _parse_dim_spec(attr_dims)
                if d.get("intent"):
                    sym.intent = d["intent"]
                if d.get("is_parameter") and init is not None:
                    sym.const_value = init
                elif init is not None and not sym.is_dummy:
                    _add_data(u, sym, [init])            # real :: x = 0.5 initialises once and implies SAVE
            if u.kind == "FUNCTION" and u.name in u.symbols and u.symbols[u.name].explicit_type:
                u.result_type = u.symbols[u.name].type
                u.result_bits = u.symbols[u.name].bits
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
                u.common_layout.setdefault(sym.common_block, []).append(sym.name)
                if dim:
                    sym.dims = _parse_dim_spec(dim)


def _subst(e: Expr, env: Dict[str, int]) -> Expr:
    """Replace implied-DO variables in `e` by their integer values."""
    if isinstance(e, Name) and e.name in env:
        return Num(str(env[e.name]), True)
    if isinstance(e, Ref):
        return Ref(e.name, [_subst(a, env) for a in e.args])
    if isinstance(e, Unary):
        return Unary(e.op, _subst(e.operand, env))
    if isinstance(e, Binary):
        return Binary(e.op, _subst(e.left, env), _subst(e.right, env))
    if isinstance(e, ComplexLit):
        return ComplexLit(_subst(e.re, env), _subst(e.im, env))
    return e


def _expand_ctor(e: Expr, u: "Unit", env: Optional[Dict[str, int]] = None) -> Optional[List[Expr]]:
    """Flatten an array constructor with constant implied-DO bounds into its element expressions (None if it cannot be)."""
    env = env or {}
    if isinstance(e, ArrayCtor):
        out: List[Expr] = []
        for item in e.items:
            part = _expand_ctor(item, u, env)
            if part is None:
                return None
            out += part
        return out
    if isinstance(e, ImpliedDo):
        lo, hi = _eval_int(e.lo, u, 0, env), _eval_int(e.hi, u, 0, env)
        step = 1 if e.step is None else _eval_int(e.step, u, 0, env)
        if lo is None or hi is None or not step:
            return None
        out = []
        for value in range(lo, hi + (1 if step > 0 else -1), step):
            inner = {**env, e.var: value}
            for item in e.items:
                part = _expand_ctor(item, u, inner)
                if part is None:
                    return None
                out += part
        return out
    return [_subst(e, env)]


def _zero_of(sym: Symbol) -> Expr:
    return {LOGICAL: Logical(False), CHAR: Str("")}.get(sym.type, Num("0", True))


def _flat_index(sym: Symbol, args: List[Expr], u: "Unit", env: Dict[str, int]) -> Optional[int]:
    """Column-major offset of a constant-subscript array element."""
    if len(args) != sym.rank:
        return None
    offset, stride = 0, 1
    for (lb, ub), arg in zip(sym.dims, args):
        sub, lo, hi = _eval_int(arg, u, 0, env), const_int(lb, u), const_int(ub, u)
        if sub is None or lo is None or hi is None or not lo <= sub <= hi:
            return None
        offset += (sub - lo) * stride
        stride *= hi - lo + 1
    return offset


def _add_data(u: "Unit", sym: Symbol, texts: List[str], source: str = "") -> None:
    """Record initial values for a whole variable (flat, in array order) and give it static storage."""
    if sym.common_block or sym.is_dummy or sym.const_value is not None:
        u.static_problems.append(f"{source or sym.name}: DATA/initialiser for a COMMON member, dummy argument or constant "
                                 "is not translated")
        return
    size = 1
    if sym.rank:
        extents = _extents(sym, u)
        if extents is None:
            u.static_problems.append(f"{sym.name}: DATA for an array with non-constant bounds is not translated")
            return
        size = 1
        for n in extents:
            size *= n
    try:
        if sym.rank and len(texts) == 1 and texts[0].lstrip().startswith(("(/", "[")):
            values = _expand_ctor(parse_expression(texts[0]), u)           # integer :: v(3) = (/ 1, 2, 3 /)
            if values is None:
                u.static_problems.append(f"{sym.name}: the initial array constructor is not constant")
                return
        else:
            values = [parse_expression(t) for t in texts]
    except FortranParseError as exc:
        u.static_problems.append(f"{sym.name}: cannot parse a DATA value ({exc})")
        return
    if len(values) != size:
        u.static_problems.append(f"{sym.name}: DATA gives {len(values)} value(s) for {size} element(s)")
        return
    u.data_values[sym.name] = values
    sym.saved = True


def _extents(sym: Symbol, u: "Unit") -> Optional[List[int]]:
    return common_extents(sym, u) if sym.rank else []


def _collect_static(u: "Unit") -> None:
    """DATA and SAVE statements: which variables have static storage and what they start as."""
    assigned = set()
    for s in u.stmts:
        if s.stmt_type == "ASSIGNMENT":
            m = re.match(r"^([A-Za-z_]\w*)", s.details["lhs"].strip())
            if m:
                assigned.add(m.group(1).lower())
        elif s.stmt_type == "DO":
            assigned.add(s.details["var"].lower())
    for s in u.stmts:
        if s.stmt_type != "NONEXEC":
            continue
        text = s.details["text"]
        m_save = re.match(r"^SAVE\b\s*(.*)$", text, re.IGNORECASE)
        if m_save:
            rest = m_save.group(1).strip()
            names = [n.strip().lower() for n in rest.split(",") if n.strip()] if rest else None
            for name in (names if names is not None else [
                    n for n, sy in u.symbols.items() if (sy.explicit_type or sy.rank or n in assigned)]):
                sym = u.symbols.get(name)
                if sym is None or name.startswith("/"):
                    if name.startswith("/"):
                        u.static_problems.append(f"SAVE of a COMMON block ({name}) is not translated")
                    continue
                if sym.is_dummy or sym.common_block or sym.const_value is not None or (
                        u.kind == "FUNCTION" and name == u.name) or name in u.params:
                    continue
                sym.saved = True
            continue
        m_data = re.match(r"^DATA\b\s*(.*)$", text, re.IGNORECASE)
        if m_data:
            _parse_data(u, m_data.group(1))


def _data_targets(u: "Unit", obj: str) -> Optional[List[Tuple[Symbol, Optional[int]]]]:
    """What one DATA object receives values: [(symbol, None)] for a whole variable, or [(symbol, flat index), ...] for an
    array element or an implied DO over constant bounds. None if the form is not translated."""
    if re.fullmatch(r"[A-Za-z_]\w*", obj):
        return [(u.sym(obj), None)]
    try:
        tree = parse_expression(obj)
    except FortranParseError:
        return None

    def walk(e: Expr, env: Dict[str, int]) -> Optional[List[Tuple[Symbol, Optional[int]]]]:
        if isinstance(e, Ref):
            sym = u.symbols.get(e.name)
            if sym is None or not sym.rank:
                return None
            if any(isinstance(a, Slice) for a in e.args):             # a section: its elements in array order
                axes: List[List[Expr]] = []
                for (lb, ub), a in zip(sym.dims, e.args):
                    if isinstance(a, Slice):
                        lo = _eval_int(a.lo, u, 0, env) if a.lo is not None else const_int(lb, u)
                        hi = _eval_int(a.hi, u, 0, env) if a.hi is not None else const_int(ub, u)
                        if lo is None or hi is None:
                            return None
                        axes.append([Num(str(k), True) for k in range(lo, hi + 1)])
                    else:
                        axes.append([a])
                combos: List[List[Expr]] = [[]]
                for axis in reversed(axes):                            # the first subscript varies fastest
                    combos = [[item] + rest for rest in combos for item in axis]
                combos = [[c for c in combo] for combo in combos]
                order = sorted(combos, key=lambda combo: [(_eval_int(x, u, 0, env) or 0) for x in reversed(combo)])
                out = []
                for combo in order:
                    index = _flat_index(sym, combo, u, env)
                    if index is None:
                        return None
                    out.append((sym, index))
                return out
            index = _flat_index(sym, e.args, u, env)
            return None if index is None else [(sym, index)]
        if isinstance(e, ImpliedDo):
            lo, hi = _eval_int(e.lo, u, 0, env), _eval_int(e.hi, u, 0, env)
            step = 1 if e.step is None else _eval_int(e.step, u, 0, env)
            if lo is None or hi is None or not step:
                return None
            out: List[Tuple[Symbol, Optional[int]]] = []
            for value in range(lo, hi + (1 if step > 0 else -1), step):
                for item in e.items:
                    part = walk(item, {**env, e.var: value})
                    if part is None:
                        return None
                    out += part
            return out
        return None
    return walk(tree, {})


def _parse_data(u: "Unit", body: str) -> None:
    """DATA obj, obj /v, r*v/ , obj /v/ ... : whole scalars and arrays, array elements, and implied-DO lists."""
    pos = 0
    partial: Dict[str, Dict[int, Expr]] = {}
    while pos < len(body):
        m = re.match(r"\s*,?\s*([^/]+?)\s*/(.*?)/", body[pos:], re.DOTALL)
        if not m:
            if body[pos:].strip(" ,"):
                u.static_problems.append(f"DATA statement not understood: {body[pos:].strip()}")
            break
        pos += m.end()
        objects = [o.strip() for o in split_top_level(m.group(1)) if o.strip()]
        values: List[str] = []
        for tok in split_top_level(m.group(2)):
            tok = tok.strip()
            rep_m = re.match(r"^(\d+)\s*\*\s*(.+)$", tok)
            values += [rep_m.group(2)] * int(rep_m.group(1)) if rep_m else [tok]
        cursor = 0
        for obj in objects:
            targets = _data_targets(u, obj)
            if targets is None:
                u.static_problems.append(f"DATA for {obj} (section, non-constant subscript or bound) is not translated")
                return
            if len(targets) == 1 and targets[0][1] is None:
                sym = targets[0][0]
                size = 1
                if sym.rank:
                    extents = _extents(sym, u)
                    if extents is None:
                        u.static_problems.append(f"{obj}: DATA for an array with non-constant bounds is not translated")
                        return
                    for n in extents:
                        size *= n
                _add_data(u, sym, values[cursor:cursor + size], obj)
                cursor += size
                continue
            for sym, index in targets:
                if cursor >= len(values):
                    break
                try:
                    partial.setdefault(sym.name, {})[index] = parse_expression(values[cursor])
                except FortranParseError as exc:
                    u.static_problems.append(f"{sym.name}: cannot parse a DATA value ({exc})")
                    return
                cursor += 1
        if cursor != len(values):
            u.static_problems.append(f"DATA statement for {', '.join(objects)}: {len(values)} value(s) for {cursor} element(s)")
    for name, elements in partial.items():
        sym = u.symbols[name]
        if sym.common_block or sym.is_dummy or sym.const_value is not None:
            u.static_problems.append(f"{name}: DATA for a COMMON member, dummy argument or constant is not translated")
            continue
        extents = _extents(sym, u)
        if extents is None:
            continue
        size = 1
        for n in extents:
            size *= n
        previous = u.data_values.get(name, [_zero_of(sym)] * size)
        u.data_values[name] = [elements.get(k, previous[k]) for k in range(size)]
        sym.saved = True


# ---------------------------------------------------------------------------------------------
# Kinds: the width of INTEGER and REAL values (32 or 64 bits). REAL is IEEE single precision, DOUBLE PRECISION and
# REAL*8 / REAL(8) / REAL(KIND=8) are double. A kind named by a PARAMETER (REAL(dp)) is resolved from that PARAMETER;
# one that cannot be resolved is -1 and the emitters fail loudly instead of guessing.
# ---------------------------------------------------------------------------------------------

_KNOWN_KIND_NAMES = {"real32": 32, "real64": 64, "int8": 8, "int16": 16, "int32": 32, "int64": 64,
                     "c_float": 32, "c_double": 64, "c_int": 32, "c_long_long": 64, "c_int32_t": 32, "c_int64_t": 64}
UNRESOLVED_KIND = -1


def _kind_value_bits(text: str, u: Optional["Unit"], depth: int = 0) -> int:
    """Bits for the value of a kind expression: 8, KIND(1.0D0), SELECTED_REAL_KIND(15), another kind PARAMETER."""
    t = text.strip().lower().replace(" ", "")
    if depth > 4:
        return UNRESOLVED_KIND
    if t.isdigit():
        return 8 * int(t)
    m = re.fullmatch(r"kind\((.*)\)", t)
    if m:
        inner = m.group(1)
        if re.fullmatch(r"[0-9.]+(?:e[+-]?\d+)?", inner) and "." not in inner and "e" not in inner:
            return 32                                   # KIND(1): default integer
        if re.search(r"\d[d]", inner):
            return 64
        suffix = re.search(r"_(\w+)$", inner)
        if suffix:
            return _kind_value_bits(suffix.group(1), u, depth + 1)
        return 32 if re.fullmatch(r"[0-9.]+(?:e[+-]?\d+)?", inner) else UNRESOLVED_KIND
    m = re.fullmatch(r"selected_real_kind\((?:p=)?(\d+).*\)", t)
    if m:
        digits = int(m.group(1))
        return 32 if digits <= 6 else 64 if digits <= 15 else UNRESOLVED_KIND
    m = re.fullmatch(r"selected_int_kind\((?:r=)?(\d+)\)", t)
    if m:
        digits = int(m.group(1))
        return 8 if digits <= 2 else 16 if digits <= 4 else 32 if digits <= 9 else 64 if digits <= 18 else UNRESOLVED_KIND
    if t in _KNOWN_KIND_NAMES:
        return _KNOWN_KIND_NAMES[t]
    if u is not None and re.fullmatch(r"[a-z_]\w*", t):
        sym = u.symbols.get(t)
        if sym is not None and sym.const_value is not None:
            return _kind_value_bits(sym.const_value, u, depth + 1)
    return UNRESOLVED_KIND


def kind_bits(f_type: str, u: Optional["Unit"] = None) -> int:
    """Width in bits of a declared type string such as 'DOUBLE PRECISION', 'REAL', 'REAL*8', 'INTEGER*DP'."""
    t = f_type.upper().strip()
    if t.startswith("DOUBLE"):
        return 64
    m = re.fullmatch(r"(REAL|INTEGER|COMPLEX|LOGICAL|CHARACTER)(?:\*(\w+))?", t)
    if not m:
        return 32
    base, k = m.groups()
    if base == "COMPLEX":
        if k is None:
            return 32                                    # COMPLEX is two single-precision REALs
        return 4 * int(k) if k.isdigit() else _kind_value_bits(k, u)    # COMPLEX*16 -> parts of 64 bits; COMPLEX(dp) -> kind dp
    if base in ("LOGICAL", "CHARACTER") or k is None:
        return 32
    return _kind_value_bits(k, u)


def literal_bits(num: "Num", u: Optional["Unit"] = None) -> int:
    if num.suffix is not None:
        return _kind_value_bits(num.suffix, u)
    if num.is_int:
        return 64 if int(num.text) > 2147483647 else 32
    return 64 if num.double else 32


KIND_INQUIRY = ("KIND", "SELECTED_REAL_KIND", "SELECTED_INT_KIND")


def kind_call_value(e: "Ref", u: "Unit", func_types: Optional[Dict[str, str]] = None,
                    units: Optional[Dict[str, "Unit"]] = None) -> int:
    """Value (a byte count, as gfortran numbers kinds) of KIND(x), SELECTED_REAL_KIND(p) or SELECTED_INT_KIND(r)."""
    key = e.name.upper()
    arg = e.args[0] if e.args else None
    if key == "KIND" and arg is not None:
        return num_bits(arg, u, func_types, units) // 8
    if isinstance(arg, Num) and arg.is_int:
        digits = int(arg.text)
        if key == "SELECTED_REAL_KIND":
            if digits <= 6:
                return 4
            if digits <= 15:
                return 8
        elif key == "SELECTED_INT_KIND":
            return 1 if digits <= 2 else 2 if digits <= 4 else 4 if digits <= 9 else 8 if digits <= 18 else -1
    raise FortranParseError(f"cannot evaluate {key}(...) at translation time")


_COMPLEX_ELEMENTAL = {"SQRT", "EXP", "LOG", "SIN", "COS", "TAN", "SINH", "COSH", "TANH", "ASIN", "ACOS", "ATAN"}
_DOUBLE_NAMED = {"DBLE", "DFLOAT", "DSQRT", "DABS", "DEXP", "DLOG", "DLOG10", "DSIN", "DCOS", "DTAN", "DASIN", "DACOS",
                 "DATAN", "DATAN2", "DSINH", "DCOSH", "DTANH", "DMOD", "DSIGN", "DDIM", "DMAX1", "DMIN1", "DNINT", "DINT",
                 "DPROD"}
_SINGLE_NAMED = {"SNGL", "FLOAT", "ALOG", "ALOG10", "AMOD", "AMAX0", "AMIN0", "AMAX1", "AMIN1"}
_INT_FROM_REAL = {"INT", "IFIX", "IDINT", "NINT", "IDNINT", "MAX1", "MIN1", "SIZE", "LEN", "FLOOR", "CEILING"}


def num_bits(expr: "Expr", u: "Unit", func_types: Optional[Dict[str, str]] = None,
             units: Optional[Dict[str, "Unit"]] = None) -> int:
    """Width of an INTEGER or REAL expression's value (0 for other types). The width of a mixed expression is the larger
    width of its operands of the result type; an operand of the other numeric type is converted to it."""
    result = infer_type(expr, u, func_types)
    if result not in (INT, REAL, COMPLEX):
        return 0

    def bits_of(e):
        return num_bits(e, u, func_types, units)

    def kind_of(e):
        return infer_type(e, u, func_types)

    def checked(value, what):
        if value == UNRESOLVED_KIND:
            raise FortranParseError(f"kind of {what} could not be resolved")
        return value

    if isinstance(expr, Component):
        comp = component_of(expr, u)
        return checked(comp.bits or 32, f"component {expr.comp}") if comp is not None else 32
    if isinstance(expr, ComplexLit):
        parts = [bits_of(x) for x in (expr.re, expr.im) if kind_of(x) == REAL]
        return max(parts) if parts else 32
    if isinstance(expr, (ArrayCtor, ImpliedDo)):
        operands = [bits_of(x) for x in _ctor_leaves(expr) if kind_of(x) == result]
        return max(operands) if operands else 32
    if isinstance(expr, Num):
        return checked(literal_bits(expr, u), f"literal {expr.text}")
    if isinstance(expr, Name):
        sym = u.sym(expr.name)
        return checked(sym.bits or 32, f"variable {expr.name}")
    if isinstance(expr, Unary):
        return bits_of(expr.operand)
    if isinstance(expr, Binary):
        if expr.op == "**" and result == REAL and kind_of(expr.left) == REAL:
            return bits_of(expr.left)
        if result == COMPLEX:
            operands = [bits_of(x) for x in (expr.left, expr.right) if kind_of(x) in (REAL, COMPLEX)]
        else:
            operands = [bits_of(x) for x in (expr.left, expr.right) if kind_of(x) == result]
        return max(operands) if operands else 32
    if isinstance(expr, Ref):
        sym = u.symbols.get(expr.name)
        key = expr.name.upper()
        if sym is not None and sym.rank:
            return checked(sym.bits or 32, f"array {expr.name}")
        user = units.get(expr.name) if units else None
        if user is not None and user.kind == "FUNCTION":
            return checked(user.symbols[user.name].bits or 32, f"function {expr.name}")
        if key in FORTRAN_INTRINSICS:
            if key in ("CMPLX", "DCMPLX"):
                if key == "DCMPLX":
                    return 64
                if len(expr.args) == 3:
                    kind_arg = expr.args[2]
                    return checked(_kind_value_bits(kind_arg.text if isinstance(kind_arg, Num) else
                                                    getattr(kind_arg, "name", ""), u), "CMPLX(x, y, kind)")
                return 32
            if key in ("AIMAG", "DIMAG", "REAL", "DREAL", "ABS", "CABS") and expr.args and kind_of(expr.args[0]) == COMPLEX \
                    and result == REAL:
                return 64 if key in ("DIMAG", "DREAL") else bits_of(expr.args[0])
            if result == REAL:
                if key in _DOUBLE_NAMED:
                    return 64
                if key in _SINGLE_NAMED:
                    return 32
                if key == "REAL" and len(expr.args) == 2:
                    kind_arg = expr.args[1]
                    return checked(_kind_value_bits(kind_arg.text if isinstance(kind_arg, Num) else
                                                    getattr(kind_arg, "name", ""), u), "REAL(x, kind)")
                if key == "REAL":
                    return 32
            else:
                if key in _INT_FROM_REAL:
                    if key in ("INT", "NINT") and len(expr.args) == 2:
                        kind_arg = expr.args[1]
                        return checked(_kind_value_bits(kind_arg.text if isinstance(kind_arg, Num) else
                                                        getattr(kind_arg, "name", ""), u), f"{key}(x, kind)")
                    return 32
            operands = [bits_of(a) for a in expr.args if kind_of(a) == result and not isinstance(a, Slice)]
            return max(operands) if operands else 32
        if sym is not None and sym.bits:
            return checked(sym.bits, f"external {expr.name}")
        return 32
    return 32


# ---------------------------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------------------------

_INT_RESULT_INTRINSICS = {"INT", "IFIX", "IDINT", "NINT", "IDNINT", "IABS", "ISIGN", "IDIM", "MAX0", "MIN0",
                          "MAX1", "MIN1", "SIZE", "LEN", "LEN_TRIM", "FLOOR", "CEILING"}
_REAL_RESULT_INTRINSICS = {"REAL", "FLOAT", "DFLOAT", "DBLE", "SNGL", "AMAX0", "AMIN0"}


def derived_name(expr: Expr, u: "Unit") -> Optional[str]:
    """The derived type an expression denotes (a variable, array element or component), else None."""
    if isinstance(expr, (Name, Ref)):
        sym = u.symbols.get(expr.name)
        if sym is not None and sym.derived:
            return sym.derived
        if isinstance(expr, Ref) and expr.name in u.types:
            return expr.name                                    # structure constructor: point(1.0, 2.0)
        return None
    if isinstance(expr, Component):
        comp = component_of(expr, u)
        return comp.derived if comp is not None else None
    return None


def component_of(expr: "Component", u: "Unit") -> Optional[TypeComponent]:
    owner = derived_name(expr.base, u)
    typedef = u.types.get(owner) if owner else None
    return typedef.get(expr.comp) if typedef is not None else None


def _ctor_leaves(expr: Expr) -> List[Expr]:
    """The element expressions of an array constructor, implied DOs flattened."""
    if isinstance(expr, ArrayCtor):
        return [leaf for item in expr.items for leaf in _ctor_leaves(item)]
    if isinstance(expr, ImpliedDo):
        return [leaf for item in expr.items for leaf in _ctor_leaves(item)]
    return [expr]


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
        if sym is None and expr.name in u.types:
            return DERIVED                                       # structure constructor: point(1.0, 2.0)
        if sym is not None and sym.rank:
            return sym.type
        if sym is not None and sym.name in func_types:
            return func_types[sym.name]
        key = expr.name.upper()
        if key in KIND_INQUIRY and expr.name not in func_types:
            return INT
        if key in FORTRAN_INTRINSICS:
            if key == "TRIM":
                return CHAR
            if key in ("ALLOCATED", "ASSOCIATED"):
                return LOGICAL
            if key in ("CONJG", "DCONJG", "CMPLX", "DCMPLX"):
                return COMPLEX
            if key in ("AIMAG", "DIMAG", "DREAL"):
                return REAL
            if key in _COMPLEX_ELEMENTAL and any(infer_type(a, u, func_types) == COMPLEX for a in expr.args):
                return COMPLEX
            if key in ("ABS", "REAL", "DBLE") and expr.args and infer_type(expr.args[0], u, func_types) == COMPLEX:
                return REAL
            if key == "HUGE" and expr.args:
                return infer_type(expr.args[0], u, func_types)
            if key in _INT_RESULT_INTRINSICS:
                return INT
            if key in _REAL_RESULT_INTRINSICS:
                return REAL
            if key in ("MOD", "MODULO", "MAX", "MIN", "ABS", "SIGN", "DIM", "SUM", "PRODUCT", "MAXVAL", "MINVAL", "DOT_PRODUCT",
                       "MATMUL", "TRANSPOSE"):
                kinds = [infer_type(a, u, func_types) for a in expr.args] or [REAL]
                return INT if all(k == INT for k in kinds) else REAL
            return REAL
        if expr.name in func_types:
            return func_types[expr.name]
        return u.implicit_type(expr.name)
    if isinstance(expr, Component):
        comp = component_of(expr, u)
        if comp is None:
            raise FortranParseError(f"unknown component {expr.comp}")
        return comp.type
    if isinstance(expr, Ref) and expr.name in u.types and expr.name not in u.symbols:
        return DERIVED                                           # structure constructor
    if isinstance(expr, ComplexLit):
        return COMPLEX
    if isinstance(expr, (ArrayCtor, ImpliedDo)):
        kinds = [infer_type(x, u, func_types) for x in _ctor_leaves(expr)] or [REAL]
        for wanted in (COMPLEX, REAL):
            if wanted in kinds:
                return wanted
        return kinds[0]
    if isinstance(expr, Unary):
        return LOGICAL if expr.op == "not" else infer_type(expr.operand, u, func_types)
    if isinstance(expr, Binary):
        if expr.op in ("==", "!=", "<", "<=", ">", ">=", "and", "or", "eqv", "neqv"):
            return LOGICAL
        if expr.op == "//":
            return CHAR
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
    elif isinstance(expr, Component):
        _names_in(expr.base, out)
        for a in expr.args or []:
            _names_in(a, out)
    elif isinstance(expr, ArrayCtor):
        for item in expr.items:
            _names_in(item, out)
    elif isinstance(expr, ImpliedDo):
        for item in expr.items + [expr.lo, expr.hi] + ([expr.step] if expr.step is not None else []):
            _names_in(item, out)
        out.add(expr.var)
    elif isinstance(expr, ComplexLit):
        _names_in(expr.re, out)
        _names_in(expr.im, out)


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
    if st == "SELECT_CASE":
        return [d["expr"]]
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


def format_io(raw: str):
    """Formatted PRINT / WRITE to the default unit. Returns (fmt, items) with fmt = ("string", text), ("label", digits)
    or ("expr", text), and items the output list as strings; None if the statement is not of that form (list-directed
    output, other units, IOSTAT=, ADVANCE=, ...)."""
    def classify(spec: str):
        spec = spec.strip()
        if spec.lower().startswith("fmt="):
            spec = spec[4:].strip()
        if spec == "*":
            return None
        if len(spec) >= 2 and spec[0] in "'\"" and spec[-1] == spec[0]:
            return ("string", spec[1:-1].replace(spec[0] * 2, spec[0]))
        if spec.isdigit():
            return ("label", spec)
        return ("expr", spec)

    m = re.match(r"^PRINT\s*(.*)$", raw, re.IGNORECASE | re.DOTALL)
    if m and not raw.upper().startswith("PRINT *"):
        parts = split_top_level(m.group(1))
        fmt = classify(parts[0]) if parts else None
        return (fmt, parts[1:]) if fmt else None
    m = re.match(r"^WRITE\s*\(", raw, re.IGNORECASE)
    if not m:
        return None
    depth, end = 0, None
    for k in range(m.end() - 1, len(raw)):
        depth += raw[k] == "("
        depth -= raw[k] == ")"
        if depth == 0:
            end = k
            break
    if end is None:
        return None
    controls = [c.strip() for c in split_top_level(raw[m.end():end])]
    if len(controls) != 2:
        return None
    unit = controls[0][5:] if controls[0].lower().startswith("unit=") else controls[0]
    if unit.strip() not in ("*", "6"):
        return None
    fmt = classify(controls[1])
    return (fmt, split_top_level(raw[end + 1:])) if fmt else None


def classify_format(spec: str):
    """A format specifier as ("string", text) | ("label", digits) | ("expr", text); None for list-directed (*)."""
    spec = spec.strip()
    if spec.lower().startswith("fmt="):
        spec = spec[4:].strip()
    if spec == "*":
        return None
    if len(spec) >= 2 and spec[0] in "'\"" and spec[-1] == spec[0]:
        return ("string", spec[1:-1].replace(spec[0] * 2, spec[0]))
    if spec.isdigit():
        return ("label", spec)
    return ("expr", spec)


def io_statement(raw: str):
    """Any I/O statement: {"kind", "unit", "fmt", "controls", "items"} (texts); None if it cannot be parsed.

    unit is "*" for the default unit; fmt is the format specifier text (None for none given, "*" for list-directed);
    controls holds the keyword specifiers in lower case (iostat, end, err, file, status, action, exist, ...)."""
    m = re.match(r"^(READ|WRITE|PRINT|OPEN|CLOSE|REWIND|BACKSPACE|ENDFILE|INQUIRE|FLUSH)\b\s*(.*)$", raw, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    kind, rest = m.group(1).upper(), m.group(2).strip()
    controls: Dict[str, str] = {}
    positional: List[str] = []
    if rest.startswith("("):
        depth, end = 0, None
        for k, ch in enumerate(rest):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0:
                end = k
                break
        if end is None:
            return None
        for part in split_top_level(rest[1:end]):
            part = part.strip()
            kv = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.*)$", part, re.DOTALL)
            if kv and not part.startswith(("'", '"')):
                controls[kv.group(1).lower()] = kv.group(2).strip()
            elif part:
                positional.append(part)
        unit = controls.pop("unit", positional[0] if positional else None)
        fmt = controls.pop("fmt", positional[1] if len(positional) > 1 else None)
        items = split_top_level(rest[end + 1:]) if rest[end + 1:].strip() else []
    elif kind in ("REWIND", "BACKSPACE", "ENDFILE", "CLOSE") and rest:
        return {"kind": kind, "unit": rest, "fmt": None, "controls": {}, "items": []}      # REWIND 10
    else:
        parts = split_top_level(rest)
        unit, fmt, items = "*", (parts[0] if parts else None), parts[1:]
        if kind not in ("READ", "PRINT"):
            return None
    return {"kind": kind, "unit": unit, "fmt": fmt, "controls": controls, "items": [i.strip() for i in items if i.strip()]}


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
    elif isinstance(tree, Component):
        _register_tree(u, tree.base)
        for a in tree.args or []:
            _register_tree(u, a)
    elif isinstance(tree, ArrayCtor):
        for item in tree.items:
            _register_tree(u, item)
    elif isinstance(tree, ImpliedDo):
        for item in tree.items + [tree.lo, tree.hi] + ([tree.step] if tree.step is not None else []):
            _register_tree(u, item)
        u.sym(tree.var)
    elif isinstance(tree, ComplexLit):
        _register_tree(u, tree.re)
        _register_tree(u, tree.im)


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
                    if sym and sym.is_dummy and not sym.rank and n not in modified and sym.intent != "in" and sym.type != DERIVED:
                        modified.append(n)
        for p in u.params:
            sym = u.symbols[p]
            if sym.is_dummy and not sym.rank and sym.type != DERIVED and sym.intent in ("out", "inout") and p not in modified:
                modified.append(p)
        u.modified_scalars = [p for p in u.params if p in modified]
        u.out_only = [p for p in u.params if u.symbols[p].intent == "out" and not u.symbols[p].rank and u.symbols[p].type != DERIVED]
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
                    if sym and sym.is_dummy and not sym.rank and sym.type != DERIVED and sym.intent != "in" and n not in u.modified_scalars:
                        u.modified_scalars = [p for p in u.params if p in set(u.modified_scalars) | {n}]
                        changed = True

    changed = True
    while changed:
        changed = False
        for u in units:
            for callee in u.calls:
                cu = by_name.get(callee)
                if cu and not (cu.commons - cu.owned_blocks) <= u.commons:
                    u.commons |= cu.commons - cu.owned_blocks
                    changed = True


def const_int(text: str, u: Unit, depth: int = 0) -> Optional[int]:
    """Integer value of a constant expression (literals, PARAMETER names, + - * /), or None."""
    try:
        return _eval_int(parse_expression(text), u, depth)
    except FortranParseError:
        return None


def _eval_int(e: Expr, u: Unit, depth: int, env: Optional[Dict[str, int]] = None) -> Optional[int]:
    """Integer value of a constant expression; `env` gives implied-DO variables their current value."""
    if depth > 6:
        return None
    if isinstance(e, Num):
        return int(e.text) if e.is_int else None
    if isinstance(e, Name):
        if env and e.name in env:
            return env[e.name]
        sym = u.symbols.get(e.name)
        return const_int(sym.const_value, u, depth + 1) if sym is not None and sym.const_value is not None else None
    if isinstance(e, Unary) and e.op in ("-", "+"):
        v = _eval_int(e.operand, u, depth, env)
        return None if v is None else (-v if e.op == "-" else v)
    if isinstance(e, Binary) and e.op in ("+", "-", "*", "/"):
        a, b = _eval_int(e.left, u, depth, env), _eval_int(e.right, u, depth, env)
        if a is None or b is None:
            return None
        if e.op == "/":
            return None if b == 0 else abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)
        return a + b if e.op == "+" else a - b if e.op == "-" else a * b
    return None


def common_extents(sym: Symbol, u: Unit) -> Optional[List[int]]:
    """Constant extent of each dimension of a COMMON member, or None when one is not constant."""
    out = []
    for lb, ub in sym.dims:
        lo, hi = const_int(lb, u), const_int(ub, u)
        if lo is None or hi is None:
            return None
        out.append(max(hi - lo + 1, 0))
    return out


def _has_pointer_components(name: str, types: Dict[str, "TypeDef"], seen: Optional[Set[str]] = None) -> bool:
    seen = seen or set()
    typedef = types.get(name)
    if typedef is None or name in seen:
        return False
    seen.add(name)
    return any(c.pointer or (c.derived and _has_pointer_components(c.derived, types, seen)) for c in typedef.components)


def _link_attributes(u: Unit) -> None:
    """ALLOCATABLE / POINTER statements, and the lower bounds that ALLOCATE statements give (fixed per variable)."""
    for st in u.stmts:
        if st.stmt_type == "ATTRIBUTE" and st.details["attr"] == "target":
            for decl in st.details["names"]:
                try:
                    u.sym(_split_decl_item(decl)[0]).target = True
                except FortranParseError:
                    pass
        if st.stmt_type == "ATTRIBUTE" and st.details["attr"] in ("allocatable", "pointer"):
            for decl in st.details["names"]:
                try:
                    name, dim, _ = _split_decl_item(decl)
                except FortranParseError:
                    continue
                sym = u.sym(name)
                if dim:
                    sym.dims = _parse_dim_spec(dim)
                setattr(sym, st.details["attr"], True)
    for sym in u.symbols.values():
        if sym.derived and _has_pointer_components(sym.derived, u.types):
            u.static_problems.append(f"{sym.name}: type {sym.derived} has POINTER or ALLOCATABLE components, which are not translated")
        if (sym.allocatable or sym.pointer) and sym.rank and sym.type == DERIVED and sym.rank != 1:
            u.static_problems.append(f"{sym.name}: allocatable arrays of derived type are translated for rank 1 only")
    for st in u.stmts:
        if st.stmt_type != "ALLOCATE":
            continue
        for obj in st.details["objects"]:
            try:
                tree = parse_expression(obj)
            except FortranParseError:
                continue
            sym = u.symbols.get(getattr(tree, "name", ""))
            if not isinstance(tree, Ref) or sym is None or not (sym.allocatable or sym.pointer):
                continue
            for d, arg in enumerate(tree.args[: len(sym.dims)]):
                lo = arg.lo if isinstance(arg, Slice) else Num("1", True)
                if isinstance(lo, Unary) and lo.op == "-" and isinstance(lo.operand, Num):
                    text = "-" + lo.operand.text
                elif isinstance(lo, Num) and lo.is_int:
                    text = lo.text
                else:
                    u.static_problems.append(f"{sym.name}: a lower bound that is not an integer literal in ALLOCATE is not translated")
                    continue
                if getattr(sym, "_alloc_lb", None) is None:
                    sym._alloc_lb = {}
                previous = sym._alloc_lb.get(d)
                if previous is not None and previous != text:
                    u.static_problems.append(f"{sym.name}: ALLOCATE with different lower bounds is not translated")
                sym._alloc_lb[d] = text
                sym.dims[d] = (text, sym.dims[d][1])


def _link_equivalence(u: Unit) -> None:
    """EQUIVALENCE: variables that share storage. Members must have one type and kind (no type punning). Each connected
    set of members gets one storage of `size` elements; a member sits at element offset `base`."""
    edges: Dict[str, List[Tuple[str, int]]] = {}
    for st in u.stmts:
        if st.stmt_type != "EQUIVALENCE":
            continue
        for group in st.details["sets"]:
            located: List[Tuple[Symbol, int]] = []
            for obj in group:
                try:
                    tree = parse_expression(obj)
                except FortranParseError:
                    tree = None
                if isinstance(tree, Name):
                    sym, offset = u.sym(tree.name), 0
                elif isinstance(tree, Ref) and tree.name in u.symbols and u.symbols[tree.name].rank:
                    sym = u.symbols[tree.name]
                    offset = _flat_index(sym, tree.args, u, {})
                else:
                    offset, sym = None, None
                if sym is None or offset is None:
                    u.static_problems.append(f"EQUIVALENCE of {obj} (non-constant subscript or unsupported form) is not translated")
                    return
                if sym.is_dummy or sym.common_block or sym.const_value is not None or sym.module:
                    u.static_problems.append(f"EQUIVALENCE of {sym.name} (dummy, COMMON, module or constant) is not translated")
                    return
                located.append((sym, offset))
            first = located[0]
            for sym, offset in located[1:]:
                if (sym.type, sym.bits) != (first[0].type, first[0].bits) or sym.type in (CHAR, COMPLEX):
                    u.static_problems.append(f"EQUIVALENCE of {first[0].name} and {sym.name}: different types or kinds "
                                             "(type punning) are not translated")
                    return
                # base(sym) + offset = base(first) + first_offset
                edges.setdefault(first[0].name, []).append((sym.name, first[1] - offset))
                edges.setdefault(sym.name, []).append((first[0].name, offset - first[1]))
    seen: Dict[str, int] = {}
    for start in list(edges):
        if start in seen:
            continue
        base: Dict[str, int] = {start: 0}
        queue = [start]
        while queue:
            node = queue.pop()
            for other, delta in edges[node]:
                if other not in base:
                    base[other] = base[node] + delta
                    queue.append(other)
                elif base[other] != base[node] + delta:
                    u.static_problems.append("EQUIVALENCE is inconsistent (the same storage would overlap at two offsets)")
                    return
        low = min(base.values())
        size = 0
        for name, b in base.items():
            sym = u.symbols[name]
            count = 1
            if sym.rank:
                extents = common_extents(sym, u)
                if extents is None:
                    u.static_problems.append(f"EQUIVALENCE of {name}: the array bounds are not constant")
                    return
                for n in extents:
                    count *= n
            size = max(size, b - low + count)
        gid = len(u.eq_groups)
        first_sym = u.symbols[start]
        u.eq_groups.append({"type": first_sym.type, "bits": first_sym.bits, "size": size, "members": sorted(base)})
        for name, b in base.items():
            u.symbols[name].eq = (gid, b - low)
            seen[name] = gid


def _identifiers(u: Unit) -> Set[str]:
    """Every identifier the unit's statements mention (outside string literals), lower case."""
    names: Set[str] = set()
    for st in u.stmts[1:] if u.stmts and u.stmts[0].stmt_type in ("SUBROUTINE", "FUNCTION", "PROGRAM") else u.stmts:
        text = re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", " ", st.raw)
        names.update(m.lower() for m in re.findall(r"[A-Za-z_]\w*", text))
    return names


def _link_internal_procedures(units: List[Unit]) -> None:
    """Host association: an internal procedure sees the variables its host declares.

    Host constants are copied; host variables the internal procedures use move into a block the host owns and the
    internal procedures receive (the machinery of COMMON), so both see the same storage. A name the internal procedure
    declares itself hides the host's."""
    import copy
    by_name = {u.name: u for u in units}
    for host in units:
        inners = [u for u in units if u.host_unit == host.name]
        if not inners:
            continue
        shared: List[str] = []
        for inner in inners:
            used = _identifiers(inner)
            for name, hsym in list(host.symbols.items()):
                local = inner.symbols.get(name)
                if name not in used or not (hsym.explicit_type or hsym.rank):
                    continue
                if local is not None and (local.explicit_type or local.rank or local.is_dummy):
                    continue                                   # declared in the internal procedure: it hides the host's
                if hsym.const_value is not None or hsym.module or hsym.common_block:
                    inner.symbols[name] = copy.copy(hsym)       # constants, module and COMMON entities are visible as they are
                    inner.symbols[name].host_const = hsym.const_value is not None and not hsym.module
                    continue
                if hsym.is_dummy:
                    inner.static_problems.append(f"{name}: the host's dummy argument used by an internal procedure is "
                                                 "not translated")
                    continue
                if hsym.saved or name in host.data_values:
                    inner.static_problems.append(f"{name}: a host variable with DATA/SAVE used by an internal procedure "
                                                 "is not translated")
                    continue
                if name not in shared:
                    shared.append(name)
        if not shared:
            continue
        block = f"HOST_{host.name.upper()}"
        host.owned_blocks.add(block)
        host.common_layout[block] = list(shared)
        for name in shared:
            host.symbols[name].common_block = block
        for inner in inners:
            for name in shared:
                dup = copy.copy(host.symbols[name])
                dup.is_dummy = False
                inner.symbols[name] = dup
            inner.common_layout[block] = list(shared)


def _rename_identifiers(u: Unit, renames: Dict[str, str]) -> None:
    """USE m, local => remote: the unit's statements say `remote` wherever they said `local` (outside string literals)."""
    if not renames:
        return
    pattern = re.compile(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")|\b(" + "|".join(map(re.escape, renames)) + r")\b", re.IGNORECASE)

    def sub(text: str) -> str:
        return pattern.sub(lambda m: m.group(1) if m.group(1) else renames[m.group(2).lower()], text)

    def walk(value):
        if isinstance(value, str):
            return sub(value)
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        return value

    for st in u.stmts:
        if st.stmt_type == "USE":
            continue
        st.raw, st.clean, st.details = sub(st.raw), sub(st.clean), walk(st.details)


def _build_typedef(definition: dict, known: Dict[str, "TypeDef"]) -> "TypeDef":
    """Components of a derived type from the declarations between TYPE and END TYPE."""
    comps: List[TypeComponent] = []
    for st in definition["stmts"]:
        if st.stmt_type != "TYPE_DECL":
            continue
        d = st.details
        t = base_type(d["type"])
        for item in split_top_level(d["declarations"]):
            try:
                name, dim, init = _split_decl_item(item)
            except FortranParseError:
                continue
            dims = _parse_dim_spec(dim or d.get("attr_dimension") or "") if (dim or d.get("attr_dimension")) else []
            derived = re.match(r"^TYPE\((\w+)\)$", d["type"], re.IGNORECASE)
            comps.append(TypeComponent(name, t, kind_bits(d["type"], None), dims,
                                       derived.group(1).lower() if derived else None, init,
                                       bool({"pointer", "allocatable"} & set(d.get("attrs", [])))))
    return TypeDef(definition["name"], comps)


def _import_modules(units: List[Unit]) -> None:
    """Make module symbols visible: by host association in the module's own procedures and through USE elsewhere.

    A module PARAMETER is always imported (array bounds may refer to it); a variable only when USE has no ONLY list or
    names it. A local declaration of the same name hides the module's."""
    import copy
    modules = {u.name: u for u in units if u.kind == "MODULE"}
    for module in modules.values():
        for name, sym in module.symbols.items():
            sym.module = module.name
            if sym.const_value is None and not (sym.type == INT and False):
                sym.saved = True                 # module variables have static storage
    for u in units:
        wanted: List[Tuple[str, Optional[List[str]]]] = []
        if u.host_module:
            wanted.append((u.host_module, None))
        for s in u.stmts:
            if s.stmt_type == "USE":
                _rename_identifiers(u, s.details["renames"])
                wanted.append((s.details["module"], s.details["only"]))
        for mod_name, only in wanted:
            module = modules.get(mod_name)
            if module is None:
                u.static_problems.append(f"USE {mod_name}: the module is not defined in this file")
                continue
            for name, sym in module.symbols.items():
                if name in u.symbols or (only is not None and name not in only and sym.const_value is None):
                    continue
                imported = copy.copy(sym)
                imported.saved = False           # the storage lives in the module, not in this unit
                imported.is_dummy = False
                u.symbols[name] = imported


def link_common_blocks(units: List[Unit]) -> Dict[str, Tuple[Unit, List[str]]]:
    """Associate COMMON members by position, as Fortran does, whatever each unit calls them.

    The unit that declares the most members of a block defines its fields; in every other unit each member is
    aliased to the field in the same position (Symbol.common_field). A member whose type, kind or shape differs from
    the field is recorded in Unit.common_conflicts: that storage association is not translated. Returns
    block -> (defining unit, member names)."""
    declared: Dict[str, List[Tuple[Unit, List[str]]]] = {}
    for u in units:
        u.common_conflicts = []
        for blk, names in u.common_layout.items():
            declared.setdefault(blk, []).append((u, names))
    layout: Dict[str, Tuple[Unit, List[str]]] = {}
    for blk, decls in declared.items():
        canon_unit, canon_names = max(decls, key=lambda d: len(d[1]))
        layout[blk] = (canon_unit, canon_names)
        for u, names in decls:
            for pos, name in enumerate(names):
                sym, field_sym = u.symbols[name], canon_unit.symbols[canon_names[pos]]
                sym.common_field = canon_names[pos]
                shape, field_shape = common_extents(sym, u), common_extents(field_sym, canon_unit)
                problem = None
                if sym.type != field_sym.type or (sym.type in (INT, REAL) and sym.bits != field_sym.bits):
                    problem = f"type/kind differs from {canon_unit.name}'s {field_sym.name}"
                elif shape is None or field_shape is None:
                    problem = "dimension is not a constant"
                elif shape != field_shape:
                    problem = f"shape {shape} differs from {canon_unit.name}'s {field_sym.name} {field_shape}"
                if problem:
                    u.common_conflicts.append(f"COMMON /{blk}/ member {pos + 1} ({name}): {problem}")
    return layout


def function_result_types(units: List[Unit]) -> Dict[str, str]:
    return {u.name: u.result_type for u in units if u.kind == "FUNCTION"}


def lex_and_split(source_text: str) -> Tuple[List[FortranStatement], List[Unit]]:
    stmts = lex_fortran_source(source_text)
    return stmts, split_units(stmts)
