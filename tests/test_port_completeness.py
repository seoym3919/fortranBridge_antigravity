"""The complete-port rule for Fortran: every equation is translated, every procedure has a bitwise
unit test, and the regression record is bitwise identical (atol 0 and max_ulp 0).

Reference behaviour is the checker itself. A default elementwise budget is not a pass.
Nothing here loosens a tolerance to obtain a pass.
"""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import port_completeness as pc  # noqa: E402
import verification_evidence as ve  # noqa: E402

ORIGINAL = """
subroutine kick(x, v, dt)
  double precision x, v, dt
  if (x .gt. 0.0d0) v = v + dt * x
end subroutine kick
program orbit
  double precision x, v
  x = 1.0d0
  v = 0.0d0
  call kick(x, v, 0.5d0)
  print *, v
end program orbit
"""

TRANSLATED = """
def kick(x, v, dt):
    if x > 0.0:
        v = v + dt * x
    return v

def main():
    x = 1.0
    v = 0.0
    return kick(x, v, 0.5)
"""

UNIT = """
def test_kick():
    got = kick(1.0, 0.0, 0.5)
    assert ulp_distance(got, 0.5) == 0 and not zero_signbit_differs(got, 0.5)
"""


def _write(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


def _bitwise(call: str, expected: str) -> str:
    return (
        f"    got = {call}\n"
        f"    assert ulp_distance(got, {expected}) == 0 and not zero_signbit_differs(got, {expected})\n"
    )


def _log(root: Path, originals, translated, extras, **kwargs):
    dtype = kwargs.get("dtype", "float64")
    if kwargs.get("budget") == "default":
        limits = ve.limits(dtype, kwargs.get("rtol"), kwargs.get("atol"))
    else:
        limits = ve.limits(dtype, kwargs.get("rtol"), kwargs.get("atol"), kwargs.get("max_ulp", 0))
    subjects = [ve.subject(str(path), root) for path in list(originals) + list(translated) + list(extras)]
    ve.append(root / ".verification" / "log.jsonl", {
        "tool": kwargs.get("tool", pc.TOOL), "plugin": "fortranbridge", "kind": "compare",
        "name": "fortran_vs_translation", "key": "compare:port", "passed": kwargs.get("passed", True),
        "exit_status": kwargs.get("exit_status", 0),
        "tolerance": {"rtol": limits["rtol"], "atol": limits["atol"], "max_ulp": limits["max_ulp"]},
        "subjects": subjects, "flags": list(kwargs.get("flags") or []), "justification": kwargs.get("justification"),
        "detail": {"checker_version": kwargs.get("version", "2"), "dtype": dtype},
    })
    return root / ".verification" / "log.jsonl"


class PortCompleteness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.orig = self.root / "orig.f90"
        self.port = self.root / "port.py"
        self.unit = self.root / "test_port.py"
        self.ref = self.root / "ref.txt"
        self.out = self.root / "out.txt"
        self.inputs = self.root / "inputs.txt"
        for path, text in ((self.ref, "0.5\n"), (self.out, "0.5\n"), (self.inputs, "1\n")):
            _write(path, text)

    def tearDown(self):
        self.tmp.cleanup()

    def prime(self, original=ORIGINAL, translated=TRANSLATED, tests=UNIT, **log_kwargs):
        _write(self.orig, original)
        _write(self.port, translated)
        _write(self.unit, tests)
        extras = log_kwargs.pop("extras", [self.ref, self.out, self.inputs])
        return _log(self.root, [self.orig], [self.port], extras, **log_kwargs)

    def check(self, log, **kwargs):
        return pc.check_port(
            originals=kwargs.get("originals", [str(self.orig)]),
            translated=kwargs.get("translated", [str(self.port)]),
            unit_tests=kwargs.get("unit_tests", [str(self.unit)]),
            regression_tests=kwargs.get("regression_tests", []),
            evidence_log=str(log),
            renames=kwargs.get("renames"),
            target=kwargs.get("target", "python"),
        )

    def test_self_test_rejects_its_negative_controls(self):
        self.assertTrue(pc.run_self_test())

    def test_directives_state_the_rule(self):
        text = (ROOT / "rules" / "directives.md").read_text(encoding="utf-8")
        self.assertIn("port_completeness.py", text)
        self.assertIn("mathematically identical", text.lower())
        self.assertIn("unit test", text.lower())
        self.assertIn("regression", text.lower())
        self.assertIn("--target", text)
        self.assertIn("bitwise", text.lower())
        self.assertIn("investigate", text.lower())
        self.assertIn("max_ulp", text)

    def test_a_complete_port_lists_the_formula_and_not_the_literal(self):
        report = self.check(self.prime())
        self.assertEqual(report.failures, [], pc.format_report(report))
        self.assertTrue(any("v + dt * x" in eq.text for eq in report.equations))
        self.assertFalse(any(eq.text.replace(" ", "").lower().startswith("x=1.0d0") for eq in report.equations))
        self.assertTrue(any(fn.name == "orbit" and fn.target_name == "main" and fn.entry for fn in report.functions))

    def test_missing_definition_unit_test_and_untranslated_marker_fail(self):
        log = self.prime(translated="def orbit():\n    return 0.5\n")
        missing = self.check(log)
        self.assertTrue(any("not defined" in item and "kick" in item for item in missing.failures))
        self.assertTrue(any("not defined" in item and "orbit" in item for item in missing.failures))

        log = self.prime(tests="def test_kick():\n    # kick(1.0, 0.0, 0.5) == 0.5\n    assert True\n")
        commented = self.check(log)
        self.assertTrue(any("no unit test" in item for item in commented.failures))

        log = self.prime(translated=(
            "def kick(x, v, dt):\n"
            "    raise NotImplementedError('fortranBridge: untranslated statement')\n"
            "def main():\n"
            "    return kick(x, v, dt)\n"
        ))
        marked = self.check(log)
        self.assertTrue(any("not complete" in item and "kick" in item for item in marked.failures))

    def test_a_procedure_the_program_does_not_call_needs_a_bound_regression_test(self):
        original = """
            subroutine bias(x)
              double precision x
              x = x + 1.0d0
            end subroutine bias
            program orbit
              print *, 0
            end program orbit
        """
        translated = "def bias(x):\n    return x + 1.0\n\ndef main():\n    return 0\n"
        _write(self.orig, original)
        _write(self.port, translated)
        _write(self.unit, "def test_bias():\n" + _bitwise("bias(1.0)", "2.0"))
        log = _log(self.root, [self.orig], [self.port], [self.ref, self.out, self.inputs])
        uncovered = self.check(log)
        self.assertTrue(any("not executed by the regression" in item and "bias" in item for item in uncovered.failures))
        reg = self.root / "test_regress.py"
        _write(reg, "def test_bias():\n" + _bitwise("bias(1.0)", "2.0"))
        bound = _log(self.root, [self.orig], [self.port], [self.ref, self.out, self.inputs, reg])
        covered = self.check(bound, regression_tests=[str(reg)])
        self.assertEqual(covered.failures, [], pc.format_report(covered))

    def test_python_keyword_and_cpp_target_names(self):
        original = """
            function sum(a, b)
              double precision sum, a, b
              sum = a + b
            end function sum
            program orbit
              double precision x
              x = sum(1.0d0, 2.0d0)
              print *, x
            end program orbit
        """
        translated = "def sum_(a, b):\n    return a + b\n\ndef main():\n    return sum_(1.0, 2.0)\n"
        log = self.prime(original, translated, "def test_sum():\n" + _bitwise("sum_(1.0, 2.0)", "3.0"))
        report = self.check(log)
        self.assertEqual(report.failures, [], pc.format_report(report))
        self.assertTrue(any(fn.name == "sum" and fn.target_name == "sum_" for fn in report.functions))

        cpp = self.root / "port.cpp"
        cpp_unit = self.root / "test_sum_cpp.py"
        _write(cpp, "double sum(double a, double b) { return a + b; }\nint main() { return sum(1.0, 2.0) == 3.0 ? 0 : 1; }\n")
        _write(cpp_unit, "def test_sum():\n    assert abs(sum(1.0, 2.0) - 3.0) < 1e-12\n")
        cpp_log = _log(self.root, [self.orig], [cpp], [self.ref, self.out, self.inputs])
        tolerant = self.check(cpp_log, translated=[str(cpp)], unit_tests=[str(cpp_unit)], target="cpp")
        self.assertTrue(any("bitwise" in item and "sum" in item for item in tolerant.failures))
        _write(cpp_unit, "def test_sum():\n" + _bitwise("sum(1.0, 2.0)", "3.0"))
        cpp_report = self.check(cpp_log, translated=[str(cpp)], unit_tests=[str(cpp_unit)], target="cpp")
        self.assertEqual(cpp_report.failures, [], pc.format_report(cpp_report))

    def test_a_looser_record_does_not_pass(self):
        log = self.prime()
        self.assertTrue(self.check(log).ok)
        root = self.root / "budget"
        root.mkdir()
        for name in ("orig.f90", "port.py", "ref.txt", "out.txt", "inputs.txt"):
            (root / name).write_text((self.root / name).read_text(encoding="utf-8"), encoding="utf-8")
        _log(root, [root / "orig.f90"], [root / "port.py"],
             [root / "ref.txt", root / "out.txt", root / "inputs.txt"], budget="default")
        budget = pc.check_port(originals=[str(root / "orig.f90")], translated=[str(root / "port.py")],
                                unit_tests=[str(self.unit)], evidence_log=str(root / ".verification" / "log.jsonl"),
                                target="python")
        self.assertTrue(any("bitwise" in item and "investigate" in item.lower() for item in budget.failures))
        _log(self.root, [self.orig], [self.port], [self.ref, self.out, self.inputs], rtol=1e-6, max_ulp=1,
             justification="approved relaxation: not consulted")
        loose = self.check(log)
        self.assertTrue(any("rtol" in item and "investigate" in item.lower() for item in loose.failures))

    def test_cli_requires_a_target(self):
        script = ROOT / "scripts" / "port_completeness.py"
        good = subprocess.run([sys.executable, "-B", str(script), "--self-test"], capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        usage = subprocess.run([sys.executable, "-B", str(script), "--original", "a.f90", "--translated", "a.py"],
                               capture_output=True, text=True)
        self.assertEqual(usage.returncode, 2)


if __name__ == "__main__":
    unittest.main()
