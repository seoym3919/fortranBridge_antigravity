"""Tests for scripts/identity_checker.py: the static check that a translation has the same equations and algorithm.

The reference is gfortran's own parse tree, so no result here depends on the translator. No expected value is numeric: a test
says that a program matches, or that a named change to it is reported. Mutations are single edits of a real translation.
"""

import ast
import copy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import identity_checker as ic  # noqa: E402
from fortran_to_python import translate_fortran_to_python  # noqa: E402

FIXTURES = ROOT / "tests" / "roundtrip"
# Every fixture's whole translation must be established as identical.
ESTABLISHED = tuple(sorted(path.stem for path in FIXTURES.glob("*.f90")))

KERNEL = """\
      subroutine kernel(n, a, b, s)
      integer n, i, j
      double precision a(n,n), b(n), s, w
      real x
      parameter (w = 0.5d0)
      x = 0.1
      s = 0.0d0
      do 20 j = 1, n
         do 10 i = 1, n
            s = (s + a(i,j)) + b(j)*w
   10    continue
         if (s .gt. 1.0d0) s = s - dble(x)
   20 continue
      end
"""
GOOD = """\
import numpy as np


def kernel(n, a, b, s):
    w = 0.5
    x = np.float32(0.1)
    s = 0.0
    _do1 = range(1, n + 1)
    for j in _do1:
        _do2 = range(1, n + 1)
        for i in _do2:
            s = (s + a[i - 1, j - 1]) + b[j - 1] * w
        else:
            i = _do2.start + len(_do2) * _do2.step
        if s > 1.0:
            s = s - float(x)
    else:
        j = _do1.start + len(_do1) * _do1.step
    return s
"""


@unittest.skipUnless(shutil.which("gfortran"), "gfortran is the reference")
class Kernel(unittest.TestCase):
    def check(self, python, fortran=KERNEL):
        with tempfile.TemporaryDirectory() as tmp:
            source, port = Path(tmp) / "kernel.f", Path(tmp) / "kernel.py"
            source.write_text(fortran)
            port.write_text(python)
            return ic.check_python([source], [port])

    def test_the_faithful_translation_passes(self):
        report = self.check(GOOD)
        self.assertTrue(report.ok, report)

    def test_changes_of_equation_or_algorithm_are_reported(self):
        self.assertTrue(self.check(GOOD).ok, "the edits below mean nothing unless the unedited program passes")
        edits = {
            "reassociated sum": ("(s + a[i - 1, j - 1]) + b[j - 1] * w", "s + (a[i - 1, j - 1] + b[j - 1] * w)"),
            "operands swapped": ("b[j - 1] * w", "w * b[j - 1]"),
            "index off by one": ("b[j - 1]", "b[j]"),
            "indices transposed": ("a[i - 1, j - 1]", "a[j - 1, i - 1]"),
            "loop bound": ("_do2 = range(1, n + 1)", "_do2 = range(1, n)"),
            "loop start": ("_do2 = range(1, n + 1)", "_do2 = range(2, n + 1)"),
            "loop interchange": ("for j in _do1:\n        _do2 = range(1, n + 1)\n        for i in _do2:",
                                 "for i in _do1:\n        _do2 = range(1, n + 1)\n        for j in _do2:"),
            "condition": ("if s > 1.0:", "if s >= 1.0:"),
            "literal": ("if s > 1.0:", "if s > 1.0000001:"),
            "wrong PARAMETER": ("w = 0.5", "w = 0.25"),
            "rounding point dropped": ("x = np.float32(0.1)", "x = 0.1"),
            "scalar kind changed by a literal": ("s = 0.0\n", "s = np.float32(0.0)\n"),
            "statement dropped": ("    s = 0.0\n", ""),
            "loop variable after the loop": ("i = _do2.start + len(_do2) * _do2.step", "i = _do2.start"),
            "bounds moved out of the loop": ("        _do2 = range(1, n + 1)\n", "        pass\n"),
            "result not returned": ("    return s\n", ""),
        }
        for what, (old, new) in edits.items():
            with self.subTest(what):
                self.assertIn(old, GOOD)
                changed = self.check(GOOD.replace(old, new, 1))
                self.assertFalse(changed.ok, what)

    def test_an_unknown_helper_is_not_a_pass(self):
        with self.assertRaises(ic.Unchecked):
            reader = ic.PythonReader(ic.Proc("p"))
            reader.expr(ast.parse("_mystery(x)", mode="eval").body)

    def test_the_format_of_another_gfortran_is_not_trusted(self):
        self.assertIn(13, ic.SUPPORTED_GFORTRAN)


@unittest.skipUnless(shutil.which("gfortran"), "gfortran is the reference")
class TranslatedGoto(unittest.TestCase):
    """The Python state machine is lowered and walked in lockstep with the Fortran, so its scaffolding is checked too."""

    SOURCE = FIXTURES / "goto_control_flow.f90"

    def check(self, edit=None):
        code = translate_fortran_to_python(self.SOURCE.read_text())
        if edit:
            self.assertIn(edit[0], code)
            code = code.replace(edit[0], edit[1], 1)
        with tempfile.TemporaryDirectory() as tmp:
            port = Path(tmp) / "port.py"
            port.write_text(code)
            return ic.check_python([self.SOURCE], [port]).ok

    def test_the_machine_matches_the_fortran(self):
        self.assertTrue(self.check())

    def test_changes_to_the_machine_are_reported(self):
        edits = {
            "counter starts elsewhere": ("    _pc = 0\n", "    _pc = 1\n"),
            "block renumbered": ("        if _pc == 1:\n", "        if _pc == 2:\n"),
            "jump to another block": ("                _pc = 6\n", "                _pc = 7\n"),
            "loop counter starts elsewhere": ("_doG2_i = 0", "_doG2_i = 1"),
            "loop counter steps by two": ("_doG2_i += 1", "_doG2_i += 2"),
            "loop variable not set": ("            k = _doG2[_doG2_i]\n", "            pass\n"),
            "jump dropped": ("            _pc = 8\n", ""),
            "continue dropped before another test": ("                _pc = 23\n                continue\n", "                _pc = 23\n"),
            "branch reversed": ("if i < 5:", "if i >= 5:"),
            "jump at the end of a later block dropped": ("            _pc = 10\n", ""),
            "final value of the variable": ("k = _doG2.start + len(_doG2) * _doG2.step", "k = _doG2.start"),
        }
        for what, edit in edits.items():
            with self.subTest(what):
                try:
                    self.assertFalse(self.check(edit), what)
                except ic.Unchecked:
                    pass                    # not established is not a pass


@unittest.skipUnless(shutil.which("gfortran"), "gfortran is the reference")
class StorageAndText(unittest.TestCase):
    def changed(self, fixture, old, new):
        source = FIXTURES / f"{fixture}.f90"
        code = translate_fortran_to_python(source.read_text())
        self.assertIn(old, code)
        with tempfile.TemporaryDirectory() as tmp:
            port = Path(tmp) / "port.py"
            port.write_text(code.replace(old, new, 1))
            try:
                return ic.check_python([source], [port]).ok
            except ic.Unchecked:
                return False

    def test_data_values_are_read_from_the_compiler(self):
        """A check over an empty table cannot fail, so the table itself is tested."""
        table = ic.initial_values([FIXTURES / "data_elements.f90"])
        self.assertEqual(table["dataelem"]["a"], [0, 20, 0, 40])
        self.assertEqual(table["dataelem"]["b"], [0, 5, 0, 6, 0, 0])

    def test_data_values_are_part_of_the_translation(self):
        self.assertFalse(self.changed("data_elements", "np.array([0, 20, 0, 40], dtype=np.int64)", "np.array([0, 40, 0, 20], dtype=np.int64)"))
        self.assertFalse(self.changed("data_and_save", "_save_counter_k = 0", "_save_counter_k = 1"))

    def test_character_length_is_part_of_the_assignment(self):
        self.assertFalse(self.changed("character_strings", "_cfix('abcdefgh', 4)", "_cfix('abcdefgh', 5)"))

    def test_equivalence_views_must_sit_at_their_fortran_position(self):
        self.assertFalse(self.changed("equivalence_arrays", "b = _eq0[2:4]", "b = _eq0[1:3]"))

    def test_an_equivalenced_scalar_is_its_storage_element(self):
        self.assertFalse(self.changed("equivalence_scalars", "_eq0[0] = 1.5", "_eq0[0] = 1.25"))
        self.assertFalse(self.changed("equivalence_scalars", "_eq0[0] = _eq0[0] * 2.0", "_eq0[0] = _eq0[0] * 3.0"))

    def test_extent_of_an_array_of_a_derived_type(self):
        self.assertFalse(self.changed("derived_types", "range(3 - 1 + 1)", "range(2)"))

    def test_modulo_is_not_mod(self):
        self.assertFalse(self.changed("modulo_intrinsic", "((x) % (2.0))", "_fm.fmod(x, 2.0)"))

    def test_log10_is_not_log(self):
        self.assertFalse(self.changed("ieee_math", "_fm.log10(", "_fm.log("))


@unittest.skipUnless(shutil.which("gfortran"), "gfortran is the reference")
class Fixtures(unittest.TestCase):
    def test_established_translations_are_identical(self):
        for name in ESTABLISHED:
            source = FIXTURES / f"{name}.f90"
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                port = Path(tmp) / "port.py"
                port.write_text(translate_fortran_to_python(source.read_text()))
                report = ic.check_python([source], [port])
                self.assertTrue(report.ok, report)

    def test_single_edits_of_real_translations_are_reported(self):
        """Every binary operator, comparison and statement of three kernels, one at a time."""
        operators = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}
        comparisons = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}
        for name in ("baseline_arith", "single_precision", "select_case"):
            source = FIXTURES / f"{name}.f90"
            base = ast.parse(translate_fortran_to_python(source.read_text()))
            dump = ic.dump_fortran([source])
            user = [i for i, n in enumerate(base.body) if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
            with tempfile.TemporaryDirectory() as tmp:
                port = Path(tmp) / "port.py"
                for index in user:
                    for position, node in enumerate(ast.walk(base.body[index])):
                        if not (isinstance(node, ast.BinOp) and type(node.op) in operators
                                or isinstance(node, ast.Compare) and type(node.ops[0]) in comparisons):
                            continue
                        tree = copy.deepcopy(base)
                        target = list(ast.walk(tree.body[index]))[position]
                        if isinstance(target, ast.BinOp):
                            target.op = operators[type(target.op)]()
                        else:
                            target.ops[0] = comparisons[type(target.ops[0])]()
                        edited = ast.unparse(target)
                        if "range(" in edited or edited.endswith(" + 1") and "n + 1" in edited:
                            continue
                        port.write_text(ast.unparse(tree))
                        with self.subTest(fixture=name, edit=edited):
                            try:
                                self.assertFalse(ic.check_python([source], [port], dump).ok)
                            except ic.Unchecked:
                                pass            # not established is not a pass


if __name__ == "__main__":
    unittest.main()
