"""Tests for the rules of a conversion: a plan first, a byte-identical unit test per function, machine precision only for a library.

The plan and the unit results come from gfortran, gprof and the two programs themselves. No expected value here is numeric.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import conversion_plan as cp  # noqa: E402

SOURCE = """\
subroutine leaf(x, y)
  double precision x, y
  y = x * 0.5d0
end subroutine leaf
subroutine wave(x, y)
  double precision x, y
  y = sin(x) + 1.0d0
end subroutine wave
subroutine middle(x, y)
  double precision x, y, z
  call leaf(x, z)
  call wave(z, y)
end subroutine middle
subroutine never(x)
  double precision x
  x = x + 1.0d0
end subroutine never
program top
  double precision a, b
  a = 2.0d0
  call middle(a, b)
  if (a < 0.0d0) call never(a)
  print *, b
end program top
"""
DRIVER = """\
program drive
  double precision a, b
  character(len=500) :: path
  call get_environment_variable("FB_UNIT_OUT", path)
  a = 3.0d0
  call {name}(a, b)
  open(77, file=path, access="stream", form="unformatted")
  write(77) b
  close(77)
end program drive
"""
TARGET = """\
import math, os
import numpy as np
def leaf(x, y):
    return {leaf}
def wave(x, y):
    return {wave}
def never(x, y=None):
    return x + 1.0
np.array([{name}(3.0, 0.0)], dtype=np.float64).tofile(os.environ["FB_UNIT_OUT"])
"""
GOOD = {"leaf": "x * 0.5", "wave": "math.sin(x) + 1.0"}
NEVER_DRIVER = """\
program drive
  double precision a
  character(len=500) :: path
  call get_environment_variable("FB_UNIT_OUT", path)
  a = 3.0d0
  call never(a)
  open(77, file=path, access="stream", form="unformatted")
  write(77) a
  close(77)
end program drive
"""
GOLD_DRIVER = """\
program gold
  double precision a, b
  character(len=500) :: path
  call get_environment_variable("FB_UNIT_OUT", path)
  read(*, *) a
  call leaf(a, b)
  open(77, file=path, access="stream", form="unformatted")
  write(77) b
  close(77)
end program gold
"""
GOLD_TARGET = """\
import math, os, sys
import numpy as np
def leaf(x, y):
    return {leaf}
def wave(x, y):
    return {wave}
np.array([leaf(float(sys.stdin.read()), 0.0)], dtype=np.float64).tofile(os.environ["FB_UNIT_OUT"])
"""
MODULE_SOURCE = """\
module tools
  double precision, parameter :: scale = 2.0d0
contains
  double precision function twice(x)
    double precision x
    twice = scale * x
  end function twice
end module tools
subroutine user(x)
  use tools, only: scale
  double precision x
  x = x * scale
end subroutine user
program m
  use tools
  double precision a
  a = twice(1.0d0)
  call user(a)
  print *, a
end program m
"""


CHECK = shutil.which("gfortran") and shutil.which("gprof")


@unittest.skipUnless(CHECK, "gfortran and gprof are the reference")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a.f90").write_text(SOURCE)
        (self.root / "a.py").write_text("# the translation\n")
        done = self.tool("conversion_plan.py", "a.f90", "--target", "python")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.plan = json.loads((self.root / ".verification" / "conversion_plan.json").read_text())
        self.by_name = {entry["name"]: entry for entry in self.plan["functions"]}

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, script, *args, **kwargs):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args], cwd=self.root, capture_output=True, text=True, **kwargs)

    def code(self):
        return hashlib.sha256((self.root / "PLAN.md").read_bytes()).hexdigest()[:8]

    def say(self, prompt):
        return self.tool("plan_approval.py", input=json.dumps({"prompt": prompt, "cwd": str(self.root)})).stdout

    def golden(self, program_cases=11, corner=True, covers=("leaf", "wave", "middle", "never")):
        (self.root / "gold.f90").write_text(GOLD_DRIVER)
        (self.root / "gold.py").write_text(GOLD_TARGET.format(**GOOD))
        cases = []
        for k in range(program_cases + len(covers)):
            (self.root / f"g{k}.in").write_text(f"{k + 1.25}\n")
            cover = [covers[k]] if k < len(covers) else "program"
            cases.append({"name": f"g{k}", "class": "corner" if corner and k == 0 else "nominal", "covers": cover, "fortran_driver": "gold.f90",
                          "target_test": "gold.py", "target_command": f"{sys.executable} gold.py", "stdin": f"g{k}.in"})
        (self.root / "golden").mkdir(exist_ok=True)
        (self.root / "golden" / "cases.json").write_text(json.dumps({"cases": cases}))

    def ready(self):
        self.assertIn("approved by the user", self.say(f"APPROVE PLAN {self.code()}"))
        self.golden()
        done = self.tool("regression_test.py", "record")
        self.assertEqual(done.returncode, 0, done.stderr)

    def unit(self, name, leaf=None, wave=None, *flags, driver=None):
        (self.root / "drive.f90").write_text(driver or DRIVER.format(name=name))
        (self.root / "test.py").write_text(TARGET.format(name=name, leaf=leaf or GOOD["leaf"], wave=wave or GOOD["wave"]))
        return self.tool("unit_compare.py", "--function", name, "--fortran-driver", "drive.f90", "--target-test", "test.py",
                         "--target-command", f"{sys.executable} test.py", "--translated", "a.py", *flags)

    def after(self, name):
        return self.tool("regression_test.py", "run", "--after", name, "--translated", "a.py")

    def advance_to(self, name):
        """Everything ahead of `name` in the plan passes its unit test and regression."""
        for entry in self.plan["functions"]:
            if entry["order"] < self.by_name[name]["order"] and entry["kind"] not in ("program", "module"):
                done = self.unit(entry["name"], driver=None if entry["name"] != "never" else NEVER_DRIVER)
                self.assertEqual(done.returncode, 0, entry["name"] + done.stdout + done.stderr)
                done = self.after(entry["name"])
                self.assertEqual(done.returncode, 0, entry["name"] + done.stdout + done.stderr)


class Plan(Base):
    def test_the_lists_are_written(self):
        listing = (self.root / "FORTRAN_FUNCTION_LIST.md").read_text()
        for name in ("leaf", "wave", "middle", "never", "top"):
            self.assertIn(f"| {name} |", listing)
        plan = (self.root / "PLAN.md").read_text()
        for text in ("Conversion order", "byte identical", "Golden data", "nominal", "corner", "Approval"):
            self.assertIn(text, plan)

    def test_modules_are_units(self):
        (self.root / "m.f90").write_text(MODULE_SOURCE)
        plan = cp.build_plan([self.root / "m.f90"], "python", [], None)
        kinds = {entry["name"]: entry["kind"] for entry in plan["functions"]}
        self.assertEqual(kinds["tools"], "module")
        order = {entry["name"]: entry["order"] for entry in plan["functions"]}
        self.assertLess(order["tools"], order["twice"])
        self.assertLess(order["tools"], order["user"])
        self.assertIn("tools", next(e for e in plan["functions"] if e["name"] == "user")["calls"])

    def test_the_list_is_complete(self):
        self.assertEqual(set(self.by_name), {"leaf", "wave", "middle", "never", "top"})

    def test_links_come_from_the_profiled_run(self):
        pairs = {(p["caller"], p["callee"]) for p in self.plan["profile"]}
        self.assertLessEqual({("middle", "leaf"), ("middle", "wave")}, pairs)
        self.assertEqual(self.by_name["middle"]["calls"], ["leaf", "wave"])

    def test_a_link_no_run_took_is_kept_and_flagged(self):
        self.assertIn("never", self.by_name["top"]["calls"])
        self.assertFalse(self.by_name["never"]["profiled"])

    def test_callees_come_first(self):
        order = {name: entry["order"] for name, entry in self.by_name.items()}
        self.assertLess(order["leaf"], order["middle"])
        self.assertLess(order["wave"], order["middle"])
        self.assertLess(order["middle"], order["top"])

    def test_library_reach_follows_the_calls(self):
        self.assertEqual(self.by_name["leaf"]["library"], [])
        self.assertEqual(self.by_name["wave"]["library"], ["sin"])
        self.assertEqual(self.by_name["middle"]["library"], ["sin"])
        self.assertEqual(self.by_name["top"]["library"], ["sin"])

    def test_a_cycle_is_converted_as_one_group(self):
        groups = cp.conversion_order(["a", "b", "c"], {"a": {"b"}, "b": {"a", "c"}, "c": set()})
        self.assertEqual(groups, [["c"], ["a", "b"]])

    def test_a_second_run_does_not_overwrite_an_edited_plan(self):
        (self.root / "PLAN.md").write_text("reviewed text\n")
        self.assertEqual(self.tool("conversion_plan.py", "a.f90", "--target", "python").returncode, 0)
        self.assertEqual((self.root / "PLAN.md").read_text(), "reviewed text\n")


class Approval(Base):
    def test_only_the_right_code_approves(self):
        self.assertIn("not for the current PLAN.md", self.say("APPROVE PLAN 00000000"))
        self.assertFalse((self.root / ".verification" / "plan_approval.json").exists())

    def test_other_messages_are_not_approval(self):
        for prompt in ("yes, go ahead", "looks good", f"please approve plan {self.code()} tomorrow"):
            with self.subTest(prompt):
                self.assertEqual(self.say(prompt).strip(), "")
                self.assertFalse((self.root / ".verification" / "plan_approval.json").exists())

    def test_nothing_runs_before_approval(self):
        self.golden()
        for done in (self.tool("regression_test.py", "record"), self.unit("leaf")):
            self.assertEqual(done.returncode, 2)
            self.assertIn("not approved", done.stderr)

    def test_an_edit_voids_the_approval(self):
        self.ready()
        with (self.root / "PLAN.md").open("a") as handle:
            handle.write("one more line\n")
        done = self.unit("leaf")
        self.assertEqual(done.returncode, 2)
        self.assertIn("changed since it was approved", done.stderr)

    def test_the_hook_is_registered(self):
        hooks = json.loads((ROOT / "hooks.json").read_text())["fortranbridge-gate"]
        self.assertIn("plan_approval.py", json.dumps(hooks["PreInvocation"]))
        self.assertIn("claim_guard.py", json.dumps(hooks["Stop"]))

    def test_antigravity_pre_invocation_payload(self):
        done = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "plan_approval.py")],
            input=json.dumps({
                "lastUserInput": f"APPROVE PLAN {self.code()}",
                "workspacePaths": [str(self.root)],
                "conversationId": "c1",
            }),
            capture_output=True, text=True, check=True,
        )
        out = json.loads(done.stdout)
        self.assertIn("approved by the user", out["injectSteps"][0]["ephemeralMessage"])


class Golden(Base):
    def setUp(self):
        super().setUp()
        self.assertIn("approved by the user", self.say(f"APPROVE PLAN {self.code()}"))

    def record(self, **kwargs):
        self.golden(**kwargs)
        return self.tool("regression_test.py", "record")

    def test_ten_cases_are_not_enough(self):
        done = self.record(program_cases=10)
        self.assertEqual(done.returncode, 2)
        self.assertIn("at least 11", done.stderr)

    def test_eleven_cases_are(self):
        done = self.record(program_cases=11)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_repeated_cases_count_once(self):
        self.golden()
        data = json.loads((self.root / "golden" / "cases.json").read_text())
        for case in data["cases"]:
            if case["covers"] == "program":
                case["stdin"] = "g0.in"
        (self.root / "golden" / "cases.json").write_text(json.dumps(data))
        self.assertEqual(self.tool("regression_test.py", "record").returncode, 2)

    def test_corner_cases_are_required(self):
        done = self.record(corner=False)
        self.assertEqual(done.returncode, 2)
        self.assertIn("corner", done.stderr)

    def test_every_function_is_covered(self):
        done = self.record(covers=("leaf", "wave", "middle"))
        self.assertEqual(done.returncode, 2)
        self.assertIn("never", done.stderr)

    def test_a_unit_test_needs_golden_data_first(self):
        done = self.unit("leaf")
        self.assertEqual(done.returncode, 2)
        self.assertIn("no golden data", done.stderr)


class Conversion(Base):
    def setUp(self):
        super().setUp()
        self.ready()

    def test_byte_identical_passes(self):
        done = self.unit("leaf")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("identical", done.stdout)

    def test_a_different_translation_fails(self):
        self.assertEqual(self.unit("leaf", leaf="x * 0.75").returncode, 1)

    def test_one_bit_fails_a_function_with_no_library(self):
        self.assertEqual(self.unit("leaf", leaf="math.nextafter(x * 0.5, 2.0)").returncode, 1)

    def test_machine_precision_is_refused_without_a_library(self):
        done = self.unit("leaf", "math.nextafter(x * 0.5, 2.0)", None, "--machine-precision", "float64")
        self.assertEqual(done.returncode, 2)
        self.assertIn("byte identical", done.stderr)

    def test_machine_precision_is_allowed_for_a_library_function(self):
        self.advance_to("wave")
        near = "math.nextafter(math.sin(x) + 1.0, 2.0)"
        self.assertEqual(self.unit("wave", None, near).returncode, 1)
        done = self.unit("wave", None, near, "--machine-precision", "float64")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("within machine precision", done.stdout)

    def test_machine_precision_does_not_cover_a_real_difference(self):
        self.advance_to("wave")
        self.assertEqual(self.unit("wave", None, "math.sin(x) + 1.0000001", "--machine-precision", "float64").returncode, 1)

    def test_out_of_order_is_refused(self):
        done = self.unit("wave")
        self.assertEqual(done.returncode, 2)
        self.assertIn("plan order", done.stderr)

    def test_the_regression_after_a_function_is_needed_before_the_next(self):
        self.assertEqual(self.unit("leaf").returncode, 0)
        done = self.unit("wave")
        self.assertEqual(done.returncode, 2)
        self.assertIn("regression", done.stderr)
        self.assertEqual(self.after("leaf").returncode, 0)
        self.assertEqual(self.unit("wave").returncode, 0)

    def test_a_regression_catches_a_change_against_stored_bytes(self):
        (self.root / "gold.py").write_text(GOLD_TARGET.format(leaf="x * 0.75", wave=GOOD["wave"]))
        done = self.after("leaf")
        self.assertEqual(done.returncode, 1)
        self.assertIn("DIFFERENT", done.stdout)

    def test_final_regression_runs_every_case(self):
        done = self.tool("regression_test.py", "run", "--final", "--translated", "a.py")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("15 of 15", done.stdout)

    def test_changed_golden_data_is_refused(self):
        (self.root / "golden" / "data" / "g5.bin").write_bytes(b"\0" * 8)
        done = self.after("leaf")
        self.assertEqual(done.returncode, 2)
        self.assertIn("changed since", done.stderr)

    def test_a_function_outside_the_plan_is_refused(self):
        done = self.tool("unit_compare.py", "--function", "ghost", "--fortran-driver", "gold.f90", "--target-test", "gold.py",
                         "--target-command", "true", "--translated", "a.py")
        self.assertEqual(done.returncode, 2)

    def test_a_test_that_does_not_call_the_function_is_refused(self):
        self.assertEqual(self.unit("leaf", driver=DRIVER.format(name="wave")).returncode, 2)

    def test_changed_sources_need_a_new_plan(self):
        (self.root / "a.f90").write_text(SOURCE.replace("0.5d0", "0.25d0"))
        done = self.unit("leaf")
        self.assertEqual(done.returncode, 2)
        self.assertIn("Make the plan again", done.stderr)

    def test_results_come_from_running_both_sides(self):
        """A target that writes nothing has nothing to compare, whatever files lie around."""
        (self.root / "drive.f90").write_text(DRIVER.format(name="leaf"))
        (self.root / "test.py").write_text("# leaf\n")
        done = self.tool("unit_compare.py", "--function", "leaf", "--fortran-driver", "drive.f90", "--target-test", "test.py",
                         "--target-command", f"{sys.executable} test.py", "--translated", "a.py")
        self.assertEqual(done.returncode, 2)


class Parsing(unittest.TestCase):
    def test_gprof_names(self):
        self.assertEqual(cp._procedure_name("__geom_MOD_energy"), "energy")
        self.assertEqual(cp._procedure_name("poly_"), "poly")
        self.assertEqual(cp._procedure_name("add.3"), "add")

    def test_only_a_real_exponent_is_pow(self):
        proc = cp.ic.Proc("p", {"n": cp.ic.Sym(kind="i4"), "r": cp.ic.Sym(kind="r8")})
        for exponent, expected in (("n", set()), ("r", {"pow"})):
            found = set()
            cp._called(("assign", ("var", "x"), ("bin", "**", ("var", "x"), ("var", exponent))), found, proc)
            self.assertEqual(found, expected)

    def test_sqrt_is_not_a_library_excuse(self):
        """IEEE 754 fixes sqrt to the last bit: a difference there is a bug."""
        self.assertNotIn("sqrt", cp.LIBRARY_INTRINSICS)


if __name__ == "__main__":
    unittest.main()
