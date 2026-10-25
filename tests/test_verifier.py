"""Tests for scripts/translation_verifier.py: a wrong translation must make the CLI exit non-zero.

Run (from the plugin root):  python3 -B -m unittest discover -s tests -v

Exit codes under test: 0 = compared and equal within tolerance, 1 = compared and different, 2 = nothing was
compared (missing arguments, unreadable or non-numeric files). The tolerance (1e-12) is the plugin's documented
default; no test loosens it to make something pass.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERIFIER = ROOT / "scripts" / "translation_verifier.py"
sys.path.insert(0, str(ROOT / "scripts"))

import translation_verifier as tv  # noqa: E402

REF = [1.0 / (k + 1) for k in range(200)]          # fixed, deterministic values


class VerifierCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text)
        return str(path)

    def vec(self, name, values, fmt="%.17e"):
        return self.write(name, "\n".join(fmt % v for v in values) + "\n")

    def cli(self, *args):
        return subprocess.run([sys.executable, str(VERIFIER), *args], capture_output=True, text=True)

    def compare(self, ref_values, test_values, *extra):
        return self.cli("--ref", self.vec("ref.txt", ref_values), "--test", self.vec("test.txt", test_values), *extra)

    def test_identical_passes(self):
        self.assertEqual(self.compare(REF, REF).returncode, 0)

    def test_rounding_noise_passes(self):
        self.assertEqual(self.compare(REF, [v * (1 + 1e-14) for v in REF]).returncode, 0)

    def test_wrong_translation_fails(self):
        done = self.compare(REF, [v + 1e-6 for v in REF])
        self.assertEqual(done.returncode, 1)
        self.assertIn("FAILED", done.stdout)

    def test_length_mismatch_fails_with_reason(self):
        done = self.compare(REF, REF[:-1])
        self.assertEqual(done.returncode, 1)
        self.assertIn("Length mismatch", done.stdout)

    def test_nan_and_inf_fail_with_reason(self):
        for bad in ("nan", "inf", "-inf"):
            test = [repr(v) for v in REF]
            test[5] = bad
            done = self.cli("--ref", self.vec("r.txt", REF), "--test", self.write("t.txt", "\n".join(test)))
            self.assertEqual(done.returncode, 1, bad)
            self.assertIn("Non-finite", done.stdout)

    def test_empty_files_fail(self):
        done = self.cli("--ref", self.write("a.txt", "\n"), "--test", self.write("b.txt", "# nothing\n"))
        self.assertEqual(done.returncode, 1)
        self.assertIn("nothing was compared", done.stdout)
        self.assertNotIn("Traceback", done.stderr)

    def test_one_local_error_in_a_long_vector_fails(self):
        """Rel L2 = 5e-13 would pass; Rel Linf = 5e-10 must make it fail."""
        ref, test = [1.0] * 1_000_000, [1.0] * 1_000_000
        test[3] += 5e-10
        result = tv.compare_outputs(ref, test)
        self.assertLessEqual(result["rel_l2"], 1e-12)
        self.assertFalse(result["passed"])

    def test_fortran_d_exponents_and_formats(self):
        ref = self.write("ref.txt", "! output of the Fortran run\n1.0D+00 2.5d-01,  3.0E+00\n\n-4.0D-3\n")
        test = self.write("test.txt", "1.0 0.25 3.0 -0.004\n")
        self.assertEqual(self.cli("--ref", ref, "--test", test).returncode, 0)
        wrong = self.write("wrong.txt", "1.0 0.25 3.0 -0.005\n")
        self.assertEqual(self.cli("--ref", ref, "--test", wrong).returncode, 1)

    def test_tolerance_option_is_honoured_both_ways(self):
        test = [v + 1e-9 for v in REF]
        self.assertEqual(self.compare(REF, test).returncode, 1)
        self.assertEqual(self.compare(REF, test, "--tol", "1e-5").returncode, 0)

    def test_non_numeric_file_exits_two(self):
        done = self.cli("--ref", self.write("a.txt", "1.0 two 3.0\n"), "--test", self.vec("b.txt", [1.0, 2.0, 3.0]))
        self.assertEqual(done.returncode, 2)
        self.assertIn("not a number", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_usage_errors_exit_two(self):
        for args in ([], ["--ref", "x.txt"], ["--test", "x.txt"], ["--ref", "a", "--test", "b", "--tol", "0"],
                     ["--ref", str(self.dir / "missing.txt"), "--test", str(self.dir / "missing2.txt")]):
            done = self.cli(*args)
            self.assertEqual(done.returncode, 2, args)
            self.assertNotIn("Traceback", done.stderr)

    def test_report_written(self):
        report = self.dir / "report.md"
        self.compare(REF, [v + 1e-6 for v in REF], "--report", str(report))
        text = report.read_text()
        self.assertIn("FAILED", text)
        self.assertIn("Relative L2", text)

    def test_npy_input(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        np.save(self.dir / "r.npy", np.array(REF))
        np.save(self.dir / "t.npy", np.array(REF) + 1e-6)
        self.assertEqual(self.cli("--ref", str(self.dir / "r.npy"), "--test", str(self.dir / "t.npy")).returncode, 1)
        np.save(self.dir / "t.npy", np.array(REF))
        self.assertEqual(self.cli("--ref", str(self.dir / "r.npy"), "--test", str(self.dir / "t.npy")).returncode, 0)

    def test_self_test_runs_and_says_it_is_not_a_translation_check(self):
        done = self.cli("--self-test")
        self.assertEqual(done.returncode, 0)
        self.assertIn("does NOT verify your translation", done.stdout)


if __name__ == "__main__":
    unittest.main()
