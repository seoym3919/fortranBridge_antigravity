#!/usr/bin/env python3
"""Run the Fortran and its translation side by side and compare every assignment, in order, bit for bit.

identity_checker.py compares the programs as text. This compares what they do: a helper of the translation can match its table
entry and still compute something else, and output comparison only sees what the program prints. Here each assignment's value
is recorded on both sides, and the first value that differs names its Fortran statement.

Instrumentation is the checker's own. The Fortran copy gets a `write` after each assignment; the Python copy gets a call after
each assignment by an `ast` transform, so the translator takes no part. Three checks guard the measurement itself:
  - the instrumented Fortran must print exactly what the original prints, or the probes changed the program;
  - both streams must have the same length: a statement run on one side only is a difference;
  - gcov must show every statement of the original was executed. A statement never run was not verified by anything here, so
    each one must be listed with --unexecuted-ok FILE:LINE (the listing is recorded in the evidence).

Exit status: 0 identical, 1 different, 2 nothing was established (not a pass). --machine-precision compares reals within
2 ULP and eps relative, for `VERDICT: identical algorithm`; the record says so, and it does not support a bitwise claim.
"""

import argparse
import ast
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identity_checker as ic  # noqa: E402
import verification_evidence as ve  # noqa: E402

CHECKER_VERSION = "1"
UNIT = 9797
FLAGS = ["-O0", "-fwrapv", "-w", "-ffixed-line-length-none", "-ffree-line-length-none"]
TIMEOUT = 300
_NOT_ASSIGNMENT = re.compile(
    r"^(?:do|if|else|end|call|print|write|read|open|close|rewind|inquire|return|stop|goto|go\s*to|continue|cycle|exit|allocate|"
    r"deallocate|nullify|select|case|where|forall|integer|real|double|complex|logical|character|type|class|implicit|parameter|data|"
    r"common|dimension|equivalence|external|intrinsic|save|use|module|program|subroutine|function|contains|interface|format|"
    r"public|private|include|block|target|pointer|allocatable|sequence|procedure)\b", re.IGNORECASE)
_PURE = re.compile(r"^\s*(?:elemental|pure)\b", re.IGNORECASE)


class Unchecked(Exception):
    """Nothing was established. Exit 2: this is not a pass."""


@dataclass
class Probe:
    number: int
    file: str
    line: int
    target: str


@dataclass
class Logical:
    first: int                                  # index of its first physical line
    last: int
    label: Optional[str]
    text: str


# --------------------------------------------------------------------------------------------------------------
# Instrumenting the Fortran
# --------------------------------------------------------------------------------------------------------------

def is_fixed(path: Path) -> bool:
    return path.suffix.lower() in (".f", ".for", ".f77")


def _strip_comment(text: str) -> str:
    """Remove a trailing ! comment, honouring strings."""
    quote = None
    for index, char in enumerate(text):
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "!":
            return text[:index]
    return text


def logical_lines(lines: List[str], fixed: bool) -> List[Logical]:
    out: List[Logical] = []
    k = 0
    while k < len(lines):
        raw = lines[k]
        if fixed and raw[:1] in ("c", "C", "*", "!"):
            k += 1
            continue
        body = _strip_comment(raw[6:] if fixed else raw).rstrip()
        label = None
        if fixed:
            label = raw[:5].strip() or None
        else:
            match = re.match(r"^\s*(\d+)\s+(.*)$", body)
            if match:
                label, body = match.group(1), match.group(2)
        if not body.strip():
            k += 1
            continue
        first, text = k, body.strip()
        k += 1
        if fixed:
            while k < len(lines) and len(lines[k]) > 5 and lines[k][:1] not in ("c", "C", "*", "!") and lines[k][5] not in (" ", "0"):
                text += _strip_comment(lines[k][6:]).strip()
                k += 1
        else:
            while text.endswith("&") and k < len(lines):
                text = text[:-1].rstrip() + " " + _strip_comment(lines[k]).strip().lstrip("&")
                k += 1
        out.append(Logical(first, k - 1, label, text.strip()))
    return out


def _top_level_equals(text: str) -> Optional[int]:
    depth, quote = 0, None
    for index, char in enumerate(text):
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "=" and depth == 0:
            if text[index + 1:index + 2] in ("=", ">") or text[index - 1:index] in ("/", "<", ">", "="):
                return None
            return index
    return None


def _one_line_if(text: str) -> Optional[Tuple[str, str]]:
    """IF (condition) statement, as (condition with its parentheses, statement). None if it is another form."""
    match = re.match(r"^if\s*\(", text, re.IGNORECASE)
    if not match:
        return None
    depth, start = 0, match.end() - 1
    for index in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            rest = text[index + 1:].strip()
            if not rest or re.match(r"^then\b", rest, re.IGNORECASE) or re.match(r"^\d", rest):
                return None
            return text[start:index + 1], rest
    return None


def _target(statement: str) -> Optional[str]:
    """The left side when `statement` is an assignment."""
    if _NOT_ASSIGNMENT.match(statement) and not re.match(r"^\w+\s*(?:\(.*\))?\s*(?:%\s*\w+.*)?=", statement) or "::" in statement:
        return None
    at = _top_level_equals(statement)
    if at is None or statement[at + 1:at + 2] == ">":
        return None
    target = statement[:at].strip()
    return target if re.match(r"^[A-Za-z_]\w*", target) else None


def _write(number: int, target: str, fixed: bool) -> str:
    return f"{'      ' if fixed else ''}write({UNIT}) {number}, {target}"


def _not_traced(target: str, symbols: Dict[str, ic.Sym], structures: Set[str]) -> bool:
    """A statement function is not an assignment; a whole structure has no stream of its own (its components are traced)."""
    name = re.match(r"\w+", target).group().lower()
    sym = symbols.get(name)
    if "%" in target:
        last = re.search(r"%\s*(\w+)[^%]*$", target)
        return last is not None and last.group(1).lower() in structures
    return sym is not None and (sym.kind == "derived" or ("(" in target and not sym.upper and sym.kind != "character"))


_UNIT_START = re.compile(r"^(?:[\w\s()*=,]*\s)?(?:subroutine|function|program)\s+(\w+)", re.IGNORECASE)


def instrument(path: Path, display: str, probes: List[Probe], procs: Dict[str, ic.Proc]) -> str:
    fixed = is_fixed(path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    logical = logical_lines(lines, fixed)
    terminals = {m for item in logical for m in re.findall(r"^do\s+(\d+)\b", item.text, re.IGNORECASE)}
    replaced: Dict[int, List[str]] = {}
    in_pure = False
    in_type = False
    executable = False
    symbols: Dict[str, ic.Sym] = {}
    structures: Set[str] = set()                # components that are themselves structures
    for item in logical:
        low = item.text.lower()
        match = _UNIT_START.match(low)
        if match and not re.match(r"^end", low):
            in_pure, executable = bool(_PURE.match(low)), False
            if match.group(1) in procs:
                symbols = procs[match.group(1)].symbols
        if re.match(r"^type\b(?!\s*\()", low):
            in_type = True
        if in_type and re.match(r"^type\s*\(", low) and "::" in low:
            structures.update(n.strip().lower() for n in re.sub(r"\(.*?\)", "", low.split("::", 1)[1]).split(","))
        if re.match(r"^end\s*type\b", low):
            in_type = False
        conditional = _one_line_if(item.text)
        statement = conditional[1] if conditional else item.text
        target = None if in_type else _target(statement)
        if target is None or in_pure or _not_traced(target, symbols, structures):
            if not re.match(r"^(?:integer|real|double|complex|logical|character|type\s*\(|implicit|parameter|data|common|dimension|"
                            r"equivalence|external|intrinsic|save|use|contains|interface)", low):
                executable = executable or bool(target) or bool(re.match(r"^(?:do|if|call|print|write|read)\b", low))
            continue
        probes.append(Probe(len(probes) + 1, display, item.first + 1, target))
        probe = _write(probes[-1].number, target, fixed)
        indent = " " * 6 if fixed else ""
        label = item.label or ""
        head = f"{label:<5} " if fixed else (f"{label} " if label else "")
        if conditional:
            block = [f"{head}if {conditional[0]} then", f"{indent}{statement}", probe, f"{indent}end if"]
        else:
            block = [f"{head}{item.text}", probe]
        if item.label and item.label in terminals:
            block.append(f"{'9' + item.label[-4:].zfill(4):<5} continue" if fixed else f"{'9' + item.label[-4:].zfill(4)} continue")
        replaced[item.first] = block
        for index in range(item.first + 1, item.last + 1):
            replaced[index] = []
    if terminals:
        for item in logical:                    # a DO that ended on an assignment now ends on the CONTINUE after its probe
            match = re.match(r"^do\s+(\d+)\b(.*)$", item.text, re.IGNORECASE)
            if match and any(block and block[-1].rstrip().endswith("continue") and block[-1].strip().startswith("9" + match.group(1)[-4:].zfill(4))
                             for block in replaced.values()):
                prefix = lines[item.first][:6] if fixed else ""
                replaced[item.first] = [f"{prefix}do {'9' + match.group(1)[-4:].zfill(4)}{match.group(2)}"]
    out: List[str] = []
    for index, line in enumerate(lines):
        out.extend(replaced[index] if index in replaced else [line])
    return "\n".join(out) + "\n"


def read_fortran_trace(path: Path) -> Iterator[Tuple[int, bytes]]:
    """Records of a sequential unformatted file: marker, id (int32), value, marker."""
    if not path.is_file():
        return
    with path.open("rb") as handle:
        while True:
            head = handle.read(4)
            if not head:
                return
            size = struct.unpack("<i", head)[0]
            if size < 4:
                raise Unchecked(f"unformatted record of {size} bytes (a record over 2 GB is split, which is not handled)")
            body = handle.read(size)
            handle.read(4)
            yield struct.unpack("<i", body[:4])[0], body[4:]


# --------------------------------------------------------------------------------------------------------------
# Instrumenting the Python
# --------------------------------------------------------------------------------------------------------------

PRELUDE = """\
import numpy as _fb_np, struct as _fb_struct
_fb_stream = open("python.trace", "wb")
def _fb_trace(value):
    if isinstance(value, str):
        kind, data, count = "str", value.encode("latin-1"), len(value)
    else:
        array = _fb_np.asarray(value)
        kind, count = array.dtype.name, array.size
        data = _fb_np.ravel(array, order="F").tobytes()
    _fb_stream.write(_fb_struct.pack("<I", len(kind)) + kind.encode() + _fb_struct.pack("<QQ", count, len(data)) + data)
"""
EPILOGUE = "\nmain()\n_fb_stream.close()\n"


def read_python_trace(path: Path) -> Iterator[Tuple[str, int, bytes]]:
    with path.open("rb") as handle:
        while True:
            head = handle.read(4)
            if not head:
                return
            kind = handle.read(struct.unpack("<I", head)[0]).decode()
            count, size = struct.unpack("<QQ", handle.read(16))
            yield kind, count, handle.read(size)


class Probes(ast.NodeTransformer):
    """A call after each assignment of a Fortran variable. Scaffolding has names starting `_`; storage and output arguments of a
    subroutine call are not Fortran assignments."""

    def __init__(self, parameters: Set[str], subroutines: Set[str], pointers: Set[str]):
        self.parameters, self.subroutines, self.pointers = parameters, subroutines, pointers

    def traced(self, stmt: ast.stmt) -> Optional[ast.expr]:
        if isinstance(stmt, ast.AugAssign):
            target = stmt.target
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if ic._declaration(stmt) or ic._null_pointer(stmt) or ic._equivalence_view(stmt):
                return None
            value = stmt.value
            if isinstance(value, ast.Call) and ic.dotted(value.func).lower() in self.subroutines:
                return None
            if isinstance(value, ast.Name) and value.id == "_v":
                return None
            if "_doG" in ast.unparse(value):
                return None                 # a DO variable stepping in the GO TO machine: not an assignment in Fortran
            if isinstance(value, ast.Call) and re.match(r"_fopen|_fclose|_fexists|_fwriteunit|_rd\d+\.finish", ic.dotted(value.func)):
                return None                 # the status an I/O statement stores
            if isinstance(target, ast.Name) and target.id.rstrip("_").lower() in self.pointers and isinstance(value, ast.Name):
                return None                 # p => x
        else:
            return None
        root = target
        while isinstance(root, (ast.Subscript, ast.Attribute)):
            root = root.value
        if not isinstance(root, ast.Name) or isinstance(target, ast.Tuple):
            return None
        name = root.id
        if name.startswith("_") and not name.startswith(("_save_", "_eq")):
            return None
        if isinstance(target, ast.Name) and name.rstrip("_").lower() in self.parameters:
            return None
        return ast.parse(ast.unparse(target), mode="eval").body

    def body(self, statements: List[ast.stmt]) -> List[ast.stmt]:
        out: List[ast.stmt] = []
        for stmt in statements:
            out.append(self.visit(stmt))
            value = self.traced(stmt)
            if value is not None:
                out.append(ast.Expr(ast.Call(ast.Name("_fb_trace"), [value], [])))
        return out

    def visit_If(self, node: ast.If) -> ast.If:
        if ast.unparse(node.test) == "_v is not None" or ast.unparse(node.test).startswith("_rd"):
            return node                     # the value a READ stores: Fortran has no assignment there
        node.body, node.orelse = self.body(node.body), self.body(node.orelse)
        return node

    def visit_For(self, node: ast.For) -> ast.For:
        node.body = self.body(node.body)    # the else clause sets the DO variable's final value: not an assignment in Fortran
        return node

    def visit_While(self, node: ast.While) -> ast.While:
        node.body = self.body(node.body)
        return node


def _setup_zero(stmt: ast.stmt) -> bool:
    return (isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name) and isinstance(stmt.value, ast.Constant)
            and stmt.value.value == 0)


def _setup(body: List[ast.stmt], first: Optional[tuple], parameters: Set[str]) -> int:
    """How many leading statements only prepare the function: a local given a zero so that it exists to be passed, which the
    Fortran does not have (see identity_checker._without_local_zeros). A zero the Fortran does assign first stays traced."""
    k = 0
    while k < len(body):
        stmt = body[k]
        if ic._doc(stmt) or ic._declaration(stmt) or ic._null_pointer(stmt) or isinstance(stmt, ast.Global):
            k += 1
        elif isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id.rstrip("_").lower() in parameters:
            k += 1
        elif _setup_zero(stmt) and not (first and first[0] == "assign" and first[1][0] == "var" and first[2] == ("num", 0)
                                        and ic.same_name(first[1][1], stmt.targets[0].id.lower())):
            k += 1
        else:
            break
    return k


def instrument_python(text: str, parameters: Set[str], subroutines: Set[str], first_events: Dict[str, Optional[tuple]],
                      pointers: Set[str]) -> str:
    tree = ast.parse(text)
    probes = Probes(parameters, subroutines, pointers)
    future = [s for s in tree.body if isinstance(s, ast.ImportFrom) and s.module == "__future__"]
    tree.body = [s for s in tree.body if s not in future]
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and not stmt.name.startswith("_"):
            setup = _setup(stmt.body, first_events.get(stmt.name.lower()), parameters)
            stmt.body = stmt.body[:setup] + probes.body(stmt.body[setup:])
    return "".join(ast.unparse(s) + "\n" for s in future) + PRELUDE + ast.unparse(tree) + EPILOGUE


# --------------------------------------------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------------------------------------------

_REALS = {"float32": ("<f4", 32), "float64": ("<f8", 64)}
_COMPLEX = {"complex64": ("<f4", 32), "complex128": ("<f8", 64)}


def differs(fortran: bytes, kind: str, count: int, python: bytes, machine: bool) -> Optional[str]:
    """None when the two values are the same, else what differs."""
    if kind == "str":
        return None if fortran == python else f"text {fortran!r} and {python!r}"
    if kind == "bool":
        values = np.frombuffer(fortran, dtype="<i4") if len(fortran) == count * 4 else None
        if values is None or not np.array_equal(values != 0, np.frombuffer(python, dtype=bool)):
            return "logical values differ"
        return None
    if kind.startswith(("int", "uint")):
        width = len(fortran) // count if count else 0
        if width not in (4, 8) or len(fortran) != count * width:
            return f"{len(fortran)} bytes for {count} integers"
        left = np.frombuffer(fortran, dtype=f"<i{width}")
        right = np.frombuffer(python, dtype=np.dtype(kind)).astype(np.int64).astype(f"<i{width}")
        at = np.flatnonzero(left != right)
        return None if at.size == 0 else f"element {at[0] + 1}: {left[at[0]]} and {right[at[0]]}"
    if len(fortran) != len(python):
        return f"{len(fortran)} bytes and {len(python)} bytes: a different kind"
    if fortran == python:
        return None
    pair = _REALS.get(kind) or _COMPLEX.get(kind)
    if pair is None:
        raise Unchecked(f"python value of type {kind}")
    left, right = np.frombuffer(fortran, dtype=pair[0]), np.frombuffer(python, dtype=pair[0])
    limit = 2 ** (-23 if pair[1] == 32 else -52)
    for index in np.flatnonzero(left.tobytes() != right.tobytes() if left.size == 1 else (left != right) | (np.signbit(left) != np.signbit(right))):
        a, b = float(left[index]), float(right[index])
        if math.isnan(a) and math.isnan(b):
            continue                        # a NaN is a NaN: which payload a division leaves is not part of the equation
        if not machine:
            return f"element {index + 1}: {a!r} and {b!r}"
        if ve.zero_signbit_differs(a, b) or abs(a - b) > limit * abs(a) or ve.ulp_distance(a, b, bits=pair[1]) > 2:
            return f"element {index + 1}: {a!r} and {b!r}, beyond machine precision"
    return None


# --------------------------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------------------------

def _run(command: List[str], work: Path, stdin: Optional[bytes]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, cwd=work, input=stdin if stdin is not None else b"", capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise Unchecked(f"{command[0]} ran over {TIMEOUT} s") from exc


def _compile(sources: Sequence[Path], work: Path, name: str, extra: Sequence[str] = ()) -> Path:
    exe = work / name
    done = subprocess.run(["gfortran", *FLAGS, *extra, *[str(s) for s in sources], "-o", str(exe)], cwd=work, capture_output=True, text=True)
    if done.returncode != 0:
        raise Unchecked(f"gfortran: {done.stderr.strip()[:400]}")
    return exe


@dataclass
class Result:
    values: int = 0
    probes: int = 0
    divergence: Optional[str] = None
    unexecuted: List[str] = field(default_factory=list)
    allowed: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.divergence is None and not self.unexecuted and (self.values > 0 or self.probes == 0)


def coverage(sources: Sequence[Path], work: Path, inputs: Sequence[Optional[bytes]]) -> List[str]:
    """FILE:LINE of each executable line of the original that no input ran."""
    if shutil.which("gcov") is None:
        raise Unchecked("gcov is not available")
    area = work / "coverage"
    area.mkdir()
    local = []
    for source in sources:
        copy = area / source.name
        copy.write_bytes(source.read_bytes())
        local.append(copy)
    exe = _compile(local, area, "covered", ["--coverage"])
    for stdin in inputs:
        _run([str(exe)], area, stdin)
    missed: List[str] = []
    for source in local:
        notes = list(area.glob(f"*{source.stem}.gcno"))
        if not notes:
            raise Unchecked(f"gcov produced nothing for {source.name}")
        subprocess.run(["gcov", "-o", str(notes[0].parent), notes[0].name], cwd=area, capture_output=True, text=True)
        report = area / f"{source.name}.gcov"
        if not report.is_file():
            raise Unchecked(f"gcov produced no report for {source.name}")
        for line in report.read_text(errors="replace").splitlines():
            parts = line.split(":", 2)
            # `end module` carries gfortran's own generated helpers, which nothing has to call
            if len(parts) == 3 and parts[0].strip() in ("#####", "=====") and not re.match(r"^\s*end\s*module\b", parts[2], re.IGNORECASE):
                missed.append(f"{source.name}:{parts[1].strip()}")
    return missed


def check(originals: Sequence[Path], translated: Path, inputs: Sequence[Optional[bytes]], machine: bool,
          allowed: Sequence[str] = ()) -> Result:
    procs = ic.parse_dump(ic.dump_fortran(originals))
    parameters = {name for proc in procs.values() for name, sym in proc.symbols.items() if sym.parameter}
    subroutines = {proc.name.lower() for proc in procs.values() if not any(
        e[0] == "assign" and e[1] == ("var", proc.name) for e in ic.fortran_events(proc))}
    pointers = {name for proc in procs.values() for name, sym in proc.symbols.items() if sym.pointer}
    first_events: Dict[str, Optional[tuple]] = {}
    for proc in procs.values():
        events = ic.fortran_events(proc)
        name = proc.name.lower()
        first_events[name] = events[0] if events else None
        if proc.is_main:
            first_events["main"] = first_events[name]
    result = Result()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        probes: List[Probe] = []
        copies = []
        for source in originals:
            copy = work / source.name
            copy.write_text(instrument(source, source.name, probes, {name.lower(): proc for name, proc in procs.items()}), encoding="utf-8")
            copies.append(copy)
        plain = _compile([s.resolve() for s in originals], work, "plain")
        traced = _compile(copies, work, "traced")
        python = work / "port_traced.py"
        python.write_text(instrument_python(translated.read_text(encoding="utf-8"), parameters, subroutines, first_events, pointers), encoding="utf-8")
        result.probes = len(probes)
        by_number = {p.number: p for p in probes}
        fired: Set[int] = set()
        for number, stdin in enumerate(inputs):
            run = work / f"run{number}"
            run.mkdir()
            original = _run([str(plain)], run, stdin)
            for leftover in run.iterdir():
                leftover.unlink()
            instrumented = _run([str(traced)], run, stdin)
            if original.stdout != instrumented.stdout or original.returncode != instrumented.returncode:
                raise Unchecked("the instrumented Fortran does not behave like the original")
            ported = _run([sys.executable, "-I", str(python)], run, stdin)
            if ported.returncode != 0 and original.returncode == 0:
                raise Unchecked(f"the instrumented translation stopped: {ported.stderr.decode(errors='replace')[-300:]}")
            left, right = read_fortran_trace(run / f"fort.{UNIT}"), read_python_trace(run / "python.trace")
            index = 0
            while True:
                f, p = next(left, None), next(right, None)
                if f is None and p is None:
                    break
                index += 1
                if f is not None:
                    fired.add(f[0])
                if f is None or p is None:
                    where = f"after {result.values + index - 1} equal values"
                    if f is not None:
                        probe = by_number[f[0]]
                        where += f": Fortran assigns {probe.target} at {probe.file}:{probe.line}, Python has no value left"
                    else:
                        where += ": Python has more values than the Fortran"
                    result.divergence = where
                    return result
                difference = differs(f[1], p[0], p[1], p[2], machine)
                if difference:
                    probe = by_number[f[0]]
                    result.divergence = f"value {result.values + index}: {probe.target} at {probe.file}:{probe.line}: {difference}"
                    return result
            result.values += index
        # gcov counts lines, and the IF of `if (c) x = y` ran even when the assignment never did: the probes settle that case
        missed = coverage([s.resolve() for s in originals], work, inputs)
        missed += [f"{p.file}:{p.line}" for p in probes if p.number not in fired]
        result.unexecuted = sorted({m for m in missed if m not in allowed})
        result.allowed = list(allowed)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--original", nargs="+", required=True, help="Fortran sources, in compilation order")
    parser.add_argument("--translated", required=True, help="the translated Python module")
    parser.add_argument("--stdin", nargs="*", default=[], help="input files; the program is run once for each (coverage is their union)")
    parser.add_argument("--machine-precision", action="store_true", help="reals within eps and 2 ULP, for VERDICT: identical algorithm")
    parser.add_argument("--unexecuted-ok", nargs="*", default=[], help="FILE:LINE of statements no input reaches, each reported in the evidence")
    ve.add_cli_options(parser)
    args = parser.parse_args(argv)
    inputs: List[Optional[bytes]] = [Path(p).read_bytes() for p in args.stdin] or [None]
    try:
        result = check([Path(p) for p in args.original], Path(args.translated), inputs, args.machine_precision, args.unexecuted_ok)
    except (Unchecked, ic.Unchecked, OSError, SyntaxError) as exc:
        print(f"trace_checker: nothing was established: {exc}", file=sys.stderr)
        return 2
    print(f"{result.values} values compared at {result.probes} statements")
    if result.divergence:
        print(f"DIFFERENT {result.divergence}")
    for missed in result.unexecuted:
        print(f"NOT EXECUTED {missed}")
    passed = result.ok
    base = ve.log_path(args.evidence_dir).parent.parent
    evidence = ve.record_cli(
        args, tool="trace_checker", plugin="fortranbridge", kind="trace", name="fortran_vs_translation",
        key="trace:" + args.translated, passed=passed, tolerance={"machine_precision": bool(args.machine_precision)},
        subject_paths=list(args.original) + [args.translated] + list(args.stdin) + [__file__],
        detail={"checker_version": CHECKER_VERSION, "values": result.values, "statements": result.probes,
                "originals": [ve.subject(p, base)["path"] for p in args.original], "translated": [ve.subject(args.translated, base)["path"]],
                "unexecuted_allowed": result.allowed, "machine_precision": bool(args.machine_precision)})
    if evidence and evidence.get("error"):
        print(evidence["error"], file=sys.stderr)
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
