"""Tests for scripts/verification_evidence.py, the evidence log shared by the scienceStack, cppBridge, fortranBridge and
theoryExtractor verifiers (this file is the same in each plugin, so every copy of the module is held to the same checks).

Run (from the plugin root):  python3 -B -m unittest discover -s tests -v

What is tested: the hash chain detects an edited, removed or reordered line (and the test documents that a truncated
tail is NOT detected); evidence goes stale when a bound file changes; a tolerance loosened after a failure on unchanged
files is withheld (exit 3) unless justified, and is not withheld when the files changed; failures to record evidence
are never silent.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verification_evidence as ve  # noqa: E402


def sub(path, sha):
    return {"path": path, "sha256": sha}


class LogChain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.log = self.dir / ".verification" / "log.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, n=1, **over):
        for i in range(n):
            record = dict(tool="t", plugin="p", kind="benchmark", name="kepler", key="k", passed=True, exit_status=0,
                          tolerance={"tol": 1e-12}, subjects=[], flags=[], justification=None, detail={"i": i})
            record.update(over)
            ve.append(self.log, record)

    def lines(self):
        return self.log.read_bytes().split(b"\n")[:-1]

    def test_clean_log_has_no_problems_and_chain_links_each_line(self):
        self.add(4)
        records, problems = ve.read_log(self.log)
        self.assertEqual((len(records), problems), (4, []))
        self.assertEqual(records[0]["prev"], ve.GENESIS)
        self.assertTrue(all(r["schema"] == ve.SCHEMA and r["timestamp"] for r in records))

    def test_editing_a_middle_line_is_detected(self):
        self.add(4)
        lines = self.lines()
        lines[1] = lines[1].replace(b'"passed": true', b'"passed": false')
        self.log.write_bytes(b"\n".join(lines) + b"\n")
        self.assertTrue(any("chain broken" in p for p in ve.read_log(self.log)[1]))

    def test_removing_a_middle_line_is_detected(self):
        self.add(4)
        lines = self.lines()
        del lines[1]
        self.log.write_bytes(b"\n".join(lines) + b"\n")
        self.assertTrue(any("chain broken" in p for p in ve.read_log(self.log)[1]))

    def test_reordering_is_detected(self):
        self.add(4)
        lines = self.lines()
        lines[1], lines[2] = lines[2], lines[1]
        self.log.write_bytes(b"\n".join(lines) + b"\n")
        self.assertTrue(ve.read_log(self.log)[1])

    def test_garbage_and_foreign_lines_are_problems(self):
        self.add(2)
        with open(self.log, "ab") as handle:
            handle.write(b"not json\n" + json.dumps({"schema": "something-else"}).encode() + b"\n")
        problems = ve.read_log(self.log)[1]
        self.assertTrue(any("not valid JSON" in p for p in problems))
        self.assertTrue(any("not a verification-evidence/1 record" in p for p in problems))

    def test_truncating_the_tail_is_not_detected_which_is_why_ci_is_the_real_gate(self):
        self.add(4)
        self.log.write_bytes(b"\n".join(self.lines()[:2]) + b"\n")
        self.assertEqual(ve.read_log(self.log)[1], [])

    def test_symlinked_log_is_refused_for_reading_and_writing(self):
        self.add(1)
        target = self.dir / "elsewhere.jsonl"
        target.write_bytes(self.log.read_bytes())
        self.log.unlink()
        self.log.symlink_to(target)
        self.assertTrue(any("symbolic link" in p for p in ve.read_log(self.log)[1]))
        with self.assertRaises(ve.EvidenceError):
            self.add(1)

    def test_parallel_appends_keep_the_chain_intact(self):
        threads = [threading.Thread(target=self.add, args=(5,)) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        records, problems = ve.read_log(self.log)
        self.assertEqual((len(records), problems), (30, []))

    def test_subject_paths_are_relative_when_below_the_base(self):
        (self.dir / "src").mkdir()
        (self.dir / "src" / "a.py").write_text("x = 1\n")
        s = ve.subject(str(self.dir / "src" / "a.py"), self.dir)
        self.assertEqual(s["path"], "src/a.py")
        self.assertEqual(s["sha256"], hashlib.sha256(b"x = 1\n").hexdigest())          # independent of ve.sha256_file
        big = self.dir / "big.bin"
        big.write_bytes(os.urandom(2_500_000))                                         # several 1 MiB blocks
        self.assertEqual(ve.sha256_file(big), hashlib.sha256(big.read_bytes()).hexdigest())
        self.assertIsNone(ve.subject(str(self.dir / "missing.py"), self.dir)["sha256"])


class Assessment(unittest.TestCase):
    A, B = sub("s.py", "a" * 64), sub("s.py", "b" * 64)

    def failed(self, tol, subjects, key="k"):
        return {"key": key, "passed": False, "tolerance": tol, "subjects": subjects, "timestamp": "t"}

    def test_no_history_or_a_passing_history_is_never_loosening(self):
        self.assertFalse(ve.assess_tolerance([], "k", {"tol": 1.0}, [self.A], None)["loosened"])
        passed = [dict(self.failed({"tol": 1e-9}, [self.A]), passed=True)]
        self.assertFalse(ve.assess_tolerance(passed, "k", {"tol": 1.0}, [self.A], None)["loosened"])

    def test_larger_tolerance_after_a_failure_on_unchanged_files_is_withheld_without_justification(self):
        history = [self.failed({"tol": 1e-9}, [self.A])]
        result = ve.assess_tolerance(history, "k", {"tol": 1e-3}, [self.A], None)
        self.assertEqual((result["loosened"], result["unchanged"], result["withheld"]), (True, True, True))
        self.assertTrue(ve.assess_tolerance(history, "k", {"tol": 1e-3}, [self.A], "  ")["withheld"])   # blank is none

    def test_justification_releases_it_but_it_is_still_flagged_loosened(self):
        history = [self.failed({"tol": 1e-9}, [self.A])]
        result = ve.assess_tolerance(history, "k", {"tol": 1e-3}, [self.A], "sensor noise floor is 1e-4")
        self.assertEqual((result["loosened"], result["withheld"]), (True, False))

    def test_changed_files_mean_a_fix_not_a_workaround(self):
        history = [self.failed({"tol": 1e-9}, [self.A])]
        result = ve.assess_tolerance(history, "k", {"tol": 1e-3}, [self.B], None)
        self.assertEqual((result["loosened"], result["unchanged"], result["withheld"]), (True, False, False))

    def test_equal_or_tighter_tolerance_is_not_loosening(self):
        history = [self.failed({"tol": 1e-9}, [self.A])]
        for tol in (1e-9, 1e-12):
            self.assertFalse(ve.assess_tolerance(history, "k", {"tol": tol}, [self.A], None)["loosened"])

    def test_either_of_rtol_and_atol_counts(self):
        history = [self.failed({"rtol": 1e-9, "atol": 1e-12}, [self.A])]
        for tol in ({"rtol": 1e-6, "atol": 1e-12}, {"rtol": 1e-9, "atol": 1e-6}):
            self.assertTrue(ve.assess_tolerance(history, "k", tol, [self.A], None)["withheld"], tol)
        self.assertFalse(ve.assess_tolerance(history, "k", {"rtol": 1e-12, "atol": 1e-12}, [self.A], None)["loosened"])

    def test_keys_ending_in_min_are_minimums_so_smaller_is_looser(self):
        history = [self.failed({"order_min": 3.75, "repeat_min": 3}, [self.A])]
        for tol in ({"order_min": 3.0, "repeat_min": 3}, {"order_min": 3.75, "repeat_min": 1}):
            result = ve.assess_tolerance(history, "k", tol, [self.A], None)
            self.assertEqual((result["loosened"], result["withheld"]), (True, True), tol)
        for tol in ({"order_min": 3.75, "repeat_min": 3}, {"order_min": 3.9, "repeat_min": 5}):
            self.assertFalse(ve.assess_tolerance(history, "k", tol, [self.A], None)["loosened"], tol)

    def test_a_limit_and_a_minimum_in_the_same_record(self):
        history = [self.failed({"tol": 1e-9, "repeat_min": 2}, [self.A])]
        self.assertTrue(ve.assess_tolerance(history, "k", {"tol": 1e-3, "repeat_min": 2}, [self.A], None)["loosened"])
        self.assertTrue(ve.assess_tolerance(history, "k", {"tol": 1e-9, "repeat_min": 1}, [self.A], None)["loosened"])
        self.assertFalse(ve.assess_tolerance(history, "k", {"tol": 1e-12, "repeat_min": 3}, [self.A], None)["loosened"])

    def test_other_checks_do_not_interfere(self):
        history = [self.failed({"tol": 1e-9}, [self.A], key="other")]
        self.assertFalse(ve.assess_tolerance(history, "k", {"tol": 1.0}, [self.A], None)["loosened"])

    def test_missing_or_unhashable_subjects_are_not_unchanged(self):
        history = [self.failed({"tol": 1e-9}, [sub("s.py", None)])]
        result = ve.assess_tolerance(history, "k", {"tol": 1e-3}, [sub("s.py", None)], None)
        self.assertFalse(result["unchanged"])
        self.assertFalse(ve.assess_tolerance([self.failed({"tol": 1e-9}, [self.A])], "k", {"tol": 1e-3}, [], None)["unchanged"])

    def test_no_subjects_on_either_side_is_not_unchanged(self):
        result = ve.assess_tolerance([self.failed({"tol": 1e-9}, [])], "k", {"tol": 1e-3}, [], None)
        self.assertEqual((result["loosened"], result["unchanged"], result["withheld"]), (True, False, False))

    def test_non_numeric_tolerance_entries_are_ignored(self):
        history = [self.failed({"tol": 1e-9, "relaxed": False, "note": "x"}, [self.A])]
        result = ve.assess_tolerance(history, "k", {"tol": 1e-9, "relaxed": True, "note": "y"}, [self.A], None)
        self.assertFalse(result["loosened"])                      # True > False must not count as loosening

    def test_withheld_runs_are_skipped_when_finding_the_previous_run(self):
        """Regression: a withheld record says passed=true; it must not become the baseline."""
        withheld = dict(self.failed({"tol": 1e-3}, [self.A]), passed=True, exit_status=3)
        history = [self.failed({"tol": 1e-9}, [self.A]), withheld, dict(withheld)]
        result = ve.assess_tolerance(history, "k", {"tol": 1e-3}, [self.A], None)
        self.assertEqual((result["loosened"], result["withheld"]), (True, True))
        self.assertEqual(result["previous"]["tolerance"], {"tol": 1e-9})

    def test_extra_subjects_cannot_make_the_files_look_changed(self):
        extra_before, extra_now = dict(sub("x.txt", "1" * 64), role="extra"), dict(sub("x.txt", "2" * 64), role="extra")
        history = [self.failed({"tol": 1e-9}, [self.A, extra_before])]
        result = ve.assess_tolerance(history, "k", {"tol": 1e-3}, [self.A, extra_now], None)
        self.assertEqual((result["unchanged"], result["withheld"]), (True, True))
        added = ve.assess_tolerance([self.failed({"tol": 1e-9}, [self.A])], "k", {"tol": 1e-3},
                                    [self.A, dict(sub("new.txt", "3" * 64), role="extra")], None)
        self.assertTrue(added["withheld"])                                         # adding an extra does not help either

    def test_a_justified_pass_becomes_the_new_baseline(self):
        justified = dict(self.failed({"tol": 1e-3}, [self.A]), passed=True, exit_status=0)
        history = [self.failed({"tol": 1e-9}, [self.A]), justified]
        self.assertFalse(ve.assess_tolerance(history, "k", {"tol": 1e-3}, [self.A], None)["loosened"])

    def test_only_the_latest_run_of_a_check_counts(self):
        history = [self.failed({"tol": 1e-9}, [self.A]), dict(self.failed({"tol": 1e-9}, [self.A]), passed=True)]
        self.assertFalse(ve.assess_tolerance(history, "k", {"tol": 1.0}, [self.A], None)["loosened"])


class CliHelper(unittest.TestCase):
    """record_cli / finish are shared by every plugin's verifier, so they are tested directly as well."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.target = self.dir / "out.txt"
        self.target.write_text("1\n")

    def tearDown(self):
        self._tmp.cleanup()

    def args(self, **over):
        import argparse
        parser = argparse.ArgumentParser()
        ve.add_cli_options(parser)
        ns = parser.parse_args(["--evidence-dir", str(self.dir / ".verification")])
        for key, value in over.items():
            setattr(ns, key, value)
        return ns

    def record(self, args, passed=True, tol=1e-12):
        return ve.record_cli(args, tool="t", plugin="p", kind="compare", name="n", key="k", passed=passed,
                             tolerance={"tol": tol}, subject_paths=[str(self.target)], detail={})

    def test_no_evidence_records_nothing_and_returns_none(self):
        self.assertIsNone(self.record(self.args(no_evidence=True)))
        self.assertFalse((self.dir / ".verification").exists())

    def test_check_id_replaces_the_key_and_is_recorded(self):
        self.record(self.args(check_id="my-check"))
        (record,) = ve.read_log(self.dir / ".verification" / "log.jsonl")[0]
        self.assertEqual(record["key"], "manifest:my-check")
        self.assertEqual(record["detail"]["manifest_id"], "my-check")

    def test_extra_subjects_and_justification_are_recorded(self):
        extra = self.dir / "extra.txt"
        extra.write_text("x\n")
        self.record(self.args(subject=[str(extra)]), passed=False)
        summary = self.record(self.args(subject=[str(extra)], justify="measurement noise"), tol=1.0)
        (first, second) = ve.read_log(self.dir / ".verification" / "log.jsonl")[0]
        self.assertEqual(sorted((s["path"], s["role"]) for s in first["subjects"]),
                         [("extra.txt", "extra"), ("out.txt", "primary")])
        self.assertEqual((second["justification"], second["exit_status"]), ("measurement noise", 0))
        self.assertFalse(summary["withheld"])

    def test_finish_maps_every_outcome_to_one_exit_status(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.finish_cases()
        self.assertIn("evidence NOT recorded: disk full", err.getvalue())
        self.assertIn("x: m", err.getvalue())

    def finish_cases(self):
        self.assertEqual(ve.finish("x", True, None), 0)
        self.assertEqual(ve.finish("x", False, None), 1)
        self.assertEqual(ve.finish("x", True, {"error": "disk full"}), 2)
        self.assertEqual(ve.finish("x", False, {"error": "disk full"}), 1)
        self.assertEqual(ve.finish("x", True, {"exit_status": 0, "withheld": False}), 0)
        self.assertEqual(ve.finish("x", True, {"exit_status": 3, "withheld": True, "message": "m"}), 3)
        self.assertEqual(ve.finish("x", False, {"exit_status": 1, "withheld": False}), 1)

    def test_unwritable_evidence_dir_gives_an_error_summary_not_an_exception(self):
        (self.dir / ".verification").write_text("a file\n")
        summary = self.record(self.args())
        self.assertIn("--no-evidence", summary["error"])


class Freshness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.solver = self.dir / "solver.py"
        self.solver.write_text("def solve(): return 1\n")
        self.log = self.dir / ".verification" / "log.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def record(self, **over):
        args = dict(tool="t", plugin="p", kind="benchmark", name="kepler", key="k", passed=True, tolerance={"tol": 1e-12},
                    subjects=[ve.subject(str(self.solver), self.dir)])
        args.update(over)
        return ve.record_run(self.log, **args)

    def fresh(self, name=None):
        return ve.fresh(ve.read_log(self.log)[0], str(self.solver), self.dir, name)

    def test_a_passing_record_is_fresh_until_the_file_changes(self):
        self.record()
        self.assertTrue(self.fresh()["fresh_pass"])
        self.solver.write_text("def solve(): return 2\n")
        stale = self.fresh()
        self.assertFalse(stale["fresh_pass"])
        self.assertEqual(stale["stale_subjects"], ["solver.py"])

    def test_failed_withheld_and_unknown_files_are_not_fresh(self):
        self.record(passed=False)
        self.assertFalse(self.fresh()["fresh_pass"])
        self.assertIn("no evidence", ve.fresh([], str(self.solver), self.dir)["reason"])

    def test_withheld_runs_are_not_fresh_even_though_the_verdict_passed(self):
        self.record(passed=False, tolerance={"tol": 1e-12})
        record, assessment = self.record(passed=True, tolerance={"tol": 1.0})
        self.assertEqual((record["exit_status"], assessment["withheld"]), (3, True))
        self.assertFalse(self.fresh()["fresh_pass"])

    def test_a_record_that_says_failed_is_never_fresh_whatever_its_exit_status(self):
        crafted = [{"subjects": [ve.subject(str(self.solver), self.dir)], "passed": False, "exit_status": 0, "name": "k"}]
        self.assertFalse(ve.fresh(crafted, str(self.solver), self.dir)["fresh_pass"])

    def test_recording_on_a_tampered_log_flags_it(self):
        self.record(key="a")
        self.record(key="b")
        lines = self.log.read_bytes().split(b"\n")[:-1]
        self.log.write_bytes(lines[1] + b"\n")                                      # the first line removed
        record, _ = self.record(key="c")
        self.assertIn("log_chain_problem", record["flags"])

    def test_repeating_a_withheld_run_stays_withheld_and_never_fresh(self):
        self.record(passed=False, tolerance={"tol": 1e-12})
        for _ in range(3):
            record, assessment = self.record(passed=True, tolerance={"tol": 1.0})
            self.assertEqual((record["exit_status"], assessment["withheld"]), (3, True))
        self.assertFalse(self.fresh()["fresh_pass"])
        record, _ = self.record(passed=True, tolerance={"tol": 1.0}, justification="stated accuracy need is 1e-1")
        self.assertEqual(record["exit_status"], 0)
        self.assertEqual(self.record(passed=True, tolerance={"tol": 1.0})[0]["exit_status"], 0)   # justified pass is the baseline

    def test_subject_records_carry_their_role(self):
        self.assertEqual(ve.subject(str(self.solver), self.dir)["role"], "primary")
        self.assertEqual(ve.subject(str(self.solver), self.dir, "extra")["role"], "extra")

    def test_name_filter(self):
        self.record(name="kepler")
        self.assertTrue(self.fresh("kepler")["fresh_pass"])
        self.assertFalse(self.fresh("sod")["fresh_pass"])

    def test_exit_status_follows_the_verdict(self):
        self.assertEqual(self.record(key="a")[0]["exit_status"], 0)
        self.assertEqual(self.record(key="b", passed=False)[0]["exit_status"], 1)


class ModuleCommandLine(unittest.TestCase):
    """python3 verification_evidence.py verify|show|fresh (the same file ships in every plugin)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.solver = self.dir / "solver.py"
        self.solver.write_text("def solve(): return 1\n")
        self.log = self.dir / ".verification" / "log.jsonl"
        for key in ("a", "b"):
            ve.record_run(self.log, tool="t", plugin="p", kind="benchmark", name="kepler", key=key, passed=True,
                          tolerance={"tol": 1e-12}, subjects=[ve.subject(str(self.solver), self.dir)])

    def tearDown(self):
        self._tmp.cleanup()

    def run_module(self, *args):
        env = dict(os.environ, VERIFICATION_EVIDENCE_DIR=str(self.dir / ".verification"))
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "verification_evidence.py"), *args],
                              capture_output=True, text=True, env=env)

    def test_verify_show_and_fresh(self):
        self.assertEqual(self.run_module("verify").returncode, 0)
        self.assertEqual(len(json.loads(self.run_module("show", "--last", "1").stdout)), 1)
        self.assertEqual(len(json.loads(self.run_module("show").stdout)), 2)
        fresh = self.run_module("fresh", "--subject", str(self.solver))
        self.assertEqual((fresh.returncode, json.loads(fresh.stdout)["fresh_pass"]), (0, True))
        self.assertEqual(self.run_module("fresh", "--subject", str(self.solver), "--name", "other").returncode, 1)
        self.solver.write_text("def solve(): return 2\n")
        self.assertEqual(self.run_module("fresh", "--subject", str(self.solver)).returncode, 1)     # stale

    def test_fresh_needs_a_subject_and_a_trustworthy_log(self):
        self.assertEqual(self.run_module("fresh").returncode, 2)
        lines = self.log.read_bytes().split(b"\n")[:-1]
        self.log.write_bytes(lines[1] + b"\n")                                  # the first line removed
        tampered = self.run_module("verify")
        self.assertEqual(tampered.returncode, 1)
        self.assertIn("chain broken", tampered.stderr)
        self.assertEqual(self.run_module("fresh", "--subject", str(self.solver)).returncode, 1)

    def test_unknown_action_exits_two(self):
        self.assertEqual(self.run_module("bogus").returncode, 2)


class WorkTree(unittest.TestCase):
    """worktree_digest / tree_subject: one hash for every tracked and untracked-not-ignored file of a git work tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "a.py").write_text("A = 1\n")
        (self.root / ".gitignore").write_text("build/\n")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def digest(self):
        return ve.worktree_digest(self.root)[1]

    def test_same_files_same_digest_from_any_subdirectory(self):
        self.assertEqual(self.digest(), self.digest())
        self.assertEqual(ve.worktree_digest(self.root / "src")[1], self.digest())

    def test_content_name_presence_and_untracked_files_change_it(self):
        start = self.digest()
        changes = [lambda: (self.root / "src" / "a.py").write_text("A = 2\n"),
                   lambda: (self.root / "src" / "new.py").write_text("B = 1\n"),
                   lambda: (self.root / "src" / "new.py").rename(self.root / "src" / "renamed.py"),
                   lambda: (self.root / "src" / "a.py").unlink()]
        seen = {start}
        for change in changes:
            change()
            value = self.digest()
            self.assertNotIn(value, seen)
            seen.add(value)

    def test_ignored_files_caches_and_the_evidence_log_do_not(self):
        start = self.digest()
        for rel in ("build/out.bin", "src/__pycache__/a.cpython-313.pyc", ".pytest_cache/v/x", ".verification/log.jsonl",
                    "src/b.pyc"):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        self.assertEqual(self.digest(), start)

    def test_symlink_target_counts(self):
        link = self.root / "src" / "link.py"
        link.symlink_to("a.py")
        first = self.digest()
        link.unlink()
        link.symlink_to("other.py")
        self.assertNotEqual(self.digest(), first)

    def test_outside_a_repository_is_an_error(self):
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        old = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = self._tmp.name
        try:
            with self.assertRaises(ve.EvidenceError):
                ve.worktree_digest(plain)
        finally:
            if old is None:
                os.environ.pop("GIT_CEILING_DIRECTORIES", None)
            else:
                os.environ["GIT_CEILING_DIRECTORIES"] = old

    def test_tree_subject_path_is_relative_to_the_log_and_fresh_follows_the_tree(self):
        outside = Path(self._tmp.name) / "ci-temp"                       # a log that lives outside the project
        log = outside / "log.jsonl"
        tree = ve.tree_subject(self.root, outside)
        self.assertEqual((tree["path"], tree["role"]), (os.path.relpath(self.root, outside), "tree"))
        ve.record_run(log, tool="t", plugin="p", kind="tests", name="n", key="k", passed=True, tolerance={}, subjects=[tree])
        records = ve.read_log(log)[0]
        self.assertTrue(ve.fresh(records, str(self.root), outside, kind="tests")["fresh_pass"])
        self.assertFalse(ve.fresh(records, str(self.root), outside, kind="ratchet")["fresh_pass"])
        (self.root / "src" / "a.py").write_text("A = 3\n")
        self.assertFalse(ve.fresh(records, str(self.root), outside, kind="tests")["fresh_pass"])


if __name__ == "__main__":
    unittest.main()
