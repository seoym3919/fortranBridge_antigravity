"""The C++ side of identity_checker.py: read a translated C++ file through clang's AST.

The AST is the compiler's own reading of the code, so every conversion is explicit: an implicit `int` to `double` is a node,
and a C++ kind never has to be inferred. A scratch copy of the file is wrapped in one namespace after its #includes so the dump
holds the translation and not the standard library. Needs clang (`clang++`, or the command in FORTRANBRIDGE_CXX, which may be
`python -m ziglang c++`). g++ cannot dump this AST.

As on the Python side nothing here imports the translator. A construct not recognised below is a failure, never a pass.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

import identity_checker as ic
from identity_checker import Unchecked, fold, fold_call, num

NAMESPACE = "fbu_ported_code"
# C++ call -> intrinsic, as PY_CALLS.
CPP_CALLS = {
    "sqrt": "sqrt", "exp": "exp", "log": "log", "log10": "log10", "sin": "sin", "cos": "cos", "tan": "tan", "asin": "asin", "acos": "acos",
    "atan": "atan", "atan2": "atan2", "sinh": "sinh", "cosh": "cosh", "tanh": "tanh", "fabs": "abs", "abs": "abs", "fmod": "mod", "floor": "floor",
    "ceil": "ceiling", "trunc": "aint", "round": "anint", "fortran_modulo": "modulo", "fortran_matmul": "matmul", "fortran_transpose": "transpose",
    "conj": "conjg", "fortran_trim": "trim", "min": "min", "max": "max", "copysign": "sign",
}
CPP_OPERATORS = {"fortran_powi": "**", "fortran_ipow": "**", "pow": "**", "fortran_cdiv": "/"}
OPERATORS = {"+": "+", "-": "-", "*": "*", "/": "/", "<": "<", "<=": "<=", ">": ">", ">=": ">=", "==": "==", "!=": "/=", "&&": "and", "||": "or"}
KINDS = {"double": "r8", "float": "r4", "int": "i4", "long long": "i8", "bool": "l4", "std::complex<double>": "c8", "std::complex<float>": "c4"}
TRANSPARENT_CASTS = {"LValueToRValue", "NoOp", "FunctionToPointerDecay", "ArrayToPointerDecay", "ConstructorConversion", "UserDefinedConversion"}
_ARRAY_INDEX = re.compile(r"^k\d+_\d+$")


def compiler() -> List[str]:
    command = shlex.split(os.environ.get("FORTRANBRIDGE_CXX", ""))
    if command:
        return command
    found = shutil.which("clang++")
    if found:
        return [found]
    raise Unchecked("reading C++ needs clang: install clang++, or set FORTRANBRIDGE_CXX to a clang-based command")


def wrapped(text: str) -> str:
    lines = text.split("\n")
    includes = [k for k, line in enumerate(lines) if line.startswith("#include")]
    if not includes:
        raise Unchecked("the C++ file has no #include to anchor on")
    lines.insert(includes[-1] + 1, f"namespace {NAMESPACE} {{")
    lines.append("}")
    return "\n".join(lines) + "\n"


def dump_cpp(path: Path) -> List[dict]:
    """The top-level declarations of a translated file."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "port.cpp"
        copy.write_text(wrapped(Path(path).read_text(encoding="utf-8")), encoding="utf-8")
        done = subprocess.run([*compiler(), "-std=c++17", "-fsyntax-only", "-Xclang", "-ast-dump=json", "-Xclang",
                               f"-ast-dump-filter={NAMESPACE}", str(copy)], capture_output=True, text=True, cwd=tmp)
    errors = [line for line in done.stderr.splitlines() if "error:" in line and "failed to rename" not in line]
    if errors:
        raise Unchecked(f"clang: {errors[0][:300]}")
    text = done.stdout.lstrip()
    if not text:
        raise Unchecked("clang printed no AST (is this compiler clang?)")
    root, _ = json.JSONDecoder().raw_decode(text)
    return root.get("inner", [])


def _kids(node: dict) -> List[dict]:
    return [child for child in node.get("inner", []) if child]


def _strip(node: dict) -> dict:
    """Through the nodes that only carry a value along."""
    while True:
        kind = node.get("kind")
        if kind in ("ExprWithCleanups", "MaterializeTemporaryExpr", "CXXBindTemporaryExpr", "ParenExpr", "ConstantExpr"):
            node = _kids(node)[0]
        elif kind in ("ImplicitCastExpr",) and node.get("castKind") in TRANSPARENT_CASTS:
            node = _kids(node)[0]
        elif kind in ("CXXFunctionalCastExpr", "CXXStaticCastExpr", "CStyleCastExpr") and node.get("castKind") in ("NoOp", "ConstructorConversion"):
            node = _kids(node)[0]
        elif kind == "CXXConstructExpr" and len(_kids(node)) == 1 and _kids(node)[0].get("kind") in ("MaterializeTemporaryExpr", "CXXConstructExpr", "ExprWithCleanups") \
                and not str(node.get("type", {}).get("qualType", "")).startswith("std::basic_string"):
            node = _kids(node)[0]
        elif kind == "CXXConstructExpr" and len(_kids(node)) == 1 and "vector" in str(node.get("type", {}).get("qualType", "")):
            node = _kids(node)[0]           # a vector returned by moving it
        else:
            return node


def _type(node: dict) -> str:
    kind = node.get("type", {})
    return kind.get("desugaredQualType") or kind.get("qualType", "")


def _kind(type_text: str) -> Optional[str]:
    text = re.sub(r"\b(?:const|volatile|static)\b|&", "", type_text).strip()
    return KINDS.get(text)


def _name_of_callee(node: dict) -> str:
    node = _strip(node)
    reference = node.get("referencedDecl")
    if reference:
        return reference.get("name", "")
    return node.get("name", "") or node.get("referencedMemberDecl", "")


class CppReader:
    def __init__(self, proc: ic.Proc, classes: Dict[str, List[str]]):
        self.proc = proc
        self.classes = classes
        self.used_parameters: set = set()
        self.values: Dict[str, tuple] = {}          # temporaries: name -> the value they hold
        self.buffers: Dict[str, Tuple[tuple, tuple]] = {}   # array statement buffers: name -> (loops, value)
        self.declared: Dict[str, dict] = {}         # local variable -> its VarDecl
        self.loops: List[Tuple[str, tuple]] = []    # the array-statement loops around the current expression
        self.static: Dict[str, dict] = {}
        self.keep_shape = False                     # read an expression without folding it
        self.reads: Dict[str, dict] = {}

    # ---------------------------------------------------------------------------------------- expressions
    def expr(self, node: dict) -> tuple:
        node = _strip(node)
        kind = node.get("kind")
        handler = getattr(self, "x_" + str(kind), None)
        if handler is None:
            raise Unchecked(f"C++ expression not in the table: {kind}")
        return handler(node)

    def x_IntegerLiteral(self, node):
        return num(int(node["value"]))

    def x_FloatingLiteral(self, node):
        wrap = np.float32 if _kind(_type(node)) == "r4" else np.float64
        return num(wrap(float(node["value"])))

    def x_CXXBoolLiteralExpr(self, node):
        return ("bool", bool(node["value"]))

    def x_StringLiteral(self, node):
        return ("str", json.loads(node["value"]))

    def x_CXXNullPtrLiteralExpr(self, node):
        return ("null",)

    def x_DeclRefExpr(self, node):
        name = node.get("referencedDecl", {}).get("name", "")
        if name in self.values:
            return self.values[name]
        return self.name(name)

    def x_CXXDefaultArgExpr(self, node):
        return ("default",)

    def x_CXXNewExpr(self, node):
        """new T(): a target for a pointer. A vector carries its extents."""
        made = _strip(_kids(node)[0]) if _kids(node) else {}
        args = _kids(made)
        if "vector" in str(node.get("type", {}).get("qualType", "")) and args:
            return ("new", (self.expr(args[0]),))
        return ("new", ())

    def x_ConditionalOperator(self, node):
        """SIGN(a, b) of integers: (b >= 0) ? abs(a) : -abs(a)."""
        test, yes, no = (self.expr(part) for part in _kids(node))
        if test[:2] == ("bin", ">=") and test[3] == num(0) and no == fold(("un", "-", yes)) and yes[:2] == ("call", "abs"):
            return ("call", "sign", (yes[2][0], test[2]))
        if test == ("bool", True) or test == ("bool", False):
            return yes if test[1] else no
        raise Unchecked("C++ conditional expression not in the table")

    def name(self, name: str) -> tuple:
        low = name.lower()
        sym = self.proc.symbols.get(low.rstrip("_")) or self.proc.symbols.get(low) or self.proc.module_parameters.get(low)
        if sym is None and low in self.proc.host_symbols and self.proc.host_symbols[low].parameter:
            sym = self.proc.host_symbols[low]
        if sym is None:
            sym = next((s for n, s in self.proc.symbols.items() if s.parameter and low.endswith("_" + n)), None)
        if sym is not None and sym.parameter and sym.value is not None:
            self.used_parameters.add(low)
            return sym.value
        return ("var", low)

    def x_ImplicitCastExpr(self, node):
        return self.cast(node)

    def x_CXXStaticCastExpr(self, node):
        return self.cast(node)

    def x_CStyleCastExpr(self, node):
        return self.cast(node)

    def x_CXXFunctionalCastExpr(self, node):
        return self.cast(node)

    def cast(self, node: dict) -> tuple:
        inner = self.expr(_kids(node)[0])
        target = _kind(_type(node))
        if target is None:
            return inner                    # to an index type: an index is never a rounding point
        return fold(("conv", target, inner))

    def x_BinaryOperator(self, node):
        left, right = _kids(node)
        op = node["opcode"]
        if op == "%":
            return fold_call("mod", (self.expr(left), self.expr(right)))      # C++ % truncates like Fortran's MOD
        if op not in OPERATORS:
            raise Unchecked(f"C++ operator not in the table: {op}")
        a, b = self.expr(left), self.expr(right)
        if self.keep_shape:
            return ("bin", OPERATORS[op], a, b)
        if a[0] == "ccmp" and b == num(0):
            return ("bin", OPERATORS[op], a[1], a[2])
        if op in ("==", "!=") and _boolean(a) and _boolean(b):
            a, b = a[2] if a[0] == "conv" else a, b[2] if b[0] == "conv" else b       # C++ compares bools as ints
        if b == ("null",):
            if op != "!=":
                raise Unchecked("C++ comparison with nullptr not in the table")
            return ("call", "associated", (a,))
        return fold(("bin", OPERATORS[op], a, b))

    def x_UnaryOperator(self, node):
        inner = _kids(node)[0]
        op = node["opcode"]
        if op == "-":
            return fold(("un", "-", self.expr(inner)))
        if op == "+":
            return self.expr(inner)
        if op == "!":
            value = self.expr(inner)
            return ("call", "associated", (value[1],)) if value[0] == "empty" else ("un", "not", value)
        if op == "*":
            value = _strip(inner)
            if value.get("kind") == "CallExpr" and _name_of_callee(_kids(value)[0]) in ("max_element", "min_element"):
                return self.reduction(_name_of_callee(_kids(value)[0]), _kids(value)[1:])
            return self.expr(inner)         # a pointer's target: Fortran does not write the dereference
        if op == "&":
            return self.expr(inner)
        raise Unchecked(f"C++ operator not in the table: {op}")

    def x_ParenExpr(self, node):
        return self.expr(_kids(node)[0])

    def x_CXXOperatorCallExpr(self, node):
        parts = _kids(node)
        name = _name_of_callee(parts[0])
        if name == "operator[]":
            return self.subscript(node)
        if name == "operator()" and _strip(parts[1]).get("kind") == "LambdaExpr":
            return self.lambda_value(_strip(parts[1]))
        if name == "operator+" and len(parts) == 3:
            return ("bin", "//" if "string" in _type(node) else "+", self.expr(parts[1]), self.expr(parts[2]))
        if name == "operator*" and len(parts) == 2:
            inner = _strip(parts[1])
            if inner.get("kind") == "CallExpr" and _name_of_callee(_kids(inner)[0]) in ("max_element", "min_element"):
                return self.reduction(_name_of_callee(_kids(inner)[0]), _kids(inner)[1:])
            raise Unchecked("C++ dereference not in the table")
        if name in ("operator-", "operator*", "operator/") and len(parts) == 3:
            return fold(("bin", name[-1], self.expr(parts[1]), self.expr(parts[2])))
        if name == "operator-" and len(parts) == 2:
            return fold(("un", "-", self.expr(parts[1])))
        if name in ("operator==", "operator!="):
            return ("bin", "==" if name.endswith("==") else "/=", self.expr(parts[1]), self.expr(parts[2]))
        raise Unchecked(f"C++ operator call not in the table: {name}")

    def subscript(self, node: dict) -> tuple:
        """a[i - 1][j - 1]: the array, then Fortran subscripts (index + lower bound)."""
        raw: List[dict] = []
        at = node
        while _strip(at).get("kind") == "CXXOperatorCallExpr" and _name_of_callee(_kids(_strip(at))[0]) == "operator[]":
            parts = _kids(_strip(at))
            raw.insert(0, parts[2])
            at = parts[1]
        base = self.expr(at)
        if base[0] == "comp":
            return ("idxc", base, tuple(ic._plus(self.expr(sub), 1) for sub in raw))
        if base[0] == "idx":
            raise Unchecked("C++ subscript of an element")
        if base[0] != "var":
            return ("elemof", base)         # an element of a computed temporary: the whole value stands for it
        sym = self.proc.symbols.get(base[1])
        lower = self.proc.allocated_lower.get(base[1]) or (sym.lower if sym else [])
        subs = [ic._plus(self.expr(sub), lower[d] if d < len(lower) else 1) for d, sub in enumerate(raw)]
        return ("idx", base[1], tuple(subs))

    def x_CallExpr(self, node):
        parts = _kids(node)
        name = _name_of_callee(parts[0])
        if name in ("accumulate", "inner_product", "max_element", "min_element"):
            return self.reduction(name, parts[1:])
        if name in ("epsilon", "max", "min") and len(parts) == 1:
            wrap = np.float32 if _kind(_type(node)) == "r4" else np.float64
            if _kind(_type(node)) in ("i4",):
                return num(2147483647)
            return num(wrap(getattr(np.finfo(wrap), {"epsilon": "eps", "max": "max", "min": "tiny"}[name])))
        args = [self.expr(a) for a in parts[1:] if not _common_object(a)]
        args = [a for a in args if a != ("default",)]
        if name in ("max", "min") and len(args) == 1 and args[0][0] == "ctor":
            args = list(args[0][1])         # std::max({a, b, c})
        if name in CPP_OPERATORS and len(args) == 2:
            return fold(("bin", CPP_OPERATORS[name], _index(args[0]) if name == "fortran_ipow" else args[0], _index(args[1])))
        if name == "fortran_open":
            return ("fopen", tuple(args))
        if name == "fortran_close":
            return ("fclose", tuple(args))
        if name == "fortran_rewind":
            return ("frewind", tuple(args))
        if name == "fortran_exists":
            return ("fexists", tuple(args))
        if name == "fortran_fix" and len(args) == 2:
            return ("cfix", args[0], args[1])
        if name == "fortran_cmp" and len(args) == 2:
            return ("ccmp", args[0], args[1])
        if name in CPP_CALLS:
            return fold_call(CPP_CALLS[name], [_index(a) if name.startswith("fortran_") else a for a in args])
        if name.startswith(("fortran_", "__")) or not name:
            raise Unchecked(f"C++ call not in the table: {name}")
        return ("call", name.lower(), tuple(args))

    def x_CXXMemberCallExpr(self, node):
        callee = _kids(node)[0]
        member = callee.get("name", "")
        owner = _kids(callee)[0]
        if member == "empty":
            return ("empty", self.expr(owner))
        if member == "size":
            inner = _strip(owner)
            if inner.get("kind") == "CXXOperatorCallExpr" and _name_of_callee(_kids(inner)[0]) == "operator[]":    # m[0].size()
                return ("call", "size", (self.expr(_kids(inner)[1]), num(2)))
            value = self.expr(owner)
            if "string" in _type(inner) or "basic_string" in _type(inner):
                return ("call", "len_trim", value[2]) if value[:2] == ("call", "trim") else ("call", "len", (value,))
            return ("call", "size", (value, num(1)))
        if member == "real":
            value = self.expr(owner)
            kind = ic.kind_of(value, self.proc)
            return ("conv", "r" + kind[1:] if kind else "r8", value)
        if member == "imag":
            return ("call", "aimag", (self.expr(owner),))
        raise Unchecked(f"C++ member call not in the table: {member}")

    def x_MemberExpr(self, node):
        owner = _strip(_kids(node)[0])
        name = node.get("name", "").lower()
        if owner.get("kind") == "DeclRefExpr":
            text = owner.get("referencedDecl", {}).get("name", "").lower()
            if text.endswith("_common"):
                block = text[:-len("_common")]
                fields, local = self.classes.get(block + "block"), self.proc.commons.get(block)
                if fields and local and name in fields:
                    return ("var", local[fields.index(name)])
                return ("var", name)
        return ("comp", self.expr(owner), name)

    def x_CXXConstructExpr(self, node):
        parts = _kids(node)
        target = _type(node)
        if "basic_string" in target or target.startswith("std::string") or target == "string":
            return self.expr(parts[0]) if parts else ("str", "")
        if "complex" in target and len(parts) == 1:
            return fold(("conv", "c4" if "float" in target else "c8", self.expr(parts[0])))
        if len(parts) == 1:
            return self.expr(parts[0])
        if "complex" in target and len(parts) == 2:
            first, second = self.expr(parts[0]), self.expr(parts[1])
            kind = "c4" if "float" in target else "c8"
            if second == ("default",):
                return fold(("conv", kind, first))
            if first[0] == "num" and second[0] == "num":
                return ("cnum", first[1], second[1], kind)
            return ("conv", kind, ("call", "cmplx", (first, second)))
        raise Unchecked(f"C++ constructor not in the table: {target}")

    def x_CXXTemporaryObjectExpr(self, node):
        return self.x_CXXConstructExpr(node)

    def x_InitListExpr(self, node):
        items = tuple(self.expr(item) for item in _kids(node))
        target = _type(node)
        if target.endswith("_t"):
            return ("idx", re.sub(r"_t$", "", target.split("::")[-1]).lower(), items)     # a structure constructor
        return ("ctor", items)

    def x_CXXStdInitializerListExpr(self, node):
        return self.expr(_kids(node)[0])

    def x_LambdaExpr(self, node):
        raise Unchecked("a lambda that is not called")

    def begin_of(self, node: dict) -> tuple:
        """a.begin(): the array."""
        value = _strip(node)
        if value.get("kind") == "CXXConstructExpr" and _kids(value):        # the iterator passed by value
            value = _strip(_kids(value)[0])
        if value.get("kind") != "CXXMemberCallExpr" or _kids(value)[0].get("name") not in ("begin", "end"):
            raise Unchecked("C++ reduction over something other than begin()/end()")
        return self.expr(_kids(_kids(value)[0])[0])

    def reduction(self, name: str, args: Sequence[dict]) -> tuple:
        """The sequential reductions: std::accumulate adds left to right, as Fortran's SUM does in gfortran."""
        array = self.begin_of(args[0])
        if not ic.same_expr(array, self.begin_of(args[1])):
            raise Unchecked("C++ reduction over two arrays")
        if name in ("max_element", "min_element"):
            return ("call", "maxval" if name.startswith("max") else "minval", (array,))
        if name == "inner_product":
            start = self.expr(args[3])
            if start[0] != "num" or start[1] != 0:
                raise Unchecked("C++ DOT_PRODUCT does not start at zero")
            return ("call", "dot_product", (array, self.begin_of(args[2])))
        start = self.expr(args[2])
        if start[0] != "num":
            raise Unchecked("C++ reduction start not in the table")
        if len(args) == 4:
            if start[1] != 1 or "multiplies" not in _type(_strip(args[3])):
                raise Unchecked("C++ PRODUCT not in the table")
            return ic.fold_call("product", (array,))
        if start[1] != 0:
            raise Unchecked("C++ SUM does not start at zero")
        return ic.fold_call("sum", (array,))

    def lambda_value(self, node: dict) -> tuple:
        """An immediately called lambda computes one value: a filled vector, or a reduction of a temporary."""
        body = next(child for child in _kids(node) if child.get("kind") == "CompoundStmt")
        items: List[tuple] = []
        result: Optional[tuple] = None
        names: Dict[str, str] = {}
        saved_values = dict(self.values)
        for stmt in _kids(body):
            kind = stmt.get("kind")
            if kind == "DeclStmt":
                for decl in _kids(stmt):
                    init = next((c for c in _kids(decl) if c.get("kind") != "NoInlineAttr"), None)
                    name = decl.get("name", "")
                    if "vector" in _type(decl) and decl.get("init") == "call" and not name.startswith("a"):
                        names[name] = "vector"
                    elif init is not None:
                        self.values[name] = self.expr(init)
                    else:
                        names[name] = "vector"
            elif kind == "ForStmt":
                items.append(self.filled(stmt))
            elif kind == "ExprWithCleanups" or kind == "CXXMemberCallExpr":
                items.append(self.pushed(stmt))
            elif kind == "ReturnStmt":
                value = _strip(_kids(stmt)[0])
                if value.get("kind") == "DeclRefExpr" and value.get("referencedDecl", {}).get("name") in names:
                    result = _flatten_items(items)
                else:
                    result = self.expr(_kids(stmt)[0])
            else:
                raise Unchecked(f"C++ lambda statement {kind}")
        self.values = saved_values
        if result is None:
            raise Unchecked("C++ lambda with no result")
        return result

    def pushed(self, stmt: dict) -> tuple:
        value = _strip(stmt)
        if value.get("kind") != "CXXMemberCallExpr" or _kids(value)[0].get("name") not in ("push_back", "emplace_back"):
            raise Unchecked("C++ lambda statement that does not append")
        return self.expr(_kids(value)[1])

    def filled(self, stmt: dict) -> tuple:
        """for (int i = 1; i <= n; i += 1) r.push_back(e); is an implied DO. for (int k = 0; ...) t[k] = e; is an array value."""
        parts = stmt.get("inner", [])
        decl = _kids(parts[0])[0]
        variable = decl.get("name", "")
        start = self.expr(_kids(decl)[0])
        if _ARRAY_INDEX.match(variable):
            count = self.expr(_kids(_strip(parts[2]))[1])
            self.loops.append((variable, count))
            statement = _strip(parts[4])
            if statement.get("kind") == "CompoundStmt":
                statement = _strip(_kids(statement)[0])
            kind = statement.get("kind")
            if kind == "BinaryOperator":
                value = self.expr(_kids(statement)[-1])
            elif kind == "CXXMemberCallExpr":           # items.emplace_back(x[...]): an array in an output list
                value = self.expr(_kids(statement)[1])
            elif kind == "ForStmt":
                value = self.filled(statement)
            else:
                value = self.expr(_kids(statement)[2])
            loops = tuple(self.loops)
            self.loops.pop()
            return ("arrayvalue", loops, value)
        test = _strip(parts[2])
        stop = self.expr(_kids(test)[1])
        step = self.increment(parts[3], ("var", variable))
        inner: List[tuple] = []
        body = _kids(parts[4]) if parts[4].get("kind") == "CompoundStmt" else [parts[4]]
        for one in body:
            inner.append(self.filled(one) if one.get("kind") == "ForStmt" else self.pushed(one))
        return ("impdo", tuple(inner), ("var", variable.lower()), start, stop, num(step))

    # ---------------------------------------------------------------------------------------- statements
    def statements(self, nodes: Sequence[dict], events: List[tuple]) -> None:
        nodes = list(nodes)
        k = 0
        while k < len(nodes):
            node = nodes[k]
            k += 1
            following = nodes[k] if k < len(nodes) else None
            kind = node.get("kind")
            if kind == "DeclStmt":
                for decl in _kids(node):
                    self.declaration(decl, following, events)
            elif kind == "ForStmt" and self.buffer_copy(node, events):
                pass
            elif kind == "ForStmt" and self.real_do(node, events):
                pass
            elif kind == "ForStmt" and self.array_loop(node, events):
                pass
            elif kind == "CompoundStmt" and self.io_block(node, events):
                pass
            else:
                self.statement(node, events)

    def statement(self, node: dict, events: List[tuple]) -> None:
        kind = node.get("kind")
        handler = getattr(self, "s_" + str(kind), None)
        if handler is None:
            events.append(("unknown", f"C++ statement {kind}"))
            return
        handler(node, events)

    def declaration(self, decl: dict, following: Optional[dict], events: List[tuple]) -> None:
        name = decl.get("name", "")
        init = next((c for c in _kids(decl) if "Attr" not in c.get("kind", "")), None)
        if re.fullmatch(r"do\d+_(?:end|step)", name):
            if following is None or following.get("kind") != "ForStmt":
                events.append(("unknown", f"the bound {name} is not read right before its loop"))
            self.values[name] = self.expr(init)
            return
        if re.fullmatch(r"arr\d+_", name):
            self.values[name] = self.expr(init)
            return
        if re.fullmatch(r"(?:buf|pos)\d+_", name):
            return
        low = name.lower()
        self.declared[low] = decl
        sym = self.proc.symbols.get(low.rstrip("_"))
        if sym is not None and sym.parameter and sym.value is not None:
            value = self.expr(init) if init else None
            if value is None or not ic.Matcher(self.proc).tree(sym.value, value):
                events.append(("unknown", f"PARAMETER {name} is not the Fortran value"))
            return

    def array_loop(self, node: dict, events: List[tuple]) -> bool:
        """for (int k1_0 = 0; k1_0 < count; ++k1_0) ...: one Fortran array statement, element by element."""
        first = node.get("inner", [])[0]
        if first.get("kind") != "DeclStmt" or not _ARRAY_INDEX.match(_kids(first)[0].get("name", "")):
            return False
        at, loops = node, []
        while at.get("kind") == "ForStmt":
            parts = at.get("inner", [])
            decl = _kids(parts[0])[0]
            if self.expr(_kids(decl)[0]) != num(0):
                raise Unchecked("an array loop that does not start at 0")
            loops.append((decl["name"].lower(), self.expr(_kids(_strip(parts[2]))[1])))
            at = parts[4]
            if at.get("kind") == "CompoundStmt" and len(_kids(at)) == 1:
                at = _kids(at)[0]
        self.loops = loops
        statement = _strip(at)
        kind = statement.get("kind")
        try:
            if kind == "BinaryOperator" and statement.get("opcode") == "=":
                left, right = _kids(statement)
                events.append(("elem", tuple(sorted(loops, key=lambda loop: int(loop[0].rsplit("_", 1)[1]))), self.expr(left), self.expr(right)))
            elif kind == "CXXOperatorCallExpr" and _name_of_callee(_kids(statement)[0]) == "operator=":
                parts = _kids(statement)
                events.append(("elem", tuple(sorted(loops, key=lambda loop: int(loop[0].rsplit("_", 1)[1]))), self.expr(parts[1]), self.expr(parts[2])))
            elif kind == "CXXOperatorCallExpr" and _name_of_callee(_kids(statement)[0]) == "operator<<":
                before = len(self.pending)
                self.output(statement, events)
                # output is in column order: the last dimension is the outermost loop
                column = sorted(loops, key=lambda loop: int(loop[0].rsplit("_", 1)[1]))
                if list(loops) != column[::-1]:
                    events.append(("unknown", "array output not in column order"))
                self.pending[before:] = [("arrayvalue", tuple(column), value) for value in self.pending[before:]]
            elif kind == "CXXMemberCallExpr" and _kids(statement)[0].get("name") == "push_back":
                owner = _strip(_kids(_kids(statement)[0])[0]).get("referencedDecl", {}).get("name", "")
                self.buffers[owner] = (tuple(loops), self.expr(_kids(statement)[1]))
            else:
                events.append(("unknown", f"array loop over {kind}"))
        finally:
            self.loops = []
        return True

    def buffer_copy(self, node: dict, events: List[tuple]) -> bool:
        """target[k] = buf[pos++] after the loop that filled buf: the right side was computed first, as Fortran requires."""
        first = node.get("inner", [])[0]
        if first.get("kind") != "DeclStmt" or not _ARRAY_INDEX.match(_kids(first)[0].get("name", "")):
            return False
        at = node
        loops = []
        while at.get("kind") == "ForStmt":
            parts = at.get("inner", [])
            loops.append((_kids(parts[0])[0]["name"].lower(), self.expr(_kids(_strip(parts[2]))[1])))
            at = parts[4]
            if at.get("kind") == "CompoundStmt" and len(_kids(at)) == 1:
                at = _kids(at)[0]
        statement = _strip(at)
        if statement.get("kind") != "BinaryOperator":
            return False
        source = _strip(_kids(statement)[1])
        if source.get("kind") != "CXXOperatorCallExpr" or _name_of_callee(_kids(source)[0]) != "operator[]":
            return False
        name = _strip(_kids(source)[1]).get("referencedDecl", {}).get("name", "")
        if name not in self.buffers:
            return False
        filled, value = self.buffers.pop(name)
        if len(filled) != len(loops) or not all(a[0] == b[0] and ic.same_expr(a[1], b[1]) for a, b in zip(filled, loops)):
            events.append(("unknown", f"buffer {name} is copied with other loops than it was filled"))
            return True
        events.append(("elem", tuple(loops), self.expr(_kids(statement)[0]), value))
        return True

    def real_do(self, node: dict, events: List[tuple]) -> bool:
        """x = s; for (long long k = 0, n = max(0, (e - s + st) / st); k < n; ++k, x += st): DO with a real variable."""
        parts = node.get("inner", [])
        first = parts[0]
        if first.get("kind") != "DeclStmt" or not re.fullmatch(r"do\d+_k", _kids(first)[0].get("name", "")):
            return False
        if not events or events[-1][0] != "assign":
            raise Unchecked("a real DO with no start statement")
        _, target, start = events.pop()
        self.keep_shape = True
        trips = self.expr(_kids(_kids(first)[1])[0])
        self.keep_shape = False
        increment = _kids(parts[3])[1] if parts[3].get("kind") == "BinaryOperator" else None
        if increment is None or increment.get("kind") != "CompoundAssignOperator":
            raise Unchecked("a real DO with another step")
        step = self.expr(_kids(increment)[1])
        if trips[:2] != ("call", "max") or trips[2][0] != num(0):
            raise Unchecked("a real DO with another trip count")
        count = trips[2][1]
        while count[0] == "conv":
            count = count[2]
        if not (count[:2] == ("bin", "/") and ic.same_expr(count[3], step) and count[2][:2] == ("bin", "+") and ic.same_expr(count[2][3], step)
                and count[2][2][:2] == ("bin", "-") and ic.same_expr(count[2][2][3], start)):
            raise Unchecked(f"a real DO with another trip count: {count}")
        events.append(("do", target, start, count[2][2][2], step))
        self.statement(parts[4], events)
        events.append(("end",))
        return True

    def io_block(self, node: dict, events: List[tuple]) -> bool:
        statements = _kids(node)
        if len(statements) > 1 and statements[0].get("kind") == "DeclStmt" and _kids(statements[0])[0].get("name") == "fmt_":
            decl = _kids(statements[0])[0]
            self.values["fmt_"] = self.expr(_kids(decl)[0])
            statements = statements[1:]
        decl = _kids(statements[0])[0] if statements and statements[0].get("kind") == "DeclStmt" else None
        name = (decl or {}).get("name", "")
        if re.fullmatch(r"rd\d+_", name):
            self.read_block(decl, statements[1:], events)
            return True
        if re.fullmatch(r"items\d+_", name):
            items = self.collect(statements[1:-1], name)
            events.extend(self.write_block(statements[-1], items))
            return True
        if name.startswith("i0_"):
            self.sequence_block(statements, events)
            return True
        return False

    def collect(self, statements: Sequence[dict], name: str) -> List[tuple]:
        """items.emplace_back(x) and the implied DOs around them."""
        out: List[tuple] = []
        for stmt in statements:
            out.append(self.filled(stmt) if stmt.get("kind") == "ForStmt" else self.pushed(stmt))
        return out

    def write_block(self, last: dict, items: List[tuple]) -> List[tuple]:
        """fortran_write_unit(10, fortran_list(items)) or std::cout << fortran_write(format, items)."""
        call = _strip(last)
        if call.get("kind") == "BinaryOperator":       # ios = fortran_write_unit(...)
            target, call = self.expr(_kids(call)[0]), _strip(_kids(call)[1])
        else:
            target = None
        if call.get("kind") == "CallExpr" and _name_of_callee(_kids(call)[0]) == "fortran_write_unit":
            unit, text = _kids(call)[1:3]
            inner = _strip(text)
            parts = _kids(inner)
            form = self.expr(parts[1]) if _name_of_callee(parts[0]) == "fortran_write" else None
            return [("fwrite", self.expr(unit), form, tuple(items), target)]
        if call.get("kind") == "CXXOperatorCallExpr" and _name_of_callee(_kids(call)[0]) == "operator<<":
            inner = _strip(_kids(call)[2])
            return [("write", self.expr(_kids(inner)[1]), tuple(items))]
        return [("unknown", "an output block not in the table")]

    def read_block(self, decl: dict, statements: Sequence[dict], events: List[tuple]) -> None:
        """{ FReader rd(unit, format, die); rd.get(x); ...; rd.finish(1); } and the END=/ERR= jump after it."""
        args = _kids(_kids(decl)[0])
        unit = self.expr(args[0])
        form = self.expr(args[1])
        die = self.expr(args[2])
        items: List[tuple] = []
        iostat = None
        after: List[dict] = []
        for stmt in statements:
            value = _strip(stmt)
            kind = value.get("kind")
            if kind == "IfStmt":
                after.append(stmt)
            elif kind == "DeclStmt":
                if _kids(stmt)[0].get("name") != "ios_":
                    events.append(("unknown", "a declaration in a READ"))
                self.values["ios_"] = ("var", "_ios")
            elif kind == "BinaryOperator" and value.get("opcode") == "=":
                iostat = self.expr(_kids(value)[0])
            elif kind == "CXXOperatorCallExpr" and _name_of_callee(_kids(value)[0]) == "operator=":
                # s = fortran_fix(s, n) after rd.get(s, n): the length of the variable
                length = self.expr(_kids(value)[2])
                target = self.expr(_kids(value)[1])
                sym = self.proc.symbols.get(target[1]) if target[0] in ("var", "idx") else None
                if length[0] != "cfix" or sym is None or length[2] != num(sym.length):
                    events.append(("unknown", "a READ into a character of another length"))
            else:
                items.extend(self.read_items(stmt))
        events.append(("read", unit, None if form == ("null",) else form, tuple(items), iostat, die == ("bool", False)))
        self.statements(after, events)

    def read_items(self, stmt: dict) -> List[tuple]:
        value = _strip(stmt)
        if value.get("kind") == "ForStmt":
            parts = value.get("inner", [])
            decl = _kids(parts[0])[0]
            inner: List[tuple] = []
            if _ARRAY_INDEX.match(decl.get("name", "")):
                loops: List[Tuple[str, tuple]] = []
                at = value
                while at.get("kind") == "ForStmt":
                    pieces = at.get("inner", [])
                    loops.append((_kids(pieces[0])[0]["name"].lower(), self.expr(_kids(_strip(pieces[2]))[1])))
                    at = pieces[4]
                    if at.get("kind") == "CompoundStmt" and len(_kids(at)) == 1:
                        at = _kids(at)[0]
                column = sorted(loops, key=lambda loop: int(loop[0].rsplit("_", 1)[1]))
                if loops != column[::-1]:
                    raise Unchecked("array input not in column order")
                return [("arrayvalue", tuple(column), item) for item in self.read_items(at)]
            for one in (_kids(parts[4]) if parts[4].get("kind") == "CompoundStmt" else [parts[4]]):
                inner.extend(self.read_items(one))
            return [("impdo", tuple(inner), ("var", decl["name"].lower()), self.expr(_kids(decl)[0]), self.expr(_kids(_strip(parts[2]))[1]),
                     num(self.increment(parts[3], ("var", decl["name"]))))]
        if value.get("kind") == "CXXMemberCallExpr" and _kids(value)[0].get("name") == "get":
            return [self.expr(_kids(value)[1])]
        if value.get("kind") == "CXXMemberCallExpr" and _kids(value)[0].get("name") == "finish":
            return []
        raise Unchecked(f"C++ statement in a READ: {value.get('kind')}")

    def sequence_block(self, statements: Sequence[dict], events: List[tuple]) -> None:
        """Sequence association in C++: a dummy array that starts at an element gets a column-order copy, written back after the
        call. The call is compared; the loops are held to the array, the copy and their own indices."""
        offsets = [self.expr(_kids(decl)[0]) for decl in _kids(statements[0])]
        fill, call, back = statements[2], statements[3], statements[4]
        name = next(n for n in _references(fill) if not re.match(r"seq|[ij]0_|ii$|jj$|pos", n))
        allowed = re.compile(rf"^(?:{re.escape(name)}|seq\d+_|[ij]0_\d+|ii|jj|pos_)$")
        for part in (fill, back):
            if not all(allowed.match(n) for n in _references(part)):
                events.append(("unknown", "a sequence-association copy touches other variables"))
        parts = _kids(_strip(call))
        target = ("seqsub", self.name(name), tuple(ic._plus(o, 1) for o in offsets))
        args = [target if _strip(a).get("referencedDecl", {}).get("name", "").startswith("seq") else self.expr(a) for a in parts[1:]]
        events.append(("call", _name_of_callee(parts[0]).lower(), tuple(args)))

    def s_CompoundStmt(self, node, events):
        self.statements(_kids(node), events)

    def s_NullStmt(self, node, events):
        pass

    def s_SwitchStmt(self, node, events):
        """switch (i) { case 1: goto a; ... default: break; }: a computed GO TO, the IF chain it means."""
        parts = _kids(node)
        selector = self.expr(parts[0])
        first = True
        for case in _kids(parts[1]):
            if case.get("kind") == "DefaultStmt":
                continue
            if case.get("kind") != "CaseStmt":
                events.append(("unknown", f"switch over {case.get('kind')}"))
                continue
            value = self.expr(_kids(case)[0])
            events.append(("if" if first else "elif", ("bin", "==", selector, value)))
            first = False
            self.statement(_kids(case)[-1], events)
        if not first:
            events.append(("end",))

    def s_BinaryOperator(self, node, events):
        if node.get("opcode") != "=":
            events.append(("unknown", f"C++ statement with {node.get('opcode')}"))
            return
        left, right = _kids(node)
        value = self.expr(right)
        if value[0] in ("fopen", "fclose", "frewind", "fexists"):
            events.append(self.file_statement(value, self.expr(left)))
        elif value == ("null",):
            events.append(("free", self.expr(left)))
        elif value[0] == "new":
            events.append(("alloc", self.expr(left), value[1]))
        else:
            events.append(("assign", self.expr(left), value))

    def s_ExprWithCleanups(self, node, events):
        self.statement(_kids(node)[0], events)

    def s_CXXOperatorCallExpr(self, node, events):
        parts = _kids(node)
        name = _name_of_callee(parts[0])
        if name == "operator=":
            self.vector_assignment(parts, events)
        elif name == "operator<<":
            self.output(node, events)
        else:
            events.append(("unknown", f"C++ statement {name}"))

    def vector_assignment(self, parts: Sequence[dict], events: List[tuple]) -> None:
        """a = std::vector<double>(n, double()) allocates; anything else is an assignment."""
        source = _strip(parts[2])
        if source.get("kind") in ("CXXTemporaryObjectExpr", "CXXConstructExpr") and "vector" in _type(source):
            args = _kids(source)
            if args and "initializer_list" not in _type(_strip(args[0])) and _strip(args[0]).get("kind") != "CXXStdInitializerListExpr":
                extents = [self.expr(args[0])]
                inner = _strip(args[1]) if len(args) > 1 else None
                if inner is not None and "vector" in _type(inner) and inner.get("kind") in ("CXXTemporaryObjectExpr", "CXXConstructExpr"):
                    extents.append(self.expr(_kids(inner)[0]))
                events.append(("alloc", self.expr(parts[1]), tuple(extents)))
                return
        events.append(("assign", self.expr(parts[1]), self.expr(parts[2])))

    def s_CXXMemberCallExpr(self, node, events):
        callee = _kids(node)[0]
        member = callee.get("name", "")
        if member == "clear":
            events.append(("free", self.expr(_kids(callee)[0])))
        else:
            events.append(("unknown", f"C++ member call {member}"))

    def file_statement(self, value: tuple, target: Optional[tuple]) -> tuple:
        args = value[1]
        iostat = None if target == ("var", "ios_") else target
        if value[0] == "fopen":
            defaults = {"action": "readwrite", "position": "asis", "status": "unknown"}
            keys = [(key, item) for key, item in zip(("file", "status", "action", "position"), args[1:])
                    if item != ("str", defaults.get(key))]
            return ("open", args[0], tuple(keys), iostat)
        if value[0] == "fclose":
            return ("close", args[0], (("status", args[1]),) if len(args) > 1 else (), iostat)
        if value[0] == "frewind":
            return ("rewind", args[0])
        return ("inquire", args[0], target)

    def s_CallExpr(self, node, events):
        parts = _kids(node)
        name = _name_of_callee(parts[0])
        if name in ("fortran_open", "fortran_close", "fortran_rewind", "fortran_exists"):
            events.append(self.file_statement(self.expr(node), None))
            return
        if name == "fill" and len(parts) == 4:        # std::fill(a.begin(), a.end(), v), not a Fortran routine called fill
            array = self.begin_of(parts[1])
            if not ic.same_expr(array, self.begin_of(parts[2])):
                events.append(("unknown", "std::fill over two arrays"))
            events.append(("assign", array, self.expr(parts[3])))
            return
        if name.startswith("fortran_") or not name:
            events.append(("unknown", f"C++ call {name}"))
            return
        events.append(("call", name.lower(), tuple(self.expr(a) for a in parts[1:] if not _common_object(a))))

    def s_IfStmt(self, node, events):
        self.branch(node, events, "if")
        events.append(("end",))

    def branch(self, node: dict, events: List[tuple], tag: str) -> None:
        parts = _kids(node)
        events.append((tag, self.expr(parts[0])))
        self.statement(parts[1], events)
        if len(parts) > 2:
            other = parts[2]
            if other.get("kind") == "IfStmt":
                self.branch(other, events, "elif")
            else:
                events.append(("else",))
                self.statement(other, events)

    def s_CaseStmt(self, node, events):
        events.append(("unknown", "a case outside the switch forms"))

    def s_ForStmt(self, node, events):
        if self.real_do(node, events) or self.array_loop(node, events):
            return
        parts = node.get("inner", [])
        if parts[0].get("kind") == "DeclStmt":          # for (int i = 1; i <= 3; i += 1) std::cout << ...: an implied DO in a list
            decl = _kids(parts[0])[0]
            start, stop = self.expr(_kids(decl)[0]), self.expr(_kids(_strip(parts[2]))[1])
            step = self.increment(parts[3], ("var", decl["name"]))
            before = len(self.pending)
            self.statement(parts[4], events)
            inner = tuple(self.pending[before:])
            del self.pending[before:]
            self.pending.append(("impdo", inner, ("var", decl["name"].lower()), start, stop, num(step)))
            return
        init, cond, step, body = parts[0], parts[2], parts[3], parts[4]
        events.append(self.do_header(init, cond, step))
        self.statement(body, events)
        events.append(("end",))

    def do_header(self, init: dict, cond: dict, step: dict) -> tuple:
        """for (i = a; i <= b; ++i) with b evaluated once: the emitter reads a changing bound into a const before the loop."""
        if init.get("kind") != "BinaryOperator" or init.get("opcode") != "=":
            raise Unchecked(f"C++ loop not in the table: {init.get('kind')}")
        variable, start = _kids(init)
        target = self.expr(variable)
        test = _strip(cond)
        if test.get("kind") != "BinaryOperator" or test.get("opcode") not in ("<=", ">="):
            raise Unchecked("C++ loop test not in the table")
        tested, stop = _kids(test)
        if self.expr(tested) != target:
            raise Unchecked("C++ loop tests another variable")
        increment = self.increment(step, target)
        if (test["opcode"] == "<=") != (increment > 0):
            raise Unchecked("C++ loop test and step disagree")
        return ("do", target, self.expr(start), self.expr(stop), num(increment))

    def increment(self, step: dict, target: tuple) -> int:
        kind = step.get("kind")
        if kind == "UnaryOperator" and step.get("opcode") in ("++", "--"):
            return 1 if step["opcode"] == "++" else -1
        if kind == "CompoundAssignOperator" and step.get("opcode") == "+=":
            amount = self.expr(_kids(step)[1])
            if amount[0] == "num":
                return amount[1]
        raise Unchecked("C++ loop step not in the table")

    def s_WhileStmt(self, node, events):
        parts = _kids(node)
        events.append(("while", self.expr(parts[0])))
        self.statement(parts[1], events)
        events.append(("end",))

    def s_ReturnStmt(self, node, events):
        events.append(("return",))

    def s_BreakStmt(self, node, events):
        events.append(("exit",))

    def s_ContinueStmt(self, node, events):
        events.append(("cycle",))

    def s_LabelStmt(self, node, events):
        events.append(("label", node["name"].replace("label_", "")))
        self.statements(_kids(node), events)

    def s_GotoStmt(self, node, events):
        events.append(("goto", self.labels.get(node.get("targetLabelDeclId"), "?")))

    # ---------------------------------------------------------------------------------------- output
    def output(self, node: dict, events: List[tuple]) -> None:
        """std::cout << a << " " << b << " "; then std::cout << std::endl; is one list-directed WRITE."""
        items: List[dict] = []
        at = node
        while _strip(at).get("kind") == "CXXOperatorCallExpr" and _name_of_callee(_kids(_strip(at))[0]) == "operator<<":
            parts = _kids(_strip(at))
            items.insert(0, parts[2])
            at = parts[1]
        if _strip(at).get("referencedDecl", {}).get("name") != "cout":
            events.append(("unknown", "output to something other than std::cout"))
            return
        ended = False
        for item in items:
            value = _strip(item)
            if _name_of_callee(value) == "endl" or value.get("kind") == "UnresolvedLookupExpr":
                ended = True
            elif value.get("kind") == "CallExpr" and _name_of_callee(_kids(value)[0]) == "setprecision":
                return                      # the first line of main: print every digit
            elif value.get("kind") == "CallExpr" and _name_of_callee(_kids(value)[0]) == "endl":
                ended = True
            else:
                text = self.expr(item)
                if text != ("str", " "):
                    self.pending.append(text)
        if ended:
            events.append(("write", tuple(self.pending)))
            self.pending = []


def _references(node: Any, out: Optional[List[str]] = None) -> List[str]:
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("kind") == "DeclRefExpr":
            decl = node.get("referencedDecl", {})
            if decl.get("kind") in ("VarDecl", "ParmVarDecl"):
                out.append(decl.get("name", ""))
        for key, value in node.items():
            if key not in ("referencedDecl", "foundReferencedDecl"):
                _references(value, out)
    elif isinstance(node, list):
        for value in node:
            _references(value, out)
    return out


def _boolean(node: tuple) -> bool:
    return node[0] == "bool" or (node[0] == "conv" and node[1] == "i4" and node[2][0] in ("bool", "var", "idx", "call", "bin"))


def _flatten_items(items: List[tuple]) -> tuple:
    if len(items) == 1 and items[0][0] == "arrayvalue":
        return items[0]
    return ("ctor", tuple(items))


def _common_object(node: dict) -> bool:
    value = _strip(node)
    return value.get("kind") == "DeclRefExpr" and value.get("referencedDecl", {}).get("name", "").endswith("_common")


def _index(node: tuple) -> tuple:
    """An integer argument widened to the helper's parameter type is the same integer."""
    return node[2] if node[0] == "conv" and node[1] in ("i4", "i8") and node[2][0] != "conv" else node


def labels_of(node: Any, table: Dict[str, str]) -> None:
    if isinstance(node, dict):
        if node.get("kind") == "LabelStmt":
            table[node.get("declId")] = node["name"].replace("label_", "")
        for value in node.values():
            labels_of(value, table)
    elif isinstance(node, list):
        for value in node:
            labels_of(value, table)


def _shape(type_text: str) -> Tuple[int, str]:
    """(rank, element type) of a declared type: std::vector<std::vector<double>> is (2, double)."""
    text = re.sub(r"\b(?:const|static|volatile)\b|[&*]", "", type_text).strip()
    rank = 0
    while True:
        match = re.fullmatch(r"(?:std::(?:__1::)?)?vector<\s*(.*?)\s*(?:,\s*(?:std::(?:__1::)?)?allocator<.*>\s*)?>", text)
        if not match:
            return rank, text.strip()
        rank, text = rank + 1, match.group(1).strip()


def _unwrapped(node: dict) -> dict:
    """Down to the constructor call. _strip() would go through it: a vector built from one size looks like a copy to it."""
    while node.get("kind") in ("ExprWithCleanups", "MaterializeTemporaryExpr", "CXXBindTemporaryExpr", "ImplicitCastExpr",
                               "CXXFunctionalCastExpr") and _kids(node):
        node = _kids(node)[0]
    return node


def _extents_of(decl: dict, reader: CppReader) -> Optional[List[tuple]]:
    """std::vector<double> a(n) and std::vector<std::vector<double>> m(3, std::vector<double>(2)): the extents given."""
    made = next((c for c in _kids(decl) if c.get("kind") in ("CXXConstructExpr", "ExprWithCleanups")), None)
    out: List[tuple] = []
    while made is not None:
        made = _unwrapped(made)
        args = [a for a in _kids(made) if a.get("kind") != "CXXDefaultArgExpr"]
        if made.get("kind") not in ("CXXConstructExpr", "CXXTemporaryObjectExpr") or not args:
            return out or None
        out.append(reader.expr(args[0]))
        made = args[1] if len(args) > 1 and "vector" in _type(_unwrapped(args[1])) else None
    return out or None


def _initial(node: dict, reader: CppReader, rank: int) -> Optional[List[Any]]:
    """The numbers of a static's initialiser, in column order. C++ lists a matrix by rows."""
    value = reader.expr(node)

    def numbers(item: tuple) -> Optional[List[Any]]:
        if item[0] in ("num", "bool"):
            return [item[1]]
        if item[0] == "ctor":
            out: List[Any] = []
            for part in item[1]:
                inner = numbers(part)
                if inner is None:
                    return None
                out.extend(inner)
            return out
        return None
    if rank == 2 and value[0] == "ctor":
        rows = [numbers(row) for row in value[1]]
        if any(row is None for row in rows) or not rows:
            return None
        return [rows[i][j] for j in range(len(rows[0])) for i in range(len(rows))]
    return numbers(value)


def declaration_problems(fn: dict, reader: CppReader, proc: ic.Proc) -> List[str]:
    """Each declared variable has its Fortran kind, rank and extents, a saved one its initial values. Parameters are checked
    where they are read. A C++ declaration decides the arithmetic, so this is part of the equations."""
    problems: List[str] = []
    layout = ic.equivalence_layout(proc) if proc.equivalences else {}
    decls = dict(reader.declared)
    for child in _kids(fn):
        if child.get("kind") == "ParmVarDecl":
            decls[child.get("name", "").lower()] = child
    for name, decl in decls.items():
        base = name.rstrip("_")
        sym = proc.symbols.get(base)
        if sym is None or sym.parameter or sym.kind == "":
            continue
        rank, element = _shape(_type(decl))
        if sym.kind == "character":
            if "string" not in element:
                problems.append(f"{name} is not a character variable")
            continue
        if sym.kind == "derived":
            continue
        if KINDS.get(element) != sym.kind:
            problems.append(f"{name} has type {element}, not kind {sym.kind}")
        if rank != len(sym.upper):
            problems.append(f"{name} has rank {rank}, not {len(sym.upper)}")
        if sym.upper and sym.shape_kind == "EXPLICIT" and not sym.dummy and not sym.saved and decl.get("kind") == "VarDecl":
            extents = _extents_of(decl, reader)
            wanted = [fold(ic._extent(lo, hi)) for lo, hi in zip(sym.lower, sym.upper)]
            matcher = ic.Matcher(proc)
            if extents is None or len(extents) != len(wanted) or not all(ic.same_index(a, b, matcher) for a, b in zip(wanted, extents)):
                problems.append(f"{name}: the extents are not the Fortran ones")
        wanted_values = proc.initial.get(base)
        if wanted_values is not None:
            if decl.get("storageClass") != "static":
                problems.append(f"{name} keeps its value between calls in Fortran, and is not static")
            else:
                init = next((c for c in _kids(decl) if "Attr" not in c.get("kind", "")), None)
                got = _initial(init, reader, rank) if init is not None else None
                wrap = {"r4": np.float32, "r8": np.float64}.get(sym.kind)
                if got is None or not ic.same_values(wanted_values, [wrap(v) if wrap else v for v in got]):
                    problems.append(f"initial value of {name} is not the Fortran value")
        if name in layout and decl.get("kind") == "VarDecl":
            partners = [other for other in layout if other != name and layout[other] == layout[name]]
            initial = next((c for c in _kids(decl) if "Attr" not in c.get("kind", "")), None)
            target = _strip(initial).get("referencedDecl", {}).get("name", "").lower() if initial else ""
            is_reference = "&" in decl.get("type", {}).get("qualType", "")
            # the first of a group is the storage; every other one has to be a reference to it
            if is_reference and target.rstrip("_") not in partners:
                problems.append(f"{name} is not an alias of its EQUIVALENCE partner")
            if not is_reference and any(other in decls and "&" not in decls[other].get("type", {}).get("qualType", "") and other < name for other in partners):
                problems.append(f"{name} and its EQUIVALENCE partner are separate storage")
    if proc.equivalences:
        for name in layout:
            if len({k for k in layout if layout[k] == layout[name]}) > 1 and name not in decls and name + "_" not in decls:
                problems.append(f"the equivalenced variable {name} is not declared")
    return problems


def function_events(fn: dict, proc: ic.Proc, classes: Dict[str, List[str]]) -> List[tuple]:
    reader = CppReader(proc, classes)
    reader.pending, reader.labels = [], {}
    labels_of(fn, reader.labels)
    body = next(child for child in _kids(fn) if child.get("kind") == "CompoundStmt")
    statements = _kids(body)
    if statements and statements[-1].get("kind") == "ReturnStmt":
        statements = statements[:-1]        # the end of the procedure, not a RETURN statement
    events: List[tuple] = []
    reader.statements(statements, events)
    if reader.pending:
        events.append(("unknown", "output with no endl"))
    events.extend(("unknown", problem) for problem in declaration_problems(fn, reader, proc))
    return events


def check_cpp(originals: Sequence[Path], translated: Sequence[Path], dump: Optional[str] = None) -> ic.Report:
    procs = ic.parse_dump(dump if dump is not None else ic.dump_fortran(originals))
    functions: Dict[str, dict] = {}
    classes: Dict[str, List[str]] = {}
    globals_: Dict[str, dict] = {}
    for path in translated:
        for node in dump_cpp(Path(path)):
            if node.get("kind") == "FunctionDecl" and any(c.get("kind") == "CompoundStmt" for c in _kids(node)):
                name = node.get("name", "")
                if not name.startswith(("fortran_", "__")):
                    functions[name.lower()] = node
            elif node.get("kind") == "CXXRecordDecl" and node.get("name"):
                classes[node["name"].lower()] = [c.get("name", "").lower() for c in _kids(node) if c.get("kind") == "FieldDecl"]
            elif node.get("kind") == "VarDecl":
                globals_[node.get("name", "").lower()] = node
    report = ic.compare_all(procs, functions, classes, originals, function_events)
    for proc in procs.values():
        for name, sym in proc.symbols.items():
            decl = globals_.get(name) or globals_.get(name + "_")
            if decl is None or not sym.parameter or sym.value is None:
                continue
            reader = CppReader(proc, classes)
            init = next((c for c in _kids(decl) if "Attr" not in c.get("kind", "")), None)
            if init is None or not ic.Matcher(proc).tree(sym.value, reader.expr(init)):
                report.failures.setdefault(proc.name, []).append(f"PARAMETER {name} is not the Fortran value")
    return report
