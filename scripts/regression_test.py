#!/usr/bin/env python3
"""Golden data and regression tests: the Fortran's results stored before conversion, the translation held to them afterwards.

  record             runs each case on the Fortran and stores the bytes. This happens before conversion, so no change to the
                     translation can redefine the expected answer. Every function of the plan must be covered, the cases must
                     include nominal and corner ones, and at least 11 distinct cases must exercise the whole program.
  run --after NAME   the regression after converting NAME: every golden case of the functions converted so far (at least one).
  run --final        the final regression: every case.

golden/cases.json:
    {"cases": [{"name": "zero_step", "class": "corner", "covers": ["kick"] or "program", "fortran_driver": "golden/drive.f90",
                "target_test": "golden/test_port.py", "target_command": "python3 golden/test_port.py", "stdin": "golden/zero.in"}]}

Both sides write raw little-endian results to $FB_UNIT_OUT, as in unit_compare.py, with the case name in $FB_CASE. A case passes
only when the bytes are identical. Machine precision (--machine-precision float64) is refused unless the plan shows that what the
case covers reaches a Fortran-specific library.

Exit status: 0 passed, 1 different, 2 nothing was established (not a pass).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conversion_plan as cp  # noqa: E402
import unit_compare as uc  # noqa: E402
import verification_evidence as ve  # noqa: E402

CASES = Path("golden/cases.json")
MANIFEST = Path("golden/manifest.json")
DATA = Path("golden/data")


def load_cases(path: Path = CASES) -> List[Dict[str, Any]]:
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    except (OSError, ValueError, KeyError) as exc:
        raise uc.UnitError(f"cannot read {path}: {exc}") from exc
    for case in cases:
        missing = {"name", "class", "covers", "fortran_driver", "target_test", "target_command"} - set(case)
        if missing:
            raise uc.UnitError(f"case {case.get('name', '?')} lacks {sorted(missing)}")
        if case["class"] not in ("nominal", "corner"):
            raise uc.UnitError(f"case {case['name']}: class must be nominal or corner")
    return cases


def required(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The functions that are tested one by one: not the program, which the final regression covers, and not a module's namespace."""
    return [entry for entry in plan["functions"] if entry["kind"] not in ("program", "module")]


def validate(plan: Dict[str, Any], cases: Sequence[Dict[str, Any]]) -> None:
    names = [case["name"] for case in cases]
    if len(set(names)) != len(names):
        raise uc.UnitError("two golden cases share a name")
    identities = {(case["fortran_driver"], ve.sha256_file(Path(case["stdin"])) if case.get("stdin") else None) for case in cases
                  if case["covers"] == "program"}
    if len(identities) < cp.MINIMUM_FINAL_CASES:
        raise uc.UnitError(f"{len(identities)} distinct whole-program cases: at least {cp.MINIMUM_FINAL_CASES} are needed")
    classes = {case["class"] for case in cases}
    if classes != {"nominal", "corner"}:
        raise uc.UnitError(f"the golden data needs nominal and corner cases, and has only {sorted(classes)}")
    covered = {name.lower() for case in cases if case["covers"] != "program" for name in case["covers"]}
    missing = [entry["name"] for entry in required(plan) if entry["name"] not in covered]
    if missing:
        raise uc.UnitError(f"no golden case covers: {', '.join(missing)}")


def library_allowed(plan: Dict[str, Any], case: Dict[str, Any]) -> bool:
    entries = {entry["name"]: entry for entry in plan["functions"]}
    names = [e["name"] for e in plan["functions"] if e["kind"] == "program"] if case["covers"] == "program" else case["covers"]
    return any(entries.get(name.lower(), {}).get("library") for name in names)


def checked_plan(path: Path) -> Dict[str, Any]:
    plan = cp.load_plan(path)
    stale = cp.stale_sources(plan)
    if stale:
        raise uc.UnitError(f"the sources changed since the plan was made: {', '.join(stale)}. Make the plan again.")
    problem = cp.approval_problem()
    if problem:
        raise uc.UnitError(problem)
    return plan


def golden_problem(plan: Dict[str, Any]) -> Optional[str]:
    """None if the golden data matches the plan's sources and its own files."""
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "there is no golden data: run regression_test.py record before converting"
    if manifest.get("sources") != plan["sources"]:
        return "the golden data was recorded from other sources: record it again"
    if manifest.get("build", {"fflags": uc.DEFAULT_FFLAGS.split(), "libs": []}) != uc.build_options():
        return f"the golden data was recorded with other build options ({manifest.get('build')}): pass the same --fflags and --libs or record again"
    for name, digest in manifest.get("files", {}).items():
        if ve.sha256_file(Path(name)) != digest:
            return f"{name} changed since the golden data was recorded"
    return None


def stdin_of(case: Dict[str, Any]) -> bytes:
    return Path(case["stdin"]).read_bytes() if case.get("stdin") else b""


def record(plan: Dict[str, Any], cases: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    validate(plan, cases)
    DATA.mkdir(parents=True, exist_ok=True)
    files: Dict[str, str] = {str(CASES): ve.sha256_file(CASES)}
    sources = [Path(name) for name in plan["sources"]]
    os.environ.pop("FB_CASE", None)
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FB_CASE"] = case["name"]
            data = uc.run_fortran(sources, Path(case["fortran_driver"]), Path(tmp), stdin_of(case), int(case.get("expected_exit", 0)))
        target = DATA / f"{case['name']}.bin"
        target.write_bytes(data)
        for path in filter(None, (str(target), case["fortran_driver"], case.get("stdin"))):
            files[path] = ve.sha256_file(Path(path))
    manifest = {"sources": plan["sources"], "files": files, "cases": len(cases), "build": uc.build_options()}
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def selected(plan: Dict[str, Any], cases: Sequence[Dict[str, Any]], after: Optional[str]) -> List[Dict[str, Any]]:
    if after is None:
        return list(cases)
    entries = {entry["name"]: entry for entry in plan["functions"]}
    if after.lower() not in entries:
        raise uc.UnitError(f"{after} is not in the plan")
    done: Set[str] = {name for name, entry in entries.items() if entry["order"] <= entries[after.lower()]["order"]}
    chosen = [case for case in cases if case["covers"] != "program" and any(name.lower() in done for name in case["covers"])]
    if not any(after.lower() in [name.lower() for name in case["covers"]] for case in chosen):
        raise uc.UnitError(f"no golden case covers {after}")
    return chosen


def run_cases(plan: Dict[str, Any], cases: Sequence[Dict[str, Any]], machine: Optional[str]) -> Dict[str, Any]:
    outcome: Dict[str, Any] = {"passed": 0, "mode": "byte", "failures": [], "classes": sorted({c["class"] for c in cases})}
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FB_CASE"] = case["name"]
            target = uc.run_target(case["target_command"], Path(tmp), stdin_of(case))
        golden = (DATA / f"{case['name']}.bin").read_bytes()
        allowed = machine if machine and library_allowed(plan, case) else None
        result = uc.compare(plan, {}, golden, target, allowed)
        if result["difference"] is None:
            outcome["passed"] += 1
            outcome["mode"] = "machine" if result["mode"] == "machine" else outcome["mode"]
        else:
            outcome["failures"].append(f"{case['name']}: {result['difference']}")
    return outcome


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("record", "run"))
    parser.add_argument("--plan", default=".verification/conversion_plan.json")
    parser.add_argument("--after", help="the regression after converting this function")
    parser.add_argument("--final", action="store_true", help="the final regression, every case")
    parser.add_argument("--translated", nargs="*", default=[], help="the translated files under test")
    parser.add_argument("--machine-precision", choices=sorted(uc.EPSILON), help="only for cases the plan shows reaching a library")
    uc.add_build_options(parser)
    ve.add_cli_options(parser)
    args = parser.parse_args(argv)
    uc.apply_build_options(args)
    try:
        plan = checked_plan(Path(args.plan))
        cases = load_cases()
        if args.action == "record":
            manifest = record(plan, cases)
            print(f"{manifest['cases']} golden cases recorded from the Fortran")
            evidence = ve.record_cli(args, tool="regression_test", plugin="fortranbridge", kind="golden", name="golden", key="golden", passed=True,
                                     tolerance={}, subject_paths=list(plan["sources"]) + [str(MANIFEST)], detail={"cases": manifest["cases"], **uc.build_options()})
            return 2 if evidence and evidence.get("error") else 0
        if bool(args.after) == bool(args.final) or not args.translated:
            raise uc.UnitError("give --translated, and one of --after FUNCTION or --final")
        problem = golden_problem(plan)
        if problem:
            raise uc.UnitError(problem)
        validate(plan, cases)
        chosen = selected(plan, cases, args.after)
        outcome = run_cases(plan, chosen, args.machine_precision)
    except (uc.UnitError, cp.PlanError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"regression_test: nothing was established: {exc}", file=sys.stderr)
        return 2
    passed = not outcome["failures"] and outcome["passed"] == len(chosen)
    for failure in outcome["failures"]:
        print(f"DIFFERENT {failure}")
    print(f"{outcome['passed']} of {len(chosen)} cases passed ({outcome['mode']})")
    name = f"regression:{args.after.lower()}" if args.after else "regression:final"
    subjects = list(plan["sources"]) + list(args.translated) + [str(MANIFEST)] + sorted({case["target_test"] for case in chosen})
    evidence = ve.record_cli(args, tool="regression_test", plugin="fortranbridge", kind="regression", name=name, key=name, passed=passed,
                             tolerance={"mode": outcome["mode"]}, subject_paths=subjects,
                             detail={"cases": len(chosen), "passed": outcome["passed"], "mode": outcome["mode"], "classes": outcome["classes"],
                                     "program_cases": sum(1 for case in chosen if case["covers"] == "program"), "failures": outcome["failures"]})
    if evidence and evidence.get("error"):
        print(evidence["error"], file=sys.stderr)
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
