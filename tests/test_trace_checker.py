"""Tests for scripts/trace_checker.py: every assignment compared bit for bit, every statement executed.

The reference is gfortran's own run of the source. A test says that a program matches, or that a named change to it is reported
at a named statement. No numeric tolerance is used, except where a test exercises --machine-precision.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import trace_checker as tc  # noqa: E402
from fortran_to_python import translate_fortran_to_python  # noqa: E402

FIXTURES = ROOT / "tests" / "roundtrip"
# Statements of the fixtures that no run reaches: a branch left there to test its translation, not its execution.
UNREACHED = {"goto_control_flow": ["goto_control_flow.f90:52", "goto_control_flow.f90:54"],
             "select_case": ["select_case.f90:31", "select_case.f90:50"],
             "do_loop_variable": ["do_loop_variable.f90:12"],           # the body of a zero-trip loop
             "real_do_variable": ["real_do_variable.f90:20"]}

KERNEL = """\
program kernel
  implicit none
  double precision x, s, r
  real t
  integer i, n
  x = 1.1d0
  s = 0.0d0
  n = 4
  t = 0.1
  do i = 1, n
    r = x**7
    s = s + r / i
    t = t * 1.5
  end do
  if (s > 1.0d0) s = s - 1.0d0
  print *, s, t
end program kernel
"""


@unittest.skipUnless(shutil.which("gfortran") and shutil.which("gcov"), "gfortran and gcov are the reference")
class Fixtures(unittest.TestCase):
    def test_every_fixture_runs_the_same_assignments(self):
        for source in sorted(FIXTURES.glob("*.f90")):
            with self.subTest(source.stem), tempfile.TemporaryDirectory() as tmp:
                port = Path(tmp) / "port.py"
                port.write_text(translate_fortran_to_python(source.read_text()))
                stdin = source.with_suffix(".in")
                result = tc.check([source], port, [stdin.read_bytes() if stdin.exists() else None], False,
                                  UNREACHED.get(source.stem, ()))
                self.assertIsNone(result.divergence)
                self.assertEqual(result.unexecuted, [])


@unittest.skipUnless(shutil.which("gfortran") and shutil.which("gcov"), "gfortran and gcov are the reference")
class Kernel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "kernel.f90"
        self.source.write_text(KERNEL)
        self.port = self.root / "port.py"
        self.code = translate_fortran_to_python(KERNEL)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, code=None, machine=False, source=None):
        self.port.write_text(code if code is not None else self.code)
        return tc.check([source or self.source], self.port, [None], machine)

    def test_the_translation_matches(self):
        result = self.check()
        self.assertTrue(result.ok, result)
        self.assertGreater(result.values, 8)

    def test_a_helper_that_computes_something_else_is_named(self):
        """The text of the statement still reads r = _fpowi(x, 7), so the static check has nothing to say here."""
        helper = "def _fpowi(x, n):\n"
        self.assertIn(helper, self.code)
        result = self.check(self.code.replace(helper, helper + "    return float(np.float32(x)) ** n\n", 1))
        self.assertIn("r at kernel.f90:11", result.divergence)

    def test_a_changed_single_precision_step_is_named(self):
        result = self.check(self.code.replace("np.float32(1.5)", "np.float32(1.25)", 1))
        self.assertIn("t at kernel.f90:13", result.divergence)

    def test_an_extra_assignment_is_a_difference(self):
        marker = "    n = 4\n"
        self.assertIn(marker, self.code)
        self.assertIsNotNone(self.check(self.code.replace(marker, marker + "    n = 4\n", 1)).divergence)

    def test_a_kind_change_is_a_difference(self):
        result = self.check(self.code.replace("t = np.float32(0.1)", "t = 0.1", 1))
        self.assertIn("t at kernel.f90:9", result.divergence)

    def test_a_statement_never_run_is_reported(self):
        unreached = KERNEL.replace("  print *, s, t\n", "  if (n > 100) s = 0.0d0\n  print *, s, t\n")
        self.source.write_text(unreached)
        self.code = translate_fortran_to_python(unreached)
        result = self.check()
        self.assertIsNone(result.divergence)
        self.assertFalse(result.ok)
        self.assertEqual(result.unexecuted, ["kernel.f90:16"])

    def test_machine_precision_accepts_a_last_bit_and_no_more(self):
        one_bit = self.code.replace("x = 1.1\n", "x = 1.1000000000000003\n", 1)      # x itself, 1 ULP away
        self.assertIn("1.1000000000000003", one_bit)
        self.assertIsNotNone(self.check(one_bit).divergence)
        far = self.code.replace("x = 1.1\n", "x = 1.1001\n", 1)
        self.assertIsNotNone(self.check(far, machine=True).divergence)


@unittest.skipUnless(shutil.which("gfortran") and shutil.which("gcov"), "gfortran and gcov are the reference")
class Instrumentation(unittest.TestCase):
    def probes(self, text, suffix=".f90"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"s{suffix}"
            path.write_text(text)
            probes = []
            out = tc.instrument(path, path.name, probes, {})
            return out, probes

    def test_fixed_form_label_on_the_end_of_a_do(self):
        out, probes = self.probes("      program p\n      integer i\n      real a(3)\n      do 10 i = 1, 3\n   10 a(i) = 1.0\n      end\n", ".f")
        self.assertEqual([p.target for p in probes], ["a(i)"])
        self.assertIn("do 90010", out)

    def test_one_line_if_keeps_its_condition_once(self):
        out, probes = self.probes("program p\n  integer n\n  n = 1\n  if (n > 0) n = n - 1\nend program p\n")
        self.assertEqual(len(probes), 2)
        self.assertIn("if (n > 0) then", out)

    def test_declarations_are_not_assignments(self):
        _, probes = self.probes("program p\n  integer, parameter :: n = 3\n  integer :: m = 2\n  print *, n, m\nend program p\n")
        self.assertEqual(probes, [])


if __name__ == "__main__":
    unittest.main()
