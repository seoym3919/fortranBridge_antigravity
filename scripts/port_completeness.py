#!/usr/bin/env python3
"""Enforce a complete Fortran port to C++ or Python.

The rule (see rules/directives.md) is mechanical:

- Every equation in the original Fortran belongs to a procedure that is defined in the
  translation and is not left as an untranslated marker.
- Every procedure other than a PROGRAM has a unit test that calls it and checks bitwise
  identity against a numeric literal (ulp_distance == 0 and not zero_signbit_differs,
  compare_vectors with atol 0 and max_ulp 0, or bit_cast). Comparing the procedure only
  with itself does not count. Plain equality and a tolerance do not count.
- One regression record from translation_verifier passed with bitwise-identical values
  (atol exactly 0 and max_ulp exactly 0) and still matches the hashes of every original
  file, every translated file, the reference output, the translated output and the inputs.
- Every procedure is reachable from a PROGRAM in the original, or a regression test
  that calls it is a subject of that same record.

A PROGRAM is emitted as main, the name the translators use. Other procedures use the
translator's Python or C++ name (``pyname`` / ``cppname``), unless ``--rename`` says
otherwise. This does not prove that two source texts are the same formula. Mathematical
identity is that regression: the recorded values are bitwise identical, including the
sign of zero. If they differ, investigate the translation. A looser tolerance, a
--justify sentence, or a tolerance approval does not pass.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import verification_evidence as ve
from fortran_lexer import classify_statement
from fortran_semantics import FortranParseError, lex_and_split
from fortran_to_cpp import cppname
from fortran_to_python import pyname


TOOL = "translation_verifier"
_INVESTIGATE = (
    "The results are not bitwise identical. Investigate why the translated function "
    "differs from the original. Do not raise atol, rtol, or max_ulp."
)
IGNORE_CASE = True
_MARKERS = ("NotImplementedError", "cppBridge:", "fortranBridge:", "fortran_untranslated",
            "std::logic_error", "UNTRANSLATED")
_SOURCE_SUFFIXES = {".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08", ".fpp", ".F", ".F90", ".F95"}
_TRANSLATED_SUFFIXES = {".py", ".pyx", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"}
_TEST_SUFFIXES = {".py", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}
_ASSERT = re.compile(
    r"(?:^|[^\w.])(?:assert|self\.assert\w*|np\.testing\.assert\w*|"
    r"EXPECT_\w*|ASSERT_\w*|BOOST_\w*|REQUIRE|CHECK)\b"
)
_TWO = {"++", "--", "->", "+=", "-=", "*=", "/=", "%=", "==", "!=", "<=", ">=", "<<", ">>", "&&", "||"}


class InventoryError(Exception):
    """The original source could not be inventoried. That is not a passing port."""


@dataclass
class Equation:
    function: str
    path: str
    line: int
    text: str


@dataclass
class Function:
    name: str
    target_name: str
    path: str
    line: int
    calls: Set[str] = field(default_factory=set)
    equations: List[Equation] = field(default_factory=list)
    entry: bool = False
    file_scope: bool = False
    body_masked: str = ""


@dataclass
class PortReport:
    ok: bool
    exit_code: int
    failures: List[str]
    error: Optional[str]
    functions: List[Function]
    equations: List[Equation]

    def lines(self) -> List[str]:
        head = "port_completeness: PASS" if self.ok else "port_completeness: FAIL"
        out = [head]
        if self.error:
            out.append(f"error: {self.error}")
        for item in self.failures:
            out.append(f"- {item}")
        names = ", ".join(f"{fn.name}->{fn.target_name}" for fn in self.functions) or "(none)"
        out.append(f"functions: {len(self.functions)} ({names})")
        out.append(f"equations: {len(self.equations)}")
        for eq in self.equations:
            shown = " ".join(eq.text.split())
            if len(shown) > 160:
                shown = shown[:157] + "..."
            out.append(f"  {eq.function}:{eq.path}:{eq.line}: {shown}")
        return out


def format_report(report: PortReport) -> str:
    return "\n".join(report.lines()) + "\n"


def _mask_c(src: str) -> str:
    """Blank comments and string literals. Length and newlines stay put."""
    out: List[str] = []
    i, n = 0, len(src)
    while i < n:
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise InventoryError("unterminated block comment")
            out.append("".join("\n" if c == "\n" else " " for c in src[i:j + 2]))
            i = j + 2
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            if j < 0:
                out.append(" " * (n - i))
                break
            out.append(" " * (j - i))
            i = j
            continue
        if src[i] in "\"'":
            quote = src[i]
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "\n" and quote == "'":
                    break
                if src[j] == quote:
                    break
                j += 1
            if j >= n or src[j] != quote:
                raise InventoryError("unterminated character or string literal")
            out.append("".join("\n" if c == "\n" else " " for c in src[i:j + 1]))
            i = j + 1
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def _blank_preprocessor(masked: str) -> str:
    lines = masked.split("\n")
    i = 0
    while i < len(lines):
        if re.match(r"[ \t]*#", lines[i]):
            start = i
            while lines[i].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
            for k in range(start, i + 1):
                lines[k] = " " * len(lines[k])
        i += 1
    return "\n".join(lines)


def _unary(text: str, index: int) -> bool:
    j = index - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    if j < 0:
        return True
    if text[j] in "([{\n,;+-*/%<>=&|^!~?:":
        return True
    return text[j - 1:j + 1] in _TWO if j else False


def _has_binary_arith(text: str) -> bool:
    i, n = 0, len(text)
    while i < n:
        pair = text[i:i + 2]
        if pair in _TWO:
            i += 2
            continue
        ch = text[i]
        if ch in "*/%":
            return True
        if ch in "+-":
            if _unary(text, i):
                i += 1
                continue
            return True
        i += 1
    return False


def _matching_brace(masked: str, open_index: int) -> int:
    depth = 0
    for i in range(open_index, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise InventoryError("unbalanced braces")


def _mask_test(path: Path, text: str) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _mask_python(text)
    return _mask_c(text)


def _mask_python(src: str) -> str:
    """Blank Python comments and string literals. Length and newlines stay put."""
    out: List[str] = []
    i, n = 0, len(src)
    while i < n:
        if src.startswith("'''", i) or src.startswith('"""', i):
            quote = src[i:i + 3]
            j = src.find(quote, i + 3)
            if j < 0:
                raise InventoryError("unterminated Python string")
            out.append("".join("\n" if c == "\n" else " " for c in src[i:j + 3]))
            i = j + 3
            continue
        if src[i] == "#":
            j = src.find("\n", i)
            if j < 0:
                out.append(" " * (n - i))
                break
            out.append(" " * (j - i))
            i = j
            continue
        if src[i] in "\"'":
            quote = src[i]
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    break
                j += 1
            if j >= n or src[j] != quote:
                raise InventoryError("unterminated Python string")
            out.append("".join("\n" if c == "\n" else " " for c in src[i:j + 1]))
            i = j + 1
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def _python_test_blocks(masked: str) -> List[str]:
    lines = masked.split("\n")
    starts: List[Tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^([ \t]*)(?:async[ \t]+)?def[ \t]+test\w*[ \t]*\(", line)
        if match:
            starts.append((index, len(match.group(1))))
    blocks: List[str] = []
    for nth, (index, indent) in enumerate(starts):
        end = len(lines)
        for later in range(index + 1, len(lines)):
            if lines[later].strip() == "":
                continue
            lead = len(lines[later]) - len(lines[later].lstrip(" \t"))
            if lead <= indent:
                end = later
                break
        blocks.append("\n".join(lines[index:end]))
    return blocks


def _gtest_blocks(masked: str) -> List[str]:
    blocks: List[str] = []
    for match in re.finditer(r"\b(?:TEST|TEST_F|TEST_P|BOOST_AUTO_TEST_CASE|BOOST_FIXTURE_TEST_CASE)\s*\(", masked):
        brace = masked.find("{", match.end())
        if brace < 0:
            continue
        end = _matching_brace(masked, brace)
        blocks.append(masked[match.start():end + 1])
    return blocks


def _test_blocks(path: Path, text: str) -> List[str]:
    masked = _mask_test(path, text)
    blocks = _python_test_blocks(masked)
    if not blocks:
        blocks = _gtest_blocks(masked)
    if not blocks:
        blocks = [masked]
    return blocks


def _re_flags() -> int:
    return re.IGNORECASE if IGNORE_CASE else 0


def _calls(text: str, name: str) -> bool:
    flags = _re_flags()
    if re.search(rf"\b{re.escape(name)}\s*\(", text, flags):
        return True
    return bool(re.search(rf"\bCALL\s+{re.escape(name)}\b", text, flags))


def _blank_call_args(text: str, name: str) -> str:
    pieces: List[str] = []
    pattern = re.compile(rf"\b{re.escape(name)}\s*\(", _re_flags())
    cursor = 0
    while True:
        match = pattern.search(text, cursor)
        if not match:
            pieces.append(text[cursor:])
            break
        open_at = match.end() - 1
        depth = 0
        j = open_at
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        pieces.append(text[cursor:open_at])
        pieces.append("()")
        cursor = j
    return "".join(pieces)


def _assertions(block: str) -> List[str]:
    lines = block.split("\n")
    found: List[str] = []
    buf = ""
    depth = 0
    active = False
    for line in lines:
        if not active:
            if _ASSERT.search(line):
                active = True
                buf = line
                depth = line.count("(") - line.count(")")
                if depth <= 0:
                    found.append(buf)
                    buf = ""
                    active = False
            continue
        buf += "\n" + line
        depth += line.count("(") - line.count(")")
        if depth <= 0:
            found.append(buf)
            buf = ""
            active = False
    if buf:
        found.append(buf)
    return found


def _named_calls(text: str, name: str) -> List[Tuple[int, int, str]]:
    """Return (name_start, index_after_call, argument_text) for each call to name."""
    pattern = re.compile(rf"\b(?:[\w.]+\.)?{re.escape(name)}\s*\(")
    found: List[Tuple[int, int, str]] = []
    cursor = 0
    while True:
        match = pattern.search(text, cursor)
        if not match:
            break
        open_at = match.end() - 1
        depth = 0
        j = open_at
        closed = False
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    closed = True
                    break
            j += 1
        if not closed:
            break
        found.append((match.start(), j, text[open_at + 1:j - 1]))
        cursor = j
    return found


def _bit_cast_inners(text: str) -> List[str]:
    pattern = re.compile(r"\bbit_cast\s*<[^<>]*>\s*\(")
    found: List[str] = []
    cursor = 0
    while True:
        match = pattern.search(text, cursor)
        if not match:
            break
        open_at = match.end() - 1
        depth = 0
        j = open_at
        closed = False
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    closed = True
                    break
            j += 1
        if not closed:
            break
        found.append(text[open_at + 1:j - 1])
        cursor = j
    return found


def _split_args(text: str) -> List[str]:
    args: List[str] = []
    buf: List[str] = []
    depth = 0
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            args.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    args.append("".join(buf))
    return args


def _result_aliases(block: str, names: Sequence[str]) -> Set[str]:
    aliases: Set[str] = set()
    for line in block.splitlines():
        match = re.match(r"[ \t]*([A-Za-z_]\w*)(?:[ \t]*:[^=\n]+)?[ \t]*=[ \t]*(.+)$", line)
        if match and any(_calls(match.group(2), name) for name in names):
            aliases.add(match.group(1))
    return aliases


def _mentions_result(text: str, names: Sequence[str], aliases: Set[str]) -> bool:
    if any(_calls(text, name) for name in names):
        return True
    return any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in aliases)


def _literal_side(text: str, names: Sequence[str]) -> bool:
    blanked = text
    for name in names:
        blanked = _blank_call_args(blanked, name)
    return bool(re.search(r"\d", blanked))


def _result_versus_literal(inner: str, names: Sequence[str], aliases: Set[str], bits_ok: bool) -> bool:
    args = [arg.strip() for arg in _split_args(inner)]
    if bits_ok and len(args) == 3:
        third = args[2].strip()
        if third not in {"32", "64"} and not re.fullmatch(r"bits\s*=\s*(?:32|64)", third):
            return False
        args = args[:2]
    if len(args) != 2:
        return False
    mentions = [_mentions_result(arg, names, aliases) for arg in args]
    if mentions.count(True) != 1:
        return False
    expected = args[0] if mentions[1] else args[1]
    return _literal_side(expected, names)


def _ulp_compared_with_zero(text: str, name_start: int, call_end: int) -> bool:
    tail = text[call_end:]
    if re.match(r"\s*==\s*0\b", tail):
        return True
    if re.match(r"\s*,\s*0\s*\)", tail):
        prefix = text[:name_start]
        return bool(re.search(
            r"\b(?:EXPECT_EQ|ASSERT_EQ|CHECK_EQ|BOOST_CHECK_EQUAL|REQUIRE_EQ|assertEqual|assert_equal)\s*\(\s*$",
            prefix,
        ))
    return False


def _signbit_rejected(text: str, name_start: int, call_end: int) -> bool:
    prefix = text[max(0, name_start - 48):name_start]
    tail = text[call_end:]
    if re.search(r"(?:\bnot\s*|!\s*)$", prefix):
        return True
    if re.search(r"\b(?:EXPECT_FALSE|ASSERT_FALSE|REQUIRE_FALSE|CHECK_FALSE)\s*\(\s*$", prefix):
        return True
    return bool(re.match(r"\s*(?:==\s*False|is\s*False)\b", tail))


def _compare_vectors_bitwise(inner: str, names: Sequence[str], aliases: Set[str]) -> bool:
    if not re.search(r"\bmax_ulp\s*=\s*0\b", inner):
        return False
    if not re.search(r"\batol\s*=\s*0(?:\.0+)?\b", inner):
        return False
    if not _mentions_result(inner, names, aliases):
        return False
    blanked = inner
    for name in names:
        blanked = _blank_call_args(blanked, name)
    blanked = re.sub(r"\b(?:rtol|atol|max_ulp|bits)\s*=\s*[^\s,)]+", "", blanked)
    return bool(re.search(r"\d", blanked))


def _bitcast_bitwise(assertion: str, names: Sequence[str], aliases: Set[str]) -> bool:
    inners = _bit_cast_inners(assertion)
    if not any(_mentions_result(inner, names, aliases) for inner in inners):
        return False
    blanked = re.sub(r"\bbit_cast\s*<[^<>]*>", "bit_cast", assertion)
    for name in names:
        blanked = _blank_call_args(blanked, name)
    return bool(re.search(r"\d", blanked))


def _asserts_bitwise(block: str, names: Sequence[str]) -> bool:
    """True when a test calls the function and compares that result with a numeric literal bit for bit.

    Accepted forms, from verification_evidence: ulp_distance(...) == 0 together with
    not zero_signbit_differs(...); compare_vectors(..., atol=0, max_ulp=0); or bit_cast of
    both sides. Plain == treats +0 and -0 as equal, so it does not count. A tolerance does not count.
    """
    aliases = _result_aliases(block, names)
    ulp_ok = False
    sign_ok = False
    for assertion in _assertions(block):
        for name_start, call_end, inner in _named_calls(assertion, "ulp_distance"):
            if _ulp_compared_with_zero(assertion, name_start, call_end) and _result_versus_literal(
                inner, names, aliases, True
            ):
                ulp_ok = True
        for name_start, call_end, inner in _named_calls(assertion, "zero_signbit_differs"):
            if _signbit_rejected(assertion, name_start, call_end) and _result_versus_literal(
                inner, names, aliases, False
            ):
                sign_ok = True
        for _start, _end, inner in _named_calls(assertion, "compare_vectors"):
            if _compare_vectors_bitwise(inner, names, aliases):
                return True
        if _bitcast_bitwise(assertion, names, aliases):
            return True
    return ulp_ok and sign_ok


def _python_bodies(masked: str, original: str, name: str) -> List[str]:
    lines_m = masked.split("\n")
    lines_o = original.split("\n")
    bodies: List[str] = []
    for index, line in enumerate(lines_m):
        match = re.match(rf"^([ \t]*)(?:async[ \t]+)?def[ \t]+{re.escape(name)}[ \t]*\(", line, _re_flags())
        if not match:
            continue
        indent = len(match.group(1))
        end = len(lines_m)
        for later in range(index + 1, len(lines_m)):
            if lines_m[later].strip() == "":
                continue
            lead = len(lines_m[later]) - len(lines_m[later].lstrip(" \t"))
            if lead <= indent:
                end = later
                break
        bodies.append("\n".join(lines_o[index + 1:end]))
    return bodies


def _cxx_bodies(masked: str, original: str, name: str) -> List[str]:
    bodies: List[str] = []
    for match in re.finditer(rf"\b{re.escape(name)}\s*\(", masked, _re_flags()):
        i = match.end() - 1
        depth = 0
        j = i
        saw_paren = False
        while j < len(masked):
            if masked[j] == "(":
                depth += 1
                saw_paren = True
            elif masked[j] == ")":
                depth -= 1
            elif masked[j] == ";" and depth <= 0:
                break
            elif masked[j] == "{" and depth <= 0 and saw_paren:
                end = _matching_brace(masked, j)
                bodies.append(original[j + 1:end])
                break
            j += 1
    return bodies


def _bodies(paths: Sequence[Path], texts: Sequence[str], name: str) -> List[Tuple[str, bool]]:
    found: List[Tuple[str, bool]] = []
    for path, text in zip(paths, texts):
        suffix = path.suffix.lower()
        if suffix == ".py":
            masked = _mask_python(text)
            found.extend((body, True) for body in _python_bodies(masked, text, name))
        else:
            masked = _blank_preprocessor(_mask_c(text))
            found.extend((body, False) for body in _cxx_bodies(masked, text, name))
    return found


def _body_untranslated(body: str, python: bool) -> Optional[str]:
    masked = _mask_python(body) if python else _mask_c(body)
    for marker in _MARKERS:
        if marker in masked:
            return marker
    kept = []
    for line in masked.split("\n"):
        stripped = line.strip()
        if not stripped or stripped in {"pass", "{}", ";"}:
            continue
        kept.append(stripped)
    if not kept:
        return "empty body"
    return None


def _expand(paths: Sequence[str], suffixes: Set[str]) -> List[Path]:
    found: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise InventoryError(f"not found: {path}")
        if path.is_file():
            found.append(path.resolve())
            continue
        if not path.is_dir():
            raise InventoryError(f"not a file: {path}")
        for child in sorted(path.rglob("*")):
            if not child.is_file() or child.name.startswith("."):
                continue
            if any(part.startswith(".") or part == "__pycache__" for part in child.parts):
                continue
            if child.suffix.lower() in suffixes or child.suffix in suffixes:
                found.append(child.resolve())
    if not found:
        raise InventoryError("no source files were found")
    return found


def _relevant(record: dict, wanted: Set[str]) -> bool:
    if record.get("tool") != TOOL or record.get("kind") != "compare":
        return False
    paths = {subject.get("path") for subject in record.get("subjects") or []}
    return bool(paths & wanted)


def _tolerance_problem(record: dict) -> Optional[str]:
    detail = record.get("detail") or {}
    version = str(detail.get("checker_version") or "")
    major = version.split(".", 1)[0]
    if not major.isdigit() or int(major) < 2:
        return f"regression checker_version {version or '(missing)'} is older than 2"
    dtype = detail.get("dtype") or "float64"
    if dtype not in ("float32", "float64"):
        return f"regression dtype {dtype} is not float32 or float64"
    tolerance = record.get("tolerance") or {}
    try:
        rtol = float(tolerance["rtol"])
        atol = float(tolerance["atol"])
        max_ulp_f = float(tolerance["max_ulp"])
    except (KeyError, TypeError, ValueError):
        return "regression tolerance is missing rtol, atol or max_ulp"
    if max_ulp_f != int(max_ulp_f):
        return "regression max_ulp is not an integer"
    default = ve.limits(dtype)
    problems = []
    if atol != 0.0:
        problems.append(f"atol {atol} is not 0")
    if int(max_ulp_f) != 0:
        problems.append(f"max_ulp {int(max_ulp_f)} is not 0")
    if not (math.isfinite(rtol) and 0 < rtol <= float(default["rtol"])):
        problems.append(f"rtol {rtol} is looser than {default['rtol']} or is not positive")
    if problems:
        return "regression is not bitwise identical: " + "; ".join(problems) + ". " + _INVESTIGATE
    return None


def _subject_problem(record: dict, base: Path, required: Sequence[Path], label: str) -> Optional[str]:
    by_path = {subject.get("path"): subject.get("sha256") for subject in record.get("subjects") or []}
    missing = []
    for path in required:
        subject = ve.subject(str(path), base)
        if by_path.get(subject["path"]) != subject["sha256"]:
            missing.append(subject["path"])
    if missing:
        return f"regression does not bind the current {label}: {', '.join(missing)}"
    return None


def _stale_subjects(record: dict, base: Path) -> List[str]:
    stale = []
    for subject in record.get("subjects") or []:
        raw = subject.get("path")
        if not raw or not subject.get("sha256"):
            stale.append(str(raw))
            continue
        file = Path(raw)
        if not file.is_absolute():
            file = base / raw
        if ve.sha256_file(file) != subject["sha256"]:
            stale.append(str(raw))
    return stale


def _regression_failures(log: Path, originals: Sequence[Path], translated: Sequence[Path],
                         regression_tests: Sequence[Path]) -> Tuple[List[str], Set[str]]:
    """Return (failures, names called from regression-test subjects)."""
    if not log.is_file():
        return ["no regression evidence log was found"], set()
    records, problems = ve.read_log(log)
    if problems:
        raise InventoryError("evidence log cannot be trusted: " + "; ".join(problems))
    base = log.resolve().parent.parent
    wanted = {ve.subject(str(path), base)["path"] for path in list(originals) + list(translated)}
    relevant = [record for record in records if _relevant(record, wanted)]
    if not relevant:
        return [f"no {TOOL} compare record covers the original or translated files"], set()
    record = relevant[-1]
    failures: List[str] = []
    if record.get("exit_status") != 0 or not record.get("passed") or "withheld" in (record.get("flags") or []):
        failures.append(
            f"latest regression record did not pass "
            f"(passed={record.get('passed')}, exit_status={record.get('exit_status')}, flags={record.get('flags')})"
        )
    problem = _tolerance_problem(record)
    if problem:
        failures.append(problem)
    stale = _stale_subjects(record, base)
    if stale:
        failures.append("regression evidence is stale: " + ", ".join(stale))
    for paths, label in ((originals, "original files"), (translated, "translated files"),
                         (regression_tests, "regression tests")):
        bound = _subject_problem(record, base, paths, label)
        if bound:
            failures.append(bound)
    bound_paths = {subject.get("path") for subject in record.get("subjects") or []}
    accounted = {ve.subject(str(path), base)["path"]
                 for path in list(originals) + list(translated) + list(regression_tests)}
    extras = bound_paths - accounted
    if len(extras) < 3:
        failures.append(
            "regression record must bind the reference output, the translated output and the inputs "
            f"({len(extras)} other subject(s) besides the sources)"
        )
    called: Set[str] = set()
    for path in regression_tests:
        called.add(_mask_test(path, path.read_text(encoding="utf-8")))
    return failures, called


_F_LITERAL = re.compile(
    r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[dDeE][+-]?\d+)?(?:_[A-Za-z]\w*)?$",
    re.IGNORECASE,
)


def _is_f_literal(expr: str) -> bool:
    token = expr.strip().replace(" ", "")
    while token.startswith("(") and token.endswith(")") and token.count("(") == token.count(")"):
        inner = token[1:-1].strip()
        if not inner:
            break
        token = inner
    return bool(_F_LITERAL.fullmatch(token))


def _fortran_equations(unit) -> List[Tuple[int, str]]:
    found: List[Tuple[int, str]] = []
    for statement in unit.stmts:
        if statement.stmt_type in ("SUBROUTINE", "FUNCTION", "PROGRAM", "END"):
            continue
        if statement.stmt_type == "ASSIGNMENT" and not _is_f_literal(statement.details.get("rhs", "")):
            found.append((statement.line_no, f"{statement.details['lhs']} = {statement.details['rhs']}"))
        elif statement.stmt_type == "IF_STMT":
            kind, details = classify_statement(statement.details.get("statement", ""))
            if kind == "ASSIGNMENT" and not _is_f_literal(details.get("rhs", "")):
                found.append((statement.line_no, f"{details['lhs']} = {details['rhs']}"))
        elif statement.stmt_type == "PARAMETER":
            for key, value in (statement.details.get("constants") or {}).items():
                if value and not _is_f_literal(value):
                    found.append((statement.line_no, f"{key} = {value}"))
        elif statement.stmt_type in ("IF_THEN", "ELSE_IF", "ARITHMETIC_IF"):
            condition = statement.details.get("condition", "")
            if _has_binary_arith(condition):
                found.append((statement.line_no, condition))
        elif statement.stmt_type == "TYPE_DECL" and _has_binary_arith(statement.details.get("declarations", "")):
            found.append((statement.line_no, statement.details.get("declarations", "")))
    return found


def _procedure_target(kind: str, name: str, renames: Dict[str, str], target: str) -> str:
    if name in renames:
        return renames[name]
    if kind == "PROGRAM":
        return "main"
    return pyname(name) if target == "python" else cppname(name)


def inventory_fortran(path: Path, text: str, renames: Dict[str, str], target: str) -> Tuple[List[Function], List[Equation]]:
    shown = str(path)
    try:
        _statements, units = lex_and_split(text)
    except FortranParseError as exc:
        raise InventoryError(f"{shown}: {exc}") from exc
    functions: List[Function] = []
    pending: List[Tuple[Function, object]] = []
    for unit in units:
        real_program = any(statement.stmt_type == "PROGRAM" for statement in unit.stmts)
        pieces = _fortran_equations(unit)
        if unit.kind == "PROGRAM" and not real_program:
            for line, equation in pieces:
                lhs = re.match(r"([A-Za-z_][\w]*)", equation.split("=", 1)[0].strip())
                name = lhs.group(1).lower() if lhs else "<file-scope>"
                functions.append(Function(
                    name=name, target_name=renames.get(name, name), path=shown, line=line, entry=True,
                    file_scope=True, equations=[Equation(name, shown, line, equation)],
                ))
            continue
        name = unit.name.lower()
        equations = [Equation(name, shown, line, equation) for line, equation in pieces]
        body = "\n".join(statement.clean for statement in unit.stmts
                         if statement.stmt_type not in ("SUBROUTINE", "FUNCTION", "PROGRAM"))
        function = Function(
            name=name, target_name=_procedure_target(unit.kind, name, renames, target), path=shown,
            line=unit.stmts[0].line_no if unit.stmts else 1, equations=equations,
            entry=unit.kind == "PROGRAM", body_masked=body,
        )
        functions.append(function)
        pending.append((function, unit))
    names = {function.name for function in functions}
    for function, unit in pending:
        body = function.body_masked
        for other in names:
            if other != function.name and _calls(body, other):
                function.calls.add(other)
    equations = [equation for function in functions for equation in function.equations]
    return functions, equations


def check_port(*, originals: Sequence[str], translated: Sequence[str], unit_tests: Sequence[str] = (),
               regression_tests: Sequence[str] = (), evidence_log: str,
               renames: Optional[Dict[str, str]] = None, target: str = "python") -> PortReport:
    if target not in ("python", "cpp"):
        return PortReport(False, 2, [], "target must be python or cpp", [], [])
    renames = {key.lower(): value for key, value in dict(renames or {}).items()}
    try:
        original_paths = _expand(originals, _SOURCE_SUFFIXES)
        translated_paths = _expand(translated, _TRANSLATED_SUFFIXES)
        test_paths = _expand(unit_tests, _TEST_SUFFIXES) if unit_tests else []
        regression_paths = _expand(regression_tests, _SOURCE_SUFFIXES | _TRANSLATED_SUFFIXES | _TEST_SUFFIXES) if regression_tests else []
        functions: List[Function] = []
        equations: List[Equation] = []
        for path in original_paths:
            text = path.read_text(encoding="utf-8")
            found, eqs = inventory_fortran(path, text, renames, target)
            functions.extend(found)
            equations.extend(eqs)
        if not functions:
            return PortReport(False, 1, ["no functions or equations were found; refusing to treat that as a complete port"],
                              None, [], [])
        translated_texts = [path.read_text(encoding="utf-8") for path in translated_paths]
        failures: List[str] = []
        blocks: List[str] = []
        for path in test_paths:
            blocks.extend(_test_blocks(path, path.read_text(encoding="utf-8")))
        translated_masked = []
        for path, text in zip(translated_paths, translated_texts):
            if path.suffix.lower() == ".py":
                translated_masked.append(_mask_python(text))
            else:
                translated_masked.append(_blank_preprocessor(_mask_c(text)))
        for fn in functions:
            if fn.file_scope:
                blob = "\n".join(translated_masked)
                if fn.target_name.startswith("<") or not re.search(rf"\b{re.escape(fn.target_name)}\b", blob, _re_flags()):
                    failures.append(
                        f"{fn.name}: file-scope equation at line {fn.line} was not translated "
                        f"(name {fn.target_name} is absent from the translation)"
                    )
                continue
            bodies = _bodies(translated_paths, translated_texts, fn.target_name)
            if not bodies:
                failures.append(f"{fn.name}: not defined in the translation (looked for {fn.target_name})")
            else:
                for body, python in bodies:
                    marker = _body_untranslated(body, python)
                    if marker:
                        failures.append(f"{fn.name}: translation of {fn.target_name} is not complete ({marker})")
                        break
            if fn.entry:
                continue
            covered = False
            for block in blocks:
                names = (fn.target_name, fn.name)
                if any(_calls(block, name) for name in names) and _asserts_bitwise(block, names):
                    covered = True
                    break
            if not covered:
                failures.append(
                    f"{fn.name}: no unit test calls {fn.target_name} and checks bitwise identity "
                    f"against a numeric literal (ulp_distance(...) == 0 and not zero_signbit_differs(...), "
                    f"or compare_vectors(..., atol=0, max_ulp=0), or bit_cast of both sides). {_INVESTIGATE}"
                )
        log = Path(evidence_log)
        regression_failures, regression_text = _regression_failures(log, original_paths, translated_paths, regression_paths)
        failures.extend(regression_failures)
        reached: Set[str] = {fn.name for fn in functions if fn.entry or fn.file_scope}
        if isinstance(regression_text, set):
            for blob in regression_text:
                for fn in functions:
                    if _calls(blob, fn.target_name) or _calls(blob, fn.name):
                        reached.add(fn.name)
        changed = True
        calls_of = {fn.name: set(fn.calls) for fn in functions}
        known = {fn.name for fn in functions}
        while changed:
            changed = False
            for name in list(reached):
                for callee in calls_of.get(name, ()):
                    if callee in known and callee not in reached:
                        reached.add(callee)
                        changed = True
        for fn in functions:
            if fn.file_scope or fn.name in reached:
                continue
            failures.append(
                f"{fn.name}: not executed by the regression "
                "(not reachable from a PROGRAM, and no bound --regression-test calls it)"
            )
        return PortReport(not failures, 0 if not failures else 1, failures, None, functions, equations)
    except InventoryError as exc:
        return PortReport(False, 2, [], str(exc), [], [])
    except OSError as exc:
        return PortReport(False, 2, [], str(exc), [], [])


def run_self_test() -> bool:
    """Positive control and two negative controls. Exit status follows the positive control only when both negatives fail."""
    original = (
        "subroutine kick(x, v, dt)\n"
        "  double precision x, v, dt\n"
        "  if (x .gt. 0.0d0) v = v + dt * x\n"
        "end subroutine kick\n"
        "program orbit\n"
        "  double precision x, v\n"
        "  x = 1.0d0\n"
        "  v = 0.0d0\n"
        "  call kick(x, v, 0.5d0)\n"
        "  print *, v\n"
        "end program orbit\n"
    )
    translated = (
        "def kick(x, v, dt):\n"
        "    if x > 0.0:\n"
        "        v = v + dt * x\n"
        "    return v\n"
        "\n"
        "def main():\n"
        "    x = 1.0\n"
        "    v = 0.0\n"
        "    return kick(x, v, 0.5)\n"
    )
    tests = (
        "def test_kick():\n"
        "    got = kick(1.0, 0.0, 0.5)\n"
        "    assert ulp_distance(got, 0.5) == 0 and not zero_signbit_differs(got, 0.5)\n"
    )
    broken = "def test_kick():\n    assert kick(1.0, 0.0, 0.5) == kick(1.0, 0.0, 0.5)\n"
    plain = "def test_kick():\n    assert kick(1.0, 0.0, 0.5) == 0.5\n"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "orig.f90").write_text(original, encoding="utf-8")
        (root / "port.py").write_text(translated, encoding="utf-8")
        (root / "test_kick.py").write_text(tests, encoding="utf-8")
        (root / "test_self.py").write_text(broken, encoding="utf-8")
        (root / "test_plain.py").write_text(plain, encoding="utf-8")
        ref = root / "ref.txt"
        out = root / "out.txt"
        inputs = root / "inputs.txt"
        ref.write_text("0.5\n", encoding="utf-8")
        out.write_text("0.5\n", encoding="utf-8")
        inputs.write_text("1 0 0.5\n", encoding="utf-8")
        log = root / ".verification" / "log.jsonl"
        limits = ve.limits("float64", max_ulp=0)
        subjects = [ve.subject(str(path), root) for path in
                    (ref, out, root / "orig.f90", root / "port.py", inputs)]
        ve.append(log, {
            "tool": TOOL, "plugin": "fortranbridge", "kind": "compare", "name": "fortran_vs_translation",
            "key": "compare:self-test", "passed": True, "exit_status": 0,
            "tolerance": {"rtol": limits["rtol"], "atol": limits["atol"], "max_ulp": limits["max_ulp"]},
            "subjects": subjects, "flags": [], "justification": None,
            "detail": {"checker_version": "2", "dtype": "float64"},
        })
        good = check_port(originals=[str(root / "orig.f90")], translated=[str(root / "port.py")],
                          unit_tests=[str(root / "test_kick.py")], evidence_log=str(log), target="python")
        selfish = check_port(originals=[str(root / "orig.f90")], translated=[str(root / "port.py")],
                             unit_tests=[str(root / "test_self.py")], evidence_log=str(log), target="python")
        equal = check_port(originals=[str(root / "orig.f90")], translated=[str(root / "port.py")],
                           unit_tests=[str(root / "test_plain.py")], evidence_log=str(log), target="python")
        ve.append(log, {
            "tool": TOOL, "plugin": "fortranbridge", "kind": "compare", "name": "fortran_vs_translation",
            "key": "compare:self-test", "passed": True, "exit_status": 0,
            "tolerance": {"rtol": 1e-6, "atol": 0.0, "max_ulp": 1},
            "subjects": subjects, "flags": [], "justification": "not an approval",
            "detail": {"checker_version": "2", "dtype": "float64"},
        })
        loosened = check_port(originals=[str(root / "orig.f90")], translated=[str(root / "port.py")],
                              unit_tests=[str(root / "test_kick.py")], evidence_log=str(log), target="python")
    loosened_text = "\n".join(loosened.failures).lower()
    ok = (good.ok and not selfish.ok and not equal.ok and not loosened.ok
          and "bitwise" in loosened_text and "investigate" in loosened_text)
    if not ok:
        print(format_report(good), file=sys.stderr)
        print(format_report(selfish), file=sys.stderr)
        print(format_report(equal), file=sys.stderr)
        print(format_report(loosened), file=sys.stderr)
    return ok


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail unless every original equation is translated, every function is unit tested, "
                    "and a strict regression record shows the whole port matches.")
    parser.add_argument("--original", action="append", default=[], help="original Fortran file or directory (repeatable)")
    parser.add_argument("--translated", action="append", default=[], help="translated file or directory (repeatable)")
    parser.add_argument("--target", choices=("python", "cpp"), help="language the translators emit")
    parser.add_argument("--unit-tests", action="append", default=[], help="unit-test file or directory (repeatable)")
    parser.add_argument("--regression-test", action="append", default=[],
                        help="test that calls functions main does not call; must be an evidence subject")
    parser.add_argument("--evidence-log", help="verifier log (normally .verification/log.jsonl)")
    parser.add_argument("--evidence-dir", help="directory containing log.jsonl, used when --evidence-log is omitted")
    parser.add_argument("--rename", action="append", default=[], help="original=translated function name (repeatable)")
    parser.add_argument("--report", help="also write the report to this file")
    parser.add_argument("--self-test", action="store_true", help="check the checker itself; this does not check a port")
    args = parser.parse_args(argv)
    if args.self_test:
        success = run_self_test()
        print("port_completeness self-test: PASS" if success else "port_completeness self-test: FAIL")
        return 0 if success else 1
    renames: Dict[str, str] = {}
    for item in args.rename:
        if "=" not in item:
            print("port_completeness: --rename must be original=translated", file=sys.stderr)
            return 2
        old, new = item.split("=", 1)
        if not old or not new:
            print("port_completeness: --rename must be original=translated", file=sys.stderr)
            return 2
        renames[old.lower()] = new
    if not args.original or not args.translated or not args.target:
        parser.print_help(sys.stderr)
        print("\nport_completeness: give --original, --translated and --target python|cpp.", file=sys.stderr)
        return 2
    if args.evidence_log:
        log = args.evidence_log
    elif args.evidence_dir:
        log = str(Path(args.evidence_dir) / "log.jsonl")
    else:
        log = str(Path(".verification") / "log.jsonl")
    report = check_port(originals=args.original, translated=args.translated, unit_tests=args.unit_tests,
                        regression_tests=args.regression_test, evidence_log=log, renames=renames,
                        target=args.target)
    text = format_report(report)
    sys.stdout.write(text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
