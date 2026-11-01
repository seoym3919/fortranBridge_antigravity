"""Tests for scripts/identity_cpp.py: the C++ translation read through clang's AST, compared with gfortran's parse tree."""

import shlex
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import identity_checker as ic  # noqa: E402
import identity_cpp as cc  # noqa: E402
from fortran_to_cpp import translate_fortran_to_cpp  # noqa: E402

FIXTURES = ROOT / "tests" / "roundtrip"
HAS_CLANG = bool(os.environ.get("FORTRANBRIDGE_CXX") or shutil.which("clang++"))
# The translator refuses EQUIVALENCE of arrays in C++ with a throw, so this one stays unestablished, on purpose.
REFUSED = {"equivalence_arrays"}


def check(source: Path, code: str) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        port = Path(tmp) / "port.cpp"
        port.write_text(code)
        try:
            return cc.check_cpp([source], [port]).ok
        except ic.Unchecked:
            return False


@unittest.skipUnless(shutil.which("gfortran") and HAS_CLANG, "needs gfortran and clang (or FORTRANBRIDGE_CXX)")
class Fixtures(unittest.TestCase):
    def test_every_fixture(self):
        for source in sorted(FIXTURES.glob("*.f90")):
            with self.subTest(source.stem):
                self.assertEqual(check(source, translate_fortran_to_cpp(source.read_text())), source.stem not in REFUSED)

    def edited(self, fixture, old, new):
        source = FIXTURES / f"{fixture}.f90"
        code = translate_fortran_to_cpp(source.read_text())
        self.assertIn(old, code)
        return check(source, code.replace(old, new, 1))

    def test_changes_are_reported(self):
        edits = {
            "operator": ("baseline_arith", "y[i - 1] + a * x[i - 1]", "y[i - 1] - a * x[i - 1]"),
            "index": ("baseline_arith", "y[i - 1] + a", "y[i] + a"),
            "literal": ("baseline_arith", "3.0 * x * x", "3.5 * x * x"),
            "reassociation": ("baseline_arith", "3.0 * x * x", "3.0 * (x * x)"),
            "loop start": ("baseline_arith", "for (i = 1; i <= n; ++i)", "for (i = 2; i <= n; ++i)"),
            "scalar kind": ("single_precision", "float", "double"),
            "array extent": ("baseline_arith", "std::vector<double> x(n);", "std::vector<double> x(n + 1);"),
            "parameter": ("baseline_arith", "constexpr int n = 5;", "constexpr int n = 6;"),
            "bound read elsewhere": ("do_loop_variable", "const auto do1_end = m;", "const auto do1_end = m + 1;"),
            "initial value": ("data_and_save", "static int k = 0;", "static int k = 1;"),
            "not static": ("data_and_save", "static int k = 0;", "int k = 0;"),
            "goto target": ("goto_control_flow", "goto label_50;", "goto label_40;"),
            "alias": ("equivalence_scalars", "int& n = m;", "int n = m;"),
            "array statement bound": ("whole_array_ops", "k1_0 < n - 1 + 1", "k1_0 < n - 2 + 1"),
            "print order": ("matmul_kernel", "std::cout << c[1 + k14_0 - 1][1 + k14_1 - 1]", "std::cout << c[1 + k14_1 - 1][1 + k14_0 - 1]"),
        }
        for what, (fixture, old, new) in edits.items():
            with self.subTest(what):
                self.assertFalse(self.edited(fixture, old, new), what)

    def test_the_precedence_bugs_it_found_are_reported(self):
        """mod(a + 3, b) was emitted as a + 3 % b, and dim(x, y + z) as x - y + z. Output comparison passed by coincidence."""
        self.assertFalse(self.edited("intrinsic_arguments", "((a + 3) % (b))", "(a + 3 % b)"))
        self.assertFalse(self.edited("integer_overflow", "((seed * 1103515245 + 12345) % (2147483647))", "(seed * 1103515245 + 12345 % 2147483647)"))

    def test_a_user_routine_called_fill_is_not_std_fill(self):
        self.assertTrue(check(FIXTURES / "module_use.f90", translate_fortran_to_cpp((FIXTURES / "module_use.f90").read_text())))


class Wrapping(unittest.TestCase):
    def test_the_namespace_goes_after_the_includes(self):
        text = cc.wrapped("#include <vector>\n#include <cmath>\nint main() { return 0; }")
        self.assertLess(text.index("#include <cmath>"), text.index(cc.NAMESPACE))
        self.assertLess(text.index(cc.NAMESPACE), text.index("int main"))

    def test_a_file_with_no_include_is_not_read(self):
        with self.assertRaises(ic.Unchecked):
            cc.wrapped("int main() { return 0; }")


if __name__ == "__main__":
    unittest.main()
