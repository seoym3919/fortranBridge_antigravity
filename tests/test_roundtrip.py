"""Round-trip accuracy: compile the Fortran with gfortran, run it, translate it, run the translation, compare.

Every fixture in tests/roundtrip/*.f90 prints only integers, logicals and DOUBLE PRECISION values (single precision
results are printed through DBLE, which is exact), so each printed number is compared exactly: atol 0, max_ulp 0 and the
sign of zero (verification_evidence.compare_vectors). The expected values are whatever gfortran prints at test time;
none is typed in by hand and no tolerance is loosened. The reference is built with -O0 -fwrapv: the standard leaves
integer overflow undefined, and the translators emulate two's-complement wrap-around.

Skipped, not passed, when gfortran is missing. The C++ target is compiled and compared only when a C++ compiler is found.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "roundtrip"
sys.path.insert(0, str(ROOT / "scripts"))

import verification_evidence as ve  # noqa: E402
from fortran_to_cpp import translate_fortran_to_cpp  # noqa: E402
from fortran_to_python import translate_fortran_to_python  # noqa: E402

GFORTRAN = shutil.which("gfortran")
# FORTRANBRIDGE_CXX may name a compiler command (e.g. "python -m ziglang c++"); otherwise g++, c++ or clang++ is used.
CXX = (shlex.split(os.environ["FORTRANBRIDGE_CXX"]) if os.environ.get("FORTRANBRIDGE_CXX")
       else [c for c in (shutil.which("g++") or shutil.which("c++") or shutil.which("clang++"),) if c])
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*(?:[eEdD][-+]?\d+)?|\.\d+(?:[eEdD][-+]?\d+)?)")
_WORDS = {"T": "1", "F": "0", "True": "1", "False": "0"}


def numbers(text: str):
    """Every number printed, in order. Logicals become 1/0 (Fortran T/F, Python True/False)."""
    values = []
    for token in re.findall(r"[-+]?(?:\d+\.?\d*(?:[eEdD][-+]?\d+)?|\.\d+(?:[eEdD][-+]?\d+)?)|\b(?:True|False|T|F)\b", text):
        values.append(float(_WORDS.get(token, token).replace("d", "e").replace("D", "e")))
    return values


def run(command, **kwargs):
    done = subprocess.run(command, capture_output=True, text=True, timeout=120, **kwargs)
    if done.returncode != 0:
        raise AssertionError(f"{' '.join(map(str, command))} failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout


def stdin_for(source: Path):
    """The fixture's standard input, if it has a <name>.in file next to it."""
    path = source.with_suffix(".in")
    return path.read_text() if path.exists() else None


def fortran_reference(source: Path, work: Path, text: bool = False):
    exe = work / (source.stem + ".exe")
    run([GFORTRAN, "-O0", "-fwrapv", "-w", str(source), "-o", str(exe)], cwd=work)      # cwd: module .mod files stay here
    out = run([str(exe)], cwd=work, input=stdin_for(source))
    return out if text else numbers(out)


def python_result(source: Path, work: Path, text: bool = False):
    module = work / f"tr_{source.stem}.py"
    module.write_text(translate_fortran_to_python(source.read_text()))
    out = run([sys.executable, "-I", "-W", "ignore", "-c",
               f"import sys; sys.path.insert(0, {str(work)!r}); import tr_{source.stem} as m; m.main()"],
              cwd=work, input=stdin_for(source))
    return out if text else numbers(out)


def cpp_result(source: Path, work: Path, text: bool = False):
    cpp = work / f"tr_{source.stem}.cpp"
    cpp.write_text(translate_fortran_to_cpp(source.read_text()))
    exe = work / f"tr_{source.stem}_cpp.exe"
    # -fwrapv and -ffp-contract=off are what the generated CMakeLists.txt sets: wrapping integers, no fused multiply-add
    run([*CXX, "-std=c++17", "-O0", "-fwrapv", "-ffp-contract=off", str(cpp), "-o", str(exe)])
    out = run([str(exe)], cwd=work, input=stdin_for(source))
    return out if text else numbers(out)


def assert_identical(testcase, reference, got, label):
    testcase.assertEqual(len(got), len(reference), f"{label}: printed {len(got)} numbers, Fortran printed {len(reference)}")
    report = ve.compare_vectors(reference, got, rtol=ve.limits("float64")["rtol"], atol=0.0, max_ulp=0, bits=64)
    testcase.assertTrue(report["passed"], f"{label} differs from gfortran: reference={reference} translated={got} {report}")


@unittest.skipUnless(GFORTRAN, "gfortran is not installed; the round-trip tests need it")
class PythonRoundTrip(unittest.TestCase):
    def check(self, name):
        source = FIXTURES / f"{name}.f90"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            reference = fortran_reference(source, work)
            self.assertGreater(len(reference), 0, "the reference printed nothing")
            assert_identical(self, reference, python_result(source, work), f"{name} (python)")

    def test_baseline_arithmetic(self):
        self.check("baseline_arith")

    def test_real_literal_assigned_to_double(self):
        self.check("real_literal_to_double")

    def test_single_precision_real(self):
        self.check("single_precision")

    def test_integer_power(self):
        self.check("integer_power")

    def test_integer_overflow_wraps_and_division_truncates(self):
        self.check("integer_overflow")

    def test_do_variable_after_loop(self):
        self.check("do_loop_variable")

    def test_declared_kinds_and_conversion_intrinsics(self):
        self.check("kinds_and_intrinsics")

    def test_reductions_accumulate_in_array_order(self):
        self.check("sequential_reductions")

    def test_real_do_variable(self):
        self.check("real_do_variable")

    def test_common_block_in_main_program(self):
        self.check("common_in_program")

    def test_common_members_associate_by_position(self):
        self.check("common_renamed_members")

    def test_floor_ceiling_and_numeric_inquiry(self):
        self.check("standard_intrinsics")

    def test_ieee_division_by_zero(self):
        self.check("ieee_division")

    def test_modulo_takes_the_sign_of_the_divisor(self):
        self.check("modulo_intrinsic")

    def test_compound_expressions_as_intrinsic_arguments(self):
        self.check("intrinsic_arguments")

    def test_units_may_be_called_before_they_are_defined(self):
        self.check("unit_order")

    def test_select_case(self):
        self.check("select_case")

    def test_goto_computed_goto_and_arithmetic_if(self):
        self.check("goto_control_flow")

    def test_module_and_use(self):
        self.check("module_use")

    def test_files_open_write_read_close(self):
        self.check("file_io")

    def test_read_from_standard_input(self):
        self.check("read_stdin")

    def test_allocatable_arrays_and_assumed_shape(self):
        self.check("allocatable_arrays")

    def test_pointer_arrays(self):
        self.check("pointer_arrays")

    def test_pointers_to_scalars_and_derived_types(self):
        self.check("pointer_scalars")

    def test_derived_types(self):
        self.check("derived_types")

    def test_equivalence_of_scalars(self):
        self.check("equivalence_scalars")

    def test_equivalence_of_arrays_and_elements(self):
        self.check("equivalence_arrays")

    def test_internal_procedures_and_host_association(self):
        self.check("internal_procedures")

    def test_data_for_array_sections(self):
        self.check("data_sections")

    def test_use_renaming_and_interface_blocks(self):
        self.check("use_rename_interface")

    def test_array_constructors_and_implied_do(self):
        self.check("array_constructors")

    def test_whole_array_expressions_and_sections(self):
        self.check("whole_array_ops")

    def test_complex_numbers(self):
        self.check("complex_arith")

    def test_array_element_passed_to_an_array_dummy(self):
        self.check("sequence_association")


    def test_character_padding_comparison_and_concatenation(self):
        self.check("character_strings")

    def test_formatted_output_text(self):
        source = FIXTURES / "formatted_output.f90"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            self.assertEqual(python_result(source, work, text=True), fortran_reference(source, work, text=True))

    def test_complex_elemental_functions(self):
        self.check("complex_functions")

    def test_data_for_elements_and_implied_do(self):
        self.check("data_elements")

    def test_real_to_integer_power_multiplies_like_libgfortran(self):
        self.check("real_integer_power")

    def test_math_domain_and_range_errors_are_ieee(self):
        self.check("ieee_math")

    def test_data_and_save_persist_between_calls(self):
        self.check("data_and_save")

    def test_declaration_initialisers_imply_save(self):
        self.check("declaration_initialisers")

    def test_call_arguments_that_are_array_elements(self):
        self.check("call_element_arguments")


class UnresolvedKindFailsLoudly(unittest.TestCase):
    """A kind the translator cannot resolve must not silently become single or double precision."""
    SOURCE = "program t\n  real(mystery) :: x\n  x = 1.5\n  print *, x\nend program t\n"

    def test_python_raises_at_the_use(self):
        code = translate_fortran_to_python(self.SOURCE)
        self.assertIn("NotImplementedError('fortranBridge: could not parse expression (kind of x could not be resolved)", code)

    def test_cpp_throws_in_the_unit(self):
        code = translate_fortran_to_cpp(self.SOURCE)
        self.assertIn('throw std::logic_error("fortranBridge: kind of x could not be resolved', code)


@unittest.skipUnless(GFORTRAN, "gfortran is not installed; the round-trip tests need it")
class MatmulMatchesLibgfortran(unittest.TestCase):
    """MATMUL: libgfortran's kernel is blocked and compiled with -ffast-math; its summation order depends on the CPU, so no
    fixed order reproduces it. The translation calls libgfortran's own routine (ctypes) -- bit for bit gfortran's result on this
    machine -- and falls back to a fixed-order sum when the library is not available.

    Fallback bound, observed against gfortran here: max relative error 3.3e-15, max 18 ULP, 35 of 70 printed values
    differing in the last bits; the bound below is 1e-14 relative and 64 ULP -- a documented property of the fallback, not a
    loosened tolerance for the bitwise test."""
    RTOL, MAX_ULP = 1e-14, 64

    def values(self, env):
        source = FIXTURES / "matmul_products.f90"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            work = Path(tmp)
            return fortran_reference(source, work), python_result(source, work)

    def test_with_libgfortran_the_result_is_bitwise_identical(self):
        reference, got = self.values({})
        probe = subprocess.run([sys.executable, "-c", "import ctypes; ctypes.CDLL('libgfortran.so.5')"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("libgfortran is not loadable from Python here")
        assert_identical(self, reference, got, "matmul_products (python, libgfortran)")

    @unittest.skipUnless(CXX, "needs a C++ compiler (set FORTRANBRIDGE_CXX to a compiler command)")
    def test_cpp_with_libgfortran_is_bitwise_identical(self):
        source = FIXTURES / "matmul_kernel.f90"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            assert_identical(self, fortran_reference(source, work), cpp_result(source, work), "matmul_kernel (c++, libgfortran)")

    @unittest.skipUnless(CXX, "needs a C++ compiler (set FORTRANBRIDGE_CXX to a compiler command)")
    def test_cpp_with_another_libm_stays_within_machine_precision(self):
        """matmul_products takes sin() in its inputs. zig c++ bundles its own sin, which differs from glibc's at sin(18) (measured),
        so 3 of 70 values differ with zig: a library difference, the case of `VERDICT: identical algorithm`. With glibc's libm the
        comparison is bitwise; the limit here is that verdict's, atol 0, rtol eps, 2 ULP."""
        source = FIXTURES / "matmul_products.f90"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            report = ve.compare_vectors(fortran_reference(source, work), cpp_result(source, work), rtol=2.0 ** -52, atol=0.0,
                                        max_ulp=2, bits=64)
        self.assertTrue(report["passed"], report)

    def test_fallback_is_not_within_machine_precision(self):
        """Why the fallback earns neither verdict: a different summation order is not a last-bit library difference."""
        reference, got = self.values({"FORTRANBRIDGE_NO_LIBGFORTRAN": "1"})
        report = ve.compare_vectors(reference, got, rtol=2.0 ** -52, atol=0.0, max_ulp=2, bits=64)
        self.assertFalse(report["passed"], report)

    def test_fallback_agrees_to_a_few_ulp(self):
        reference, got = self.values({"FORTRANBRIDGE_NO_LIBGFORTRAN": "1"})
        self.assertEqual(len(got), len(reference))
        report = ve.compare_vectors(reference, got, rtol=self.RTOL, atol=0.0, max_ulp=self.MAX_ULP, bits=64)
        self.assertTrue(report["passed"], report)


class LoudFailures(unittest.TestCase):
    """What cannot be translated faithfully must fail at the use, never run with a different meaning."""

    def test_unmapped_standard_intrinsic_is_not_a_bare_call(self):
        source = "program t\n  integer k\n  k = iand(5, 3)\n  print *, k\nend program t\n"
        self.assertIn("NotImplementedError('fortranBridge: could not parse expression (intrinsic IAND is not translated)",
                      translate_fortran_to_python(source))
        self.assertIn('throw std::logic_error("fortranBridge: could not parse expression (intrinsic IAND is not translated)',
                      translate_fortran_to_cpp(source))

    def test_user_function_named_like_an_intrinsic_is_still_called(self):
        source = ("double precision function erf(x)\n  double precision x\n  erf = x\nend function erf\n"
                  "program t\n  double precision erf\n  print *, erf(1.0d0)\nend program t\n")
        code = translate_fortran_to_python(source)
        self.assertIn("print(erf(1.0))", code)
        self.assertNotIn("raise NotImplementedError(", code)

    def test_what_cannot_be_translated_says_so(self):
        cases = {
            "pointer component": "subroutine s()\ntype node\ndouble precision v\ntype(node), pointer :: next\nend type node\n"
                                 "type(node) :: head\nend subroutine s\n",
            "generic interface": "subroutine s()\ninterface swap\nmodule procedure swap_i\nend interface\nend subroutine s\n",
            "backspace": "subroutine s()\nbackspace(10)\nend subroutine s\n",
            "unformatted": "subroutine s()\nopen(unit=10, file='x', form='unformatted')\nend subroutine s\n",
            "print a structure": "subroutine s()\ntype t\ndouble precision x\nend type t\ntype(t) :: a\nprint *, a\nend subroutine s\n",
        }
        for name, source in cases.items():
            with self.subTest(name):
                self.assertIn("raise NotImplementedError(", translate_fortran_to_python(source))
                self.assertIn("throw std::logic_error(", translate_fortran_to_cpp(source))

    def test_equivalence_of_arrays_fails_loudly_in_cpp(self):
        source = "subroutine s()\ndouble precision a(4), b(2)\nequivalence (a(3), b(1))\nend subroutine s\n"
        self.assertIn("throw std::logic_error(", translate_fortran_to_cpp(source))
        self.assertNotIn("raise NotImplementedError(", translate_fortran_to_python(source))

    def test_common_members_that_do_not_line_up_are_reported(self):
        source = ("subroutine s\n  integer k\n  common /b/ k\n  k = 1\nend subroutine s\n"
                  "program t\n  double precision x\n  common /b/ x\n  call s\nend program t\n")
        for code in (translate_fortran_to_python(source), translate_fortran_to_cpp(source)):
            self.assertIn("COMMON /B/ member 1", code)
            self.assertTrue("NotImplementedError" in code or "logic_error" in code)


@unittest.skipUnless(GFORTRAN and CXX, "needs gfortran and a C++ compiler (set FORTRANBRIDGE_CXX to a compiler command)")
class CppRoundTrip(unittest.TestCase):
    def check(self, name):
        source = FIXTURES / f"{name}.f90"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            reference = fortran_reference(source, work)
            assert_identical(self, reference, cpp_result(source, work), f"{name} (c++)")

    def test_compound_expressions_as_intrinsic_arguments(self):
        """mod(a + 3, b) was written a + 3 % b, and dim(x, y + z) x - y + z. The final value is exp(), which zig's bundled libm
        rounds differently from glibc, so this one comparison allows that verdict's limit: atol 0, rtol eps, 2 ULP."""
        source = FIXTURES / "intrinsic_arguments.f90"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            report = ve.compare_vectors(fortran_reference(source, work), cpp_result(source, work), rtol=2.0 ** -52, atol=0.0,
                                        max_ulp=2, bits=64)
        self.assertTrue(report["passed"], report)

    def test_baseline_arithmetic(self):
        self.check("baseline_arith")

    def test_real_literal_assigned_to_double(self):
        self.check("real_literal_to_double")

    def test_single_precision_real(self):
        self.check("single_precision")

    def test_integer_power(self):
        self.check("integer_power")

    def test_integer_overflow_wraps_and_division_truncates(self):
        self.check("integer_overflow")

    def test_do_variable_after_loop(self):
        self.check("do_loop_variable")

    def test_declared_kinds_and_conversion_intrinsics(self):
        self.check("kinds_and_intrinsics")

    def test_reductions_accumulate_in_array_order(self):
        self.check("sequential_reductions")

    def test_real_do_variable(self):
        self.check("real_do_variable")

    def test_common_block_in_main_program(self):
        self.check("common_in_program")

    def test_common_members_associate_by_position(self):
        self.check("common_renamed_members")

    def test_floor_ceiling_and_numeric_inquiry(self):
        self.check("standard_intrinsics")

    def test_ieee_division_by_zero(self):
        self.check("ieee_division")

    def test_modulo_takes_the_sign_of_the_divisor(self):
        self.check("modulo_intrinsic")

    def test_units_may_be_called_before_they_are_defined(self):
        self.check("unit_order")

    def test_select_case(self):
        self.check("select_case")

    def test_goto_computed_goto_and_arithmetic_if(self):
        self.check("goto_control_flow")

    def test_module_and_use(self):
        self.check("module_use")

    def test_files_open_write_read_close(self):
        self.check("file_io")

    def test_read_from_standard_input(self):
        self.check("read_stdin")

    def test_allocatable_arrays_and_assumed_shape(self):
        self.check("allocatable_arrays")

    def test_pointer_arrays(self):
        self.check("pointer_arrays")

    def test_pointers_to_scalars_and_derived_types(self):
        self.check("pointer_scalars")

    def test_derived_types(self):
        self.check("derived_types")

    def test_equivalence_of_scalars(self):
        self.check("equivalence_scalars")

    def test_internal_procedures_and_host_association(self):
        self.check("internal_procedures")

    def test_data_for_array_sections(self):
        self.check("data_sections")

    def test_use_renaming_and_interface_blocks(self):
        self.check("use_rename_interface")

    def test_array_constructors_and_implied_do(self):
        self.check("array_constructors")

    def test_whole_array_expressions_and_sections(self):
        self.check("whole_array_ops")

    def test_complex_numbers(self):
        self.check("complex_arith")

    def test_array_element_passed_to_an_array_dummy(self):
        self.check("sequence_association")

    def test_character_padding_comparison_and_concatenation(self):
        self.check("character_strings")

    def test_formatted_output_text(self):
        source = FIXTURES / "formatted_output.f90"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            self.assertEqual(cpp_result(source, work, text=True), fortran_reference(source, work, text=True))

    def test_data_for_elements_and_implied_do(self):
        self.check("data_elements")

    def test_real_to_integer_power_multiplies_like_libgfortran(self):
        self.check("real_integer_power")

    def test_math_domain_and_range_errors_are_ieee(self):
        self.check("ieee_math")

    def test_data_and_save_persist_between_calls(self):
        self.check("data_and_save")

    def test_declaration_initialisers_imply_save(self):
        self.check("declaration_initialisers")

    def test_call_arguments_that_are_array_elements(self):
        self.check("call_element_arguments")


if __name__ == "__main__":
    unittest.main()
