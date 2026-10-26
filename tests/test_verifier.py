"""Tests for scripts/translation_verifier.py: a wrong translation must make the CLI exit non-zero.

Run (from the plugin root):  python3 -B -m unittest discover -s tests -v

Exit codes under test: 0 = compared and equal within tolerance, 1 = compared and different, 2 = nothing was
compared (missing arguments, unreadable or non-numeric files). The tolerance (1e-12) is the plugin's documented
default; no test loosens it to make something pass.
"""

import hashlib
import json
import os
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
        env = dict(os.environ, VERIFICATION_EVIDENCE_DIR=str(self.dir / ".verification"))   # never write into the repo
        return subprocess.run([sys.executable, str(VERIFIER), *args], capture_output=True, text=True, env=env)

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
        self.assertEqual(self.compare(REF, test, "--no-evidence").returncode, 1)
        self.assertEqual(self.compare(REF, test, "--tol", "1e-5", "--no-evidence").returncode, 0)

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


class EvidenceCli(unittest.TestCase):
    """Every comparison is appended to the evidence log; a tolerance loosened after a failure on unchanged files is withheld."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.write("ref.txt", REF)
        self.write("test.txt", [v + 1e-9 for v in REF])                    # fails at 1e-12, passes at 1e-6

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, values):
        (self.dir / name).write_text("\n".join("%.17e" % v for v in values) + "\n")

    def cli(self, *extra):
        env = dict(os.environ, VERIFICATION_EVIDENCE_DIR=str(self.dir / ".verification"))
        return subprocess.run([sys.executable, str(VERIFIER), "--ref", str(self.dir / "ref.txt"),
                               "--test", str(self.dir / "test.txt"), *extra], capture_output=True, text=True, env=env)

    def records(self):
        return [json.loads(line) for line in (self.dir / ".verification" / "log.jsonl").read_text().splitlines()]

    def test_a_run_is_recorded_with_the_hashes_of_both_files(self):
        self.assertEqual(self.cli("--tol", "1e-6").returncode, 0)
        (record,) = self.records()
        self.assertEqual((record["tool"], record["plugin"], record["kind"]), ("translation_verifier", "fortranbridge", "compare"))
        self.assertEqual((record["passed"], record["exit_status"], record["tolerance"]), (True, 0, {"tol": 1e-6}))
        expected = {name: hashlib.sha256((self.dir / name).read_bytes()).hexdigest() for name in ("ref.txt", "test.txt")}
        self.assertEqual({s["path"]: s["sha256"] for s in record["subjects"]}, expected)

    def test_a_failure_is_recorded_as_one(self):
        self.assertEqual(self.cli().returncode, 1)
        self.assertEqual((self.records()[0]["passed"], self.records()[0]["exit_status"]), (False, 1))

    def test_loosening_after_a_failure_on_unchanged_files_is_withheld(self):
        self.assertEqual(self.cli().returncode, 1)
        done = self.cli("--tol", "1e-6")
        self.assertEqual(done.returncode, 3, done.stdout)
        self.assertIn("verdict withheld", done.stderr)
        self.assertIn("Verdict withheld", done.stdout)
        self.assertIn("withheld", self.records()[-1]["flags"])

    def test_rerunning_a_withheld_command_stays_withheld(self):
        """Regression: the withheld run was logged with passed=true, so repeating the command used to return 0."""
        self.assertEqual(self.cli().returncode, 1)
        for _ in range(3):
            self.assertEqual(self.cli("--tol", "1e-6").returncode, 3)

    def test_a_throwaway_extra_subject_cannot_release_it(self):
        """Regression: a changing file named with --subject used to make the files look changed."""
        self.assertEqual(self.cli().returncode, 1)
        throwaway = Path(tempfile.mkdtemp()) / "noise.txt"
        throwaway.write_text(str(os.getpid()))
        self.assertEqual(self.cli("--tol", "1e-6", "--subject", str(throwaway)).returncode, 3)

    def test_a_justification_releases_it_and_is_recorded(self):
        self.cli()
        self.assertEqual(self.cli("--tol", "1e-6", "--justify", "Fortran WRITE prints 9 digits").returncode, 0)
        self.assertEqual(self.records()[-1]["justification"], "Fortran WRITE prints 9 digits")

    def test_regenerated_output_is_a_new_run_not_a_workaround(self):
        self.cli()
        self.write("test.txt", [v + 2e-9 for v in REF])                    # the translation changed
        done = self.cli("--tol", "1e-6")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("subjects_changed", self.records()[-1]["flags"])

    def test_a_different_test_file_is_a_different_comparison(self):
        self.cli()                                                            # ref vs test.txt fails at 1e-12
        self.write("other.txt", [v + 1e-9 for v in REF])
        env = dict(os.environ, VERIFICATION_EVIDENCE_DIR=str(self.dir / ".verification"))
        done = subprocess.run([sys.executable, str(VERIFIER), "--ref", str(self.dir / "ref.txt"),
                               "--test", str(self.dir / "other.txt"), "--tol", "1e-6"], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertNotIn("tolerance_loosened_after_failure", self.records()[-1]["flags"])

    def test_no_evidence_and_unwritable_evidence(self):
        self.assertEqual(self.cli("--tol", "1e-6", "--no-evidence").returncode, 0)
        self.assertFalse((self.dir / ".verification").exists())
        (self.dir / ".verification").write_text("a file where the directory should be\n")
        passed = self.cli("--tol", "1e-6")
        self.assertEqual(passed.returncode, 2)                                # a pass without evidence is not a pass
        self.assertIn("evidence NOT recorded", passed.stderr)
        self.assertEqual(self.cli().returncode, 1)                            # a failure stays a failure


if __name__ == "__main__":
    unittest.main()
