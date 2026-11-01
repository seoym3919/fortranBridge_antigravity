"""Tests for scripts/claim_guard.py, the Stop hook that blocks unverified equivalence claims.

The evidence records come from the real translation_verifier.py CLI (no hand-written log lines), so the hook is tested
against what the verifier actually writes. No tolerance is loosened anywhere. Exit codes: verifier 0 equal, 1 different.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "claim_guard.py"
VERIFIER = ROOT / "scripts" / "translation_verifier.py"
sys.path.insert(0, str(ROOT / "scripts"))

sys.path.insert(0, str(ROOT / "tests"))

import claim_guard as cg  # noqa: E402
import test_port_completeness as tpc  # noqa: E402

CLAIM = "The Python port is numerically equivalent to the original Fortran."
REF = [1.0 / (k + 1) for k in range(50)]


SOURCE = """\
subroutine half(x, y)
  double precision x, y
  y = sin(x) * 0.5d0
end subroutine half
program p
  double precision a, b
  a = 3.0d0
  call half(a, b)
  print *, b
end program p
"""
PORT = """\
import math as _fm


def half(x, y):
    y = _fm.sin(x) * 0.5
    return y


def main():
    b = 0.0
    a = 3.0
    b = half(a, b)
    print(b)
"""
DRIVER = """\
program drive
  double precision a, b
  character(len=500) :: path
  call get_environment_variable("FB_UNIT_OUT", path)
  read(*, *) a
  call half(a, b)
  open(77, file=path, access="stream", form="unformatted")
  write(77) b
  close(77)
end program drive
"""
UNIT_TEST = """\
import os, runpy, sys
import numpy as np
half = runpy.run_path("a.py")["half"]
np.array([half(float(sys.stdin.read()), 0.0)], dtype=np.float64).tofile(os.environ["FB_UNIT_OUT"])
"""
TEMPLATES = {}


def tool(directory: Path, script: str, *args, **kwargs):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args], cwd=directory, capture_output=True, text=True, **kwargs)


def must(done, label=""):
    assert done.returncode == 0, label + done.stdout + done.stderr


def approve(directory: Path):
    """A human types the code: the real hook, on a real prompt."""
    code = tool(directory, "conversion_plan.py", "--approval-code", "a.f90", "--target", "python").stdout.strip()
    payload = {"prompt": f"APPROVE PLAN {code}", "cwd": str(directory)}
    done = tool(directory, "plan_approval.py", input=json.dumps(payload))
    assert "approved by the user" in done.stdout, done.stdout + done.stderr


def golden(directory: Path, count: int = 11):
    folder = directory / "golden"
    folder.mkdir(exist_ok=True)
    (folder / "drive.f90").write_text(DRIVER)
    (folder / "test_port.py").write_text(UNIT_TEST)
    cases = []
    for k in range(count):
        (folder / f"c{k}.in").write_text(f"{1.5 + k}\n")
        cases.append({"name": f"c{k}", "class": "corner" if k < 3 else "nominal", "covers": "program" if k else ["half"],
                      "fortran_driver": "golden/drive.f90", "target_test": "golden/test_port.py",
                      "target_command": f"{sys.executable} golden/test_port.py", "stdin": f"golden/c{k}.in"})
    (folder / "extra.in").write_text("0.5\n")
    cases.append({**cases[1], "name": "extra", "stdin": "golden/extra.in"})
    (folder / "cases.json").write_text(json.dumps({"cases": cases}))


def build(directory: Path, source: str = SOURCE, port: str = PORT, units: bool = True):
    """A whole conversion by the real tools, in the required order."""
    (directory / "a.f90").write_text(source)
    (directory / "a.py").write_text(port)
    (directory / "in.deck").write_text("1\n")
    must(tool(directory, "conversion_plan.py", "a.f90", "--target", "python"), "plan")
    approve(directory)
    golden(directory)
    must(tool(directory, "regression_test.py", "record"), "golden")
    if units:
        must(tool(directory, "unit_compare.py", "--function", "half", "--fortran-driver", "golden/drive.f90", "--target-test", "golden/test_port.py",
                  "--target-command", f"{sys.executable} golden/test_port.py", "--translated", "a.py", "--inputs", "golden/c0.in"), "unit")
        must(tool(directory, "regression_test.py", "run", "--after", "half", "--translated", "a.py"), "after")
    must(tool(directory, "regression_test.py", "run", "--final", "--translated", "a.py"), "final")
    must(tool(directory, "identity_checker.py", "--original", "a.f90", "--translated", "a.py"), "identity")
    must(tool(directory, "trace_checker.py", "--original", "a.f90", "--translated", "a.py"), "trace")


def conversion(directory: Path, source: str = SOURCE, port: str = PORT, units: bool = True):
    """The finished conversion, built once for each program."""
    key = (source, port, units)
    if key not in TEMPLATES:
        template = tempfile.TemporaryDirectory()
        build(Path(template.name), source, port, units)
        TEMPLATES[key] = template
    shutil.copytree(TEMPLATES[key].name, directory, dirs_exist_ok=True)


def run_verifier(directory: Path, test_values, tolerance=("--atol", "0", "--max-ulp", "0"), source=SOURCE, port=PORT, units=True):
    conversion(directory, source, port, units)
    (directory / "ref.out").write_text("\n".join(repr(v) for v in REF) + "\n")
    (directory / "test.out").write_text("\n".join(repr(v) for v in test_values) + "\n")
    return subprocess.run(
        [sys.executable, str(VERIFIER), "--ref", "ref.out", "--test", "test.out", "--ref-source", "a.f90",
         "--test-source", "a.py", "--inputs", "in.deck", *tolerance], cwd=directory, capture_output=True, text=True).returncode


def hook(directory: Path, text: str, **extra):
    payload = {"last_assistant_message": text, "cwd": str(directory), **extra}
    done = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload), capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout) if done.stdout.strip() else None


class ClaimDetection(unittest.TestCase):
    def test_plain_claim_is_detected(self):
        self.assertEqual(len(cg.unsupported_claims(CLAIM)), 1)

    def test_hedged_negated_or_quoted_sentences_are_not_claims(self):
        for text in ("The port is not numerically equivalent yet.",
                     "Claim equivalence only if the verifier exits 0.",
                     "Is the port numerically equivalent to the Fortran?",
                     "Rule: the translation must be bitwise identical to the original.",
                     "Use `numerically equivalent to the Fortran` only after verification.",
                     "These two regexes are equivalent.",
                     "```\nthe port is complete\n```"):
            with self.subTest(text=text):
                self.assertEqual(cg.unsupported_claims(text), [])


class HookBehaviour(unittest.TestCase):
    def test_claim_without_log_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            answer = hook(Path(tmp), CLAIM)
        self.assertEqual(answer["decision"], "block")
        self.assertIn("no translation_verifier compare record", answer["reason"])

    def test_no_claim_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hook(Path(tmp), "I translated the file; nothing is verified yet."))

    def test_claim_after_passing_verifier_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_verifier(Path(tmp), REF), 0)
            self.assertIsNone(hook(Path(tmp), CLAIM))

    def test_claim_after_failing_verifier_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = list(REF)
            bad[10] *= 1.001
            self.assertEqual(run_verifier(Path(tmp), bad), 1)
            answer = hook(Path(tmp), CLAIM)
        self.assertEqual(answer["decision"], "block")
        self.assertIn("failed", answer["reason"])

    def test_claim_after_translation_edit_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(run_verifier(directory, REF), 0)
            (directory / "a.py").write_text("edited after verification\n")
            answer = hook(directory, CLAIM)
        self.assertEqual(answer["decision"], "block")
        self.assertIn("changed since", answer["reason"])

    def test_tampered_log_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            # The hash chain covers every line that a later line follows, so edit the first of two records.
            self.assertEqual(run_verifier(directory, REF), 0)
            self.assertEqual(run_verifier(directory, REF), 0)
            log = directory / ".verification" / "log.jsonl"
            lines = log.read_text().splitlines()
            lines[0] = lines[0].replace('"passed": true', '"passed":  true', 1)
            log.write_text("\n".join(lines) + "\n")
            answer = hook(directory, CLAIM)
        self.assertEqual(answer["decision"], "block")

    def test_stop_hook_active_never_loops(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hook(Path(tmp), CLAIM, stop_hook_active=True))

    def test_garbage_input_does_not_fail(self):
        done = subprocess.run([sys.executable, str(GUARD)], input="not json", capture_output=True, text=True)
        self.assertEqual(done.returncode, 0)

    def test_transcript_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text(json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": CLAIM}]}}) + "\n")
            done = subprocess.run([sys.executable, str(GUARD)], capture_output=True, text=True,
                                  input=json.dumps({"transcript_path": str(transcript), "cwd": tmp}))
        self.assertEqual(json.loads(done.stdout)["decision"], "block")

    def test_hooks_json_registers_the_guard(self):
        hooks = json.loads((ROOT / "hooks.json").read_text())["fortranbridge-gate"]
        self.assertIn("claim_guard.py", json.dumps(hooks["Stop"]))

    def test_antigravity_stop_transcript_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text(json.dumps({"type": "PLANNER_RESPONSE", "content": CLAIM}) + "\n")
            done = subprocess.run([sys.executable, str(GUARD)], capture_output=True, text=True,
                                  input=json.dumps({"transcriptPath": str(transcript), "workspacePaths": [tmp], "terminationReason": "TERMINATION_REASON_NORMAL"}))
        self.assertEqual(json.loads(done.stdout)["decision"], "continue")


class StructuredMarkerAndStrictMode(unittest.TestCase):
    def test_verdict_line_is_gated_without_any_fortran_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            answer = hook(Path(tmp), "All done.\nVERDICT: equivalent")
        self.assertEqual(answer["decision"], "block")
        self.assertIn("VERDICT: equivalent", answer["reason"])

    def test_unverified_verdict_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hook(Path(tmp), "VERDICT: unverified"))

    def test_verdict_equivalent_after_passing_verifier_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_verifier(Path(tmp), REF), 0)
            self.assertIsNone(hook(Path(tmp), "VERDICT: equivalent"))

    def test_broader_wordings_are_claims(self):
        for text in ("The Python port reproduces the Fortran results exactly.",
                     "The translation gives the same results as the original Fortran.",
                     "The C++ output agrees with the original Fortran."):
            with self.subTest(text=text):
                self.assertEqual(len(cg.unsupported_claims(text)), 1)

    def test_strict_mode_gates_a_bare_claim_only_in_a_fortran_project(self):
        bare = "The output agrees with the original."
        self.assertEqual(cg.unsupported_claims(bare), [])
        self.assertEqual(len(cg.unsupported_claims(bare, strict=True)), 1)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hook(Path(tmp), bare))
            (Path(tmp) / "model.f90").write_text("program p\nend program p\n")
            self.assertEqual(hook(Path(tmp), bare)["decision"], "block")


class LogHeadMemory(unittest.TestCase):
    def test_editing_the_only_record_after_it_was_seen_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(run_verifier(directory, REF), 0)
            self.assertIsNone(hook(directory, "Working on it."))        # the guard now remembers the head
            log = directory / ".verification" / "log.jsonl"
            text = log.read_text()
            at = text.rindex('"passed": true')
            log.write_text(text[:at] + '"passed":  true' + text[at + len('"passed": true'):])
            answer = hook(directory, CLAIM)
        self.assertEqual(answer["decision"], "block")
        self.assertIn("seen earlier", answer["reason"])

    def test_truncating_the_log_after_it_was_seen_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(run_verifier(directory, REF), 0)
            self.assertEqual(run_verifier(directory, REF), 0)
            self.assertIsNone(hook(directory, "Working on it."))
            log = directory / ".verification" / "log.jsonl"
            log.write_text(log.read_text().splitlines()[0] + "\n")
            answer = hook(directory, CLAIM)
        self.assertEqual(answer["decision"], "block")

    def test_appending_records_is_not_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(run_verifier(directory, REF), 0)
            self.assertIsNone(hook(directory, "Working on it."))
            self.assertEqual(run_verifier(directory, REF), 0)
            self.assertIsNone(hook(directory, CLAIM))


class BitwiseEvidence(unittest.TestCase):
    """A pass within a tolerance says the numbers are close; an equivalence claim needs bitwise identity."""

    def test_pass_within_the_default_tolerance_does_not_support_a_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(run_verifier(root, REF, tolerance=()), 0)
            answer = hook(root, "VERDICT: equivalent")
            self.assertEqual(answer["decision"], "block")
            self.assertIn("not bitwise", answer["reason"])

    def test_bitwise_pass_supports_a_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(run_verifier(root, REF), 0)
            self.assertIsNone(hook(root, "VERDICT: equivalent"))


@unittest.skipUnless(shutil.which("gfortran") and shutil.which("gprof"), "the checks read gfortran and gprof")
class IdenticalAlgorithmVerdict(unittest.TestCase):
    """Same equations and algorithm, a library differs in the last bits: never reported as equivalent."""

    CLAIM = "VERDICT: identical algorithm"
    EPS = 2.0 ** -52

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        conversion(self.root)
        must(tool(self.root, "trace_checker.py", "--original", "a.f90", "--translated", "a.py", "--machine-precision"))
        (self.root / "ref.out").write_text(repr(1.0) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def compare(self, value, *tolerance):
        (self.root / "test.out").write_text(repr(value) + "\n")
        return subprocess.run([sys.executable, str(VERIFIER), "--ref", "ref.out", "--test", "test.out", "--ref-source", "a.f90",
                               "--test-source", "a.py", "--inputs", "in.deck", *tolerance], cwd=self.root, capture_output=True,
                              text=True).returncode

    def machine(self):
        return ("--atol", "0", "--tol", repr(self.EPS), "--max-ulp", "2")

    def test_passes_with_a_library_and_machine_precision(self):
        self.assertEqual(self.compare(1.0 + self.EPS, *self.machine()), 0)
        self.assertIsNone(hook(self.root, self.CLAIM))

    def test_last_bit_difference_is_not_equivalent(self):
        self.assertEqual(self.compare(1.0 + self.EPS, *self.machine()), 0)
        self.assertEqual(hook(self.root, "VERDICT: equivalent")["decision"], "block")

    def test_a_looser_tolerance_is_refused(self):
        self.assertEqual(self.compare(1.0, "--atol", "0", "--tol", "1e-14", "--max-ulp", "64"), 0)
        self.assertIn("looser than machine precision", hook(self.root, self.CLAIM)["reason"])

    def test_more_than_machine_precision_does_not_pass_the_verifier(self):
        self.assertEqual(self.compare(1.0 + 8 * self.EPS, *self.machine()), 1)
        self.assertEqual(hook(self.root, self.CLAIM)["decision"], "block")

    def test_machine_precision_trace_does_not_support_equivalent(self):
        self.assertEqual(self.compare(1.0, "--atol", "0", "--max-ulp", "0"), 0)
        self.assertIn("within machine precision", hook(self.root, "VERDICT: equivalent")["reason"])

    def test_a_translation_edited_afterwards_is_refused(self):
        self.assertEqual(self.compare(1.0, *self.machine()), 0)
        (self.root / "a.py").write_text(PORT + "# edited\n")
        self.assertEqual(hook(self.root, self.CLAIM)["decision"], "block")

    def test_a_forged_identity_record_is_refused(self):
        """The hook repeats the static check: a passing record in the log is not enough."""
        self.assertEqual(self.compare(1.0, *self.machine()), 0)
        (self.root / "a.py").write_text(PORT.replace("_fm.sin(x) * 0.5", "_fm.sin(x) / 2.0"))
        log = self.root / ".verification" / "log.jsonl"
        lines = [json.loads(line) for line in log.read_text().splitlines()]
        digest = cg.ve.sha256_file(self.root / "a.py")
        for record in lines:
            for subject in record.get("subjects", []):
                if subject.get("path") == "a.py":
                    subject["sha256"] = digest
        log.unlink()
        (self.root / ".verification" / "guard_head").unlink(missing_ok=True)
        for record in lines:
            cg.ve.append(log, {k: v for k, v in record.items() if k not in ("timestamp", "prev_sha256", "sha256", "line_sha256")})
        self.assertIn("fails when repeated", hook(self.root, self.CLAIM)["reason"])

    def test_no_library_means_no_machine_precision(self):
        plain = (SOURCE.replace("sin(x) * 0.5d0", "x * 0.5d0"), PORT.replace("_fm.sin(x) * 0.5", "x * 0.5"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conversion(root, *plain)
            must(tool(root, "trace_checker.py", "--original", "a.f90", "--translated", "a.py", "--machine-precision"))
            (root / "ref.out").write_text("1.0\n")
            (root / "test.out").write_text("1.0\n")
            subprocess.run([sys.executable, str(VERIFIER), "--ref", "ref.out", "--test", "test.out", "--ref-source", "a.f90", "--test-source", "a.py",
                            "--inputs", "in.deck", *self.machine()], cwd=root, capture_output=True, text=True, check=True)
            self.assertIn("no Fortran-specific library", hook(root, self.CLAIM)["reason"])


@unittest.skipUnless(shutil.which("gfortran") and shutil.which("gprof"), "the checks read gfortran and gprof")
class ConversionSteps(unittest.TestCase):
    """Each step of the strategy is needed by a claim."""

    def claim(self, *remove, source=SOURCE, port=PORT, units=True, text="VERDICT: equivalent"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(run_verifier(root, REF, source=source, port=port, units=units), 0)
            lines = (root / ".verification" / "log.jsonl").read_text().splitlines()
            kept = [json.loads(line) for line in lines]
            kept = [r for r in kept if r.get("name") not in remove and r.get("kind") not in remove]
            log = root / ".verification" / "log.jsonl"
            log.unlink()
            for record in kept:
                cg.ve.append(log, {k: v for k, v in record.items() if k not in ("timestamp", "prev_sha256", "sha256", "line_sha256")})
            return hook(root, text)

    def test_the_whole_strategy_supports_the_claim(self):
        self.assertIsNone(self.claim())

    def test_each_missing_step_blocks(self):
        for missing, why in (("plan", "no conversion_plan"), ("plan_approval", "no fresh human approval"), ("golden", "no fresh golden data"),
                             ("regression:final", "final regression"), ("identity", "no identity_checker"), ("trace", "no trace_checker")):
            with self.subTest(missing):
                self.assertIn(why, self.claim(missing)["reason"])

    def test_complete_needs_a_unit_test_and_a_regression_per_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(run_verifier(root, REF, units=False), 0)
            records, _ = cg.ve.read_log(root / ".verification" / "log.jsonl")
            plan = next(r for r in records if r.get("kind") == "plan")
            self.assertIn("unit test and regression for: half", cg._plan_problem(plan, records, root, True, False))

    def test_editing_an_approved_plan_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(run_verifier(root, REF), 0)
            (root / "PLAN.md").write_text("edited\n")
            self.assertIn("no fresh human approval", hook(root, "VERDICT: equivalent")["reason"])


class InstructionsDoNotInviteDrift(unittest.TestCase):
    """What the translators are told must not ask for a different algorithm from the one the checks enforce."""

    def test_no_instruction_asks_for_a_changed_algorithm(self):
        banned = ("Transpose nested loops", "interchange nested loops", "interchange loops /", "vectorize NumPy kernels",
                  "fastmath=True", "parallel=True", "vectorized NumPy/SciPy", "Replaces scalar `DO` loops")
        for path in [*ROOT.glob("rules/*.md"), *ROOT.glob("skills/*/SKILL.md"), *ROOT.glob("agents/*.md")]:
            text = path.read_text(encoding="utf-8")
            for phrase in banned:
                with self.subTest(path=path.name, phrase=phrase):
                    self.assertNotIn(phrase, text)


@unittest.skipUnless(shutil.which("gfortran") and shutil.which("gprof"), "the checks read gfortran and gprof")
class CompletenessRecord(unittest.TestCase):
    """port_completeness.py writes a `completeness` record; "port complete" claims need a fresh passing one."""

    UNIT = """
def test_half():
    got = half(3.0, 0.0)
    assert ulp_distance(got, 0.07056000402993) == 0 and not zero_signbit_differs(got, 0.07056000402993)
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        conversion(self.root)
        for name, text in (("test_port.py", self.UNIT), ("ref.txt", "0.5\n"), ("out.txt", "0.5\n"), ("inputs.txt", "1\n")):
            (self.root / name).write_text(text)
        tpc._log(self.root, [self.root / "a.f90"], [self.root / "a.py"], [self.root / n for n in ("ref.txt", "out.txt", "inputs.txt")])

    def tearDown(self):
        self.tmp.cleanup()

    def completeness(self):
        return tool(self.root, "port_completeness.py", "--original", "a.f90", "--translated", "a.py", "--target", "python",
                    "--unit-tests", "test_port.py").returncode

    def records(self, kind):
        lines = (self.root / ".verification" / "log.jsonl").read_text().splitlines()
        return [r for r in map(json.loads, lines) if r.get("kind") == kind]

    def test_complete_claim_needs_the_completeness_record(self):
        claim = "VERDICT: complete"
        self.assertEqual(hook(self.root, claim)["decision"], "block")
        done = tool(self.root, "port_completeness.py", "--original", "a.f90", "--translated", "a.py", "--target", "python", "--unit-tests", "test_port.py")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIsNone(hook(self.root, claim))

    def test_failed_completeness_blocks_and_is_recorded(self):
        (self.root / "test_port.py").write_text("def test_half():\n    assert True\n")
        self.assertEqual(self.completeness(), 1)
        self.assertFalse(self.records("completeness")[-1]["passed"])
        answer = hook(self.root, "The port is complete.")
        self.assertEqual(answer["decision"], "block")
        self.assertIn("failed", answer["reason"])

    def test_editing_the_translation_after_completeness_blocks(self):
        self.assertEqual(self.completeness(), 0)
        (self.root / "a.py").write_text(PORT + "\n# edited\n")
        self.assertEqual(hook(self.root, "VERDICT: complete")["decision"], "block")

    def test_equivalence_alone_does_not_satisfy_a_completeness_claim(self):
        self.assertIsNone(hook(self.root, "VERDICT: equivalent"))
        self.assertEqual(hook(self.root, "VERDICT: complete")["decision"], "block")

    def test_compare_of_other_files_does_not_support_a_complete_claim(self):
        self.assertEqual(self.completeness(), 0)
        for name in ("other.f90", "other.py"):
            (self.root / name).write_text(name)
        tpc._log(self.root, [self.root / "other.f90"], [self.root / "other.py"],
                 [self.root / n for n in ("ref.txt", "out.txt", "inputs.txt")])
        answer = hook(self.root, "VERDICT: complete")
        self.assertEqual(answer["decision"], "block")
        self.assertIn("does not cover", answer["reason"])

    def test_no_evidence_flag_writes_no_record(self):
        done = tool(self.root, "port_completeness.py", "--original", "a.f90", "--translated", "a.py", "--target", "python",
                    "--unit-tests", "test_port.py", "--no-evidence")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.records("completeness"), [])


if __name__ == "__main__":
    unittest.main()
