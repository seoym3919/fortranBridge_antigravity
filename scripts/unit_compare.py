#!/usr/bin/env python3
"""One function's unit test: the Fortran original and its translation, run on the same input, must write the same bytes.

This runs both sides itself, so a result cannot be supplied by hand. A Fortran driver calls the original procedure, a target test
calls the translation, and each writes its results as raw little-endian values to the file named by $FB_UNIT_OUT:

    Fortran   open(77, file=path, access='stream', form='unformatted'); write(77) result
    Python    result.tofile(os.environ["FB_UNIT_OUT"])          (a numpy array of the Fortran kind)
    C++       std::ofstream(path, std::ios::binary).write(reinterpret_cast<const char*>(&value), sizeof value)

The test passes only when the two files are byte identical. --machine-precision is refused unless the conversion plan shows the
function reaches a Fortran-specific library (conversion_plan.LIBRARY_INTRINSICS). An internal procedure cannot be called from a
driver: give --via HOST and test it through the procedure that contains it.

Exit status: 0 passed, 1 different, 2 nothing was established (not a pass).
"""

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conversion_plan as cp  # noqa: E402
import verification_evidence as ve  # noqa: E402

CHECKER_VERSION = "1"
TIMEOUT = 600


def run_timeout() -> Optional[float]:
    """Seconds allowed for one run: $FB_TIMEOUT if set, 0 meaning no limit, otherwise TIMEOUT."""
    value = float(os.environ.get("FB_TIMEOUT", TIMEOUT))
    return value if value > 0 else None
EPSILON = {"float64": 2.0 ** -52, "float32": 2.0 ** -23}


class UnitError(Exception):
    """Nothing was established. Exit 2: this is not a pass."""


def without_program(text: str) -> str:
    """The source without its PROGRAM unit: the driver is the main program now."""
    lines, out, skipping = text.splitlines(), [], False
    for line in lines:
        if not skipping and re.match(r"^\s*program\s+\w+", line, re.IGNORECASE):
            skipping = True
            continue
        if skipping:
            if re.match(r"^\s*end\s*(?:program\b.*)?$", line, re.IGNORECASE):
                skipping = False
            continue
        out.append(line)
    if skipping:
        raise UnitError("a PROGRAM unit has no END")
    return "\n".join(out) + "\n"


DEFAULT_FFLAGS = "-O0 -fwrapv"


def build_options() -> Dict[str, List[str]]:
    """Compiler flags and libraries for the Fortran side: --fflags / --libs (or $FB_FFLAGS / $FB_LIBS), default -O0 -fwrapv and none.
    A reference that is to match a project's own build (its optimisation level, its LAPACK and BLAS) must be built the same way."""
    return {"fflags": shlex.split(os.environ.get("FB_FFLAGS", DEFAULT_FFLAGS)), "libs": shlex.split(os.environ.get("FB_LIBS", ""))}


def add_build_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fflags", help=f"gfortran flags for the Fortran side (default '{DEFAULT_FFLAGS}'); also $FB_FFLAGS")
    parser.add_argument("--libs", help="libraries linked after the sources, for example '-llapack -lblas' (default none); also $FB_LIBS")


def apply_build_options(args: argparse.Namespace) -> None:
    if getattr(args, "fflags", None) is not None:
        os.environ["FB_FFLAGS"] = args.fflags
    if getattr(args, "libs", None) is not None:
        os.environ["FB_LIBS"] = args.libs


def cached_objects(root: Path, copies: Sequence[str], options: Dict[str, List[str]]) -> "tuple[List[str], Path]":
    """With $FB_BUILD_CACHE the sources are compiled once per (flags, content), file by file in the plan's order as a Makefile does, and only the
    driver is compiled afterwards. Returns the object files and the directory holding the .mod files."""
    digest = hashlib.sha256()
    digest.update(" ".join(options["fflags"]).encode())
    for copy in copies:
        digest.update(Path(copy).name.encode())
        digest.update(Path(copy).read_bytes())
    directory = root / digest.hexdigest()[:24]
    marker = directory / "complete"
    if not marker.is_file():
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        for copy in copies:
            done = subprocess.run(["gfortran", *options["fflags"], "-w", "-c", copy, "-o", str(directory / (Path(copy).stem + ".o"))], cwd=directory,
                                  capture_output=True, text=True)
            if done.returncode != 0:
                raise UnitError(f"{Path(copy).name} does not build: {done.stderr.strip()[:400]}")
        marker.write_text("ok\n")
    return [str(directory / (Path(copy).stem + ".o")) for copy in copies], directory


def run_fortran(sources: Sequence[Path], driver: Path, work: Path, stdin: bytes, expected_exit: int = 0) -> bytes:
    build = work / "fortran"
    build.mkdir()
    copies = []
    for source in sources:
        copy = build / source.name
        copy.write_text(without_program(source.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        copies.append(str(copy))
    options = build_options()
    cache_root = os.environ.get("FB_BUILD_CACHE")
    if cache_root:
        objects, include = cached_objects(Path(cache_root), copies, options)
        done = subprocess.run(["gfortran", *options["fflags"], "-w", "-I", str(include), str(driver.resolve()), *objects, *options["libs"], "-o", "driver"],
                              cwd=build, capture_output=True, text=True)
    else:
        done = subprocess.run(["gfortran", *options["fflags"], "-w", *copies, str(driver.resolve()), *options["libs"], "-o", "driver"], cwd=build,
                              capture_output=True, text=True)
    if done.returncode != 0:
        raise UnitError(f"the Fortran driver does not build: {done.stderr.strip()[:400]}")
    out = build / "fb_unit.out"
    run = subprocess.run([str(build / "driver")], cwd=build, input=stdin, capture_output=True, timeout=run_timeout(), env={**os.environ, "FB_UNIT_OUT": str(out)})
    if run.returncode != expected_exit:
        raise UnitError(f"the Fortran driver exited {run.returncode}, expected {expected_exit}: {run.stderr.decode(errors='replace')[-300:]}")
    if not out.is_file() or not out.stat().st_size:
        raise UnitError("the Fortran driver wrote nothing to $FB_UNIT_OUT")
    return out.read_bytes()


def run_target(command: str, work: Path, stdin: bytes) -> bytes:
    out = work / "target.out"
    run = subprocess.run(shlex.split(command), input=stdin, capture_output=True, timeout=run_timeout(), env={**os.environ, "FB_UNIT_OUT": str(out)})
    if run.returncode != 0:
        raise UnitError(f"the target test failed: {run.stderr.decode(errors='replace')[-300:]}")
    if not out.is_file() or not out.stat().st_size:
        raise UnitError("the target test wrote nothing to $FB_UNIT_OUT")
    return out.read_bytes()


def within_machine_precision(fortran: bytes, target: bytes, dtype: str) -> Optional[str]:
    """None when every value is within eps and 2 ULP, else the first that is not."""
    left, right = np.frombuffer(fortran, dtype=dtype), np.frombuffer(target, dtype=dtype)
    bits = 32 if dtype == "float32" else 64
    for index, (a, b) in enumerate(zip(left.tolist(), right.tolist())):
        if a == b and not ve.zero_signbit_differs(a, b):
            continue
        if a != a and b != b:
            continue
        if ve.zero_signbit_differs(a, b) or abs(a - b) > EPSILON[dtype] * abs(a) or ve.ulp_distance(a, b, bits=bits) > 2:
            return f"value {index + 1}: {a!r} and {b!r}"
    return None


def compare(plan: Dict[str, Any], entry: Dict[str, Any], fortran: bytes, target: bytes, machine: Optional[str]) -> Dict[str, Any]:
    outcome: Dict[str, Any] = {"bytes": len(fortran), "mode": "byte", "difference": None}
    if len(fortran) != len(target):
        outcome["difference"] = f"{len(fortran)} bytes and {len(target)} bytes"
    elif fortran != target:
        at = next(k for k, (a, b) in enumerate(zip(fortran, target)) if a != b)
        outcome["difference"] = f"first different byte at offset {at}"
        if machine:
            outcome["mode"] = "machine"
            outcome["difference"] = within_machine_precision(fortran, target, machine)
    return outcome


def earlier_incomplete(plan: Dict[str, Any], entry: Dict[str, Any], log: Path) -> List[str]:
    """Functions ahead of `entry` in the plan that have not passed both their unit test and the regression after them."""
    records, _ = ve.read_log(log)
    latest = {record.get("name"): record for record in records}
    waiting = []
    for other in plan["functions"]:
        if other["order"] >= entry["order"] or other["kind"] in ("program", "module"):
            continue
        for name in (f"unit:{other['name']}", f"regression:{other['name']}"):
            record = latest.get(name)
            if record is None or record.get("exit_status") != 0:
                waiting.append(other["name"])
                break
    return waiting


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", default=".verification/conversion_plan.json")
    parser.add_argument("--function", required=True, help="the procedure, as the plan names it")
    parser.add_argument("--fortran-driver", required=True, help="a PROGRAM that calls the original")
    parser.add_argument("--target-test", required=True, help="the file that calls the translation")
    parser.add_argument("--target-command", required=True, help="how to run it, for example 'python3 test_kick.py'")
    parser.add_argument("--translated", nargs="+", required=True, help="the translated files under test")
    parser.add_argument("--inputs", help="file given to both sides on stdin")
    parser.add_argument("--expected-exit", type=int, default=0, help="exit status the driver must end with, for a procedure that stops the program (default 0)")
    parser.add_argument("--via", help="the host of an internal procedure, which the driver calls instead")
    parser.add_argument("--machine-precision", choices=sorted(EPSILON), help="values of this type within eps: only for a function the plan marks as reaching a library")
    add_build_options(parser)
    ve.add_cli_options(parser)
    args = parser.parse_args(argv)
    apply_build_options(args)
    try:
        plan = cp.load_plan(Path(args.plan))
        stale = cp.stale_sources(plan)
        if stale:
            raise UnitError(f"the sources changed since the plan was made: {', '.join(stale)}. Make the plan again.")
        problem = cp.approval_problem()
        if problem:
            raise UnitError(problem)
        import regression_test as rt
        problem = rt.golden_problem(plan)
        if problem:
            raise UnitError(problem)
        entry = next((e for e in plan["functions"] if e["name"] == args.function.lower()), None)
        if entry is None:
            raise UnitError(f"{args.function} is not in the plan")
        if entry["kind"] == "program":
            raise UnitError(f"{args.function} is the program: the regression test covers it")
        if entry["kind"] == "module":
            raise UnitError(f"{args.function} is a module: test its functions")
        waiting = earlier_incomplete(plan, entry, ve.log_path(args.evidence_dir))
        if waiting:
            raise UnitError(f"convert in plan order: {', '.join(waiting)} must pass their unit test and their regression first")
        called = (args.via or args.function).lower()
        if args.via and not any(e["name"] == args.via.lower() and args.function.lower() in e["calls"] for e in plan["functions"]):
            raise UnitError(f"the plan does not show {args.via} calling {args.function}")
        for path, label in ((args.fortran_driver, "the driver"), (args.target_test, "the target test")):
            if not re.search(rf"\b{re.escape(called)}\b", Path(path).read_text(encoding="utf-8", errors="replace"), re.IGNORECASE):
                raise UnitError(f"{label} does not call {called}")
        if args.machine_precision and not entry["library"]:
            raise UnitError(f"{args.function} reaches no Fortran-specific library, so it has to be byte identical: "
                            "machine precision is refused. If the bytes differ, investigate the translation.")
        stdin = Path(args.inputs).read_bytes() if args.inputs else b""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            fortran = run_fortran([Path(name) for name in plan["sources"]], Path(args.fortran_driver), work, stdin, args.expected_exit)
            target = run_target(args.target_command, work, stdin)
        outcome = compare(plan, entry, fortran, target, args.machine_precision)
    except (UnitError, cp.PlanError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"unit_compare: nothing was established: {exc}", file=sys.stderr)
        return 2
    passed = outcome["difference"] is None
    print(f"{args.function}: {outcome['bytes']} bytes, " + ("identical" if passed and outcome["mode"] == "byte"
          else "within machine precision (library)" if passed else f"DIFFERENT: {outcome['difference']}"))
    subjects = list(plan["sources"]) + list(args.translated) + [args.fortran_driver, args.target_test] + ([args.inputs] if args.inputs else [])
    evidence = ve.record_cli(args, tool="unit_compare", plugin="fortranbridge", kind="unit", name=f"unit:{args.function.lower()}",
                             key=f"unit:{args.function.lower()}", passed=passed, tolerance={"mode": outcome["mode"]}, subject_paths=subjects,
                             detail={"checker_version": CHECKER_VERSION, "function": args.function.lower(), "mode": outcome["mode"],
                                     "bytes": outcome["bytes"], "via": args.via, "difference": outcome["difference"], **build_options()})
    if evidence and evidence.get("error"):
        print(evidence["error"], file=sys.stderr)
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
