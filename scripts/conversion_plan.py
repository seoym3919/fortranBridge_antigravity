#!/usr/bin/env python3
"""The plan a Fortran codebase conversion starts from: every procedure and module, how they link, the order to convert them.

A conversion goes function by function, and nothing can be missed only if the list is complete before the first line is
translated. So this comes first. It writes FORTRAN_FUNCTION_LIST.md and PLAN.md next to the project, and nothing converts or
tests until a human has approved that PLAN.md (see plan_approval.py).

  inventory    every procedure and module, from gfortran's own parse tree (a text search misses internal and module procedures)
  links        who calls whom: what a profiled run did (gprof), plus what the source says (a branch no input took is still a link)
  order        callees before callers, so that each function is converted and tested on top of tested ones
  library      which procedures reach a library whose results Fortran and the target need not share to the last bit. Only those
               may pass a test within machine precision; every other function has to be byte identical

The plan binds the sources by hash. Exit status: 0 written, 2 nothing was established (not a pass).
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identity_checker as ic  # noqa: E402
import verification_evidence as ve  # noqa: E402

SCHEMA = "fortranbridge-conversion-plan/1"
TIMEOUT = 600
LARGE_SCOPE = 25                            # units from which PLAN.md says so in its first lines; every plan is approved either way
MINIMUM_FINAL_CASES = 11                    # "more than 10 different cases"
SOURCE_SUFFIXES = {".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08"}
# Intrinsics that gfortran hands to libm or libgfortran, where a target's own library may round differently. sqrt and abs are
# not here: IEEE 754 fixes sqrt to the last bit, so a difference there is a bug, not a library.
LIBRARY_INTRINSICS = {"sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh", "exp", "log", "log10", "gamma",
                      "lgamma", "erf", "erfc", "bessel_j0", "bessel_j1", "bessel_y0", "bessel_y1", "hypot", "matmul", "pow"}


class PlanError(Exception):
    """Nothing was established. Exit 2: this is not a pass."""


def sources_under(paths: Sequence[str]) -> List[Path]:
    found: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in SOURCE_SUFFIXES))
        elif path.is_file():
            found.append(path)
        else:
            raise PlanError(f"no such source: {raw}")
    if not found:
        raise PlanError("no Fortran sources were found")
    return found


def _called(node: Any, out: Set[str], proc: ic.Proc) -> None:
    """Every name called anywhere inside an event. REAL ** REAL is libm's pow; REAL ** INTEGER is repeated multiplication."""
    if isinstance(node, tuple):
        if len(node) >= 2 and node[0] == "call" and isinstance(node[1], str):
            out.add(node[1].lower())
        if len(node) > 3 and node[0] == "bin" and node[1] == "**" and (ic.kind_of(node[3], proc) or "").startswith("r"):
            out.add("pow")
        for item in node:
            _called(item, out, proc)


def static_links(procs: Dict[str, ic.Proc]) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """(procedure -> procedures it calls, procedure -> library intrinsics it calls), from the source."""
    names = {name.lower() for name in procs}
    calls: Dict[str, Set[str]] = {}
    library: Dict[str, Set[str]] = {}
    for name, proc in procs.items():
        used: Set[str] = set()
        _called(tuple(ic.fortran_events(proc)), used, proc)
        key = name.lower()
        calls[key] = {u for u in used if u in names and u != key}
        for module in list(proc.uses) + ([proc.host] if proc.host else []):     # a unit is converted after the modules it stands on
            if module.lower() in names and module.lower() != key and procs[module].declares.get(module) != "subroutine":
                calls[key].add(module.lower())
        library[key] = {u for u in used if u in LIBRARY_INTRINSICS and u not in names}
    return calls, library


def _procedure_name(symbol: str) -> str:
    """gprof's symbol to the Fortran name: poly_, __geom_MOD_energy, add.3, MAIN__."""
    text = symbol.split(".")[0]
    match = re.match(r"^__\w+?_MOD_(.+)$", text)
    if match:
        text = match.group(1)
    return text.rstrip("_").lower()


def parse_gprof(text: str) -> Dict[Tuple[str, str], int]:
    """(caller, callee) -> calls, from the call graph of `gprof -b -q`."""
    edges: Dict[Tuple[str, str], int] = {}
    entry = re.compile(r"^\s*(?:[\d.]+\s+[\d.]+\s+)?(\d+)(?:/\d+)?(?:\+\d+)?\s+(\S+)\s+\[\d+\]\s*$")
    primary = re.compile(r"^\[\d+\]\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+(?:\d+(?:\+\d+)?\s+)?(\S+)\s+\[\d+\]\s*$")
    block: List[str] = []
    for line in text.splitlines() + ["-" * 20]:
        if not line.startswith("-----"):
            block.append(line)
            continue
        at = next((k for k, row in enumerate(block) if primary.match(row)), None)
        if at is not None:
            callee = _procedure_name(primary.match(block[at]).group(1))
            for row in block[at + 1:]:
                match = entry.match(row)
                if match:
                    target = _procedure_name(match.group(2))
                    edges[(callee, target)] = edges.get((callee, target), 0) + int(match.group(1))
        block = []
    return edges


def profile(sources: Sequence[Path], extra: Sequence[Path], inputs: Sequence[Optional[bytes]]) -> Dict[Tuple[str, str], int]:
    """Run the program under gprof, once per input, and sum the call graph."""
    if not shutil.which("gprof"):
        raise PlanError("gprof is not available: install binutils")
    edges: Dict[Tuple[str, str], int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        files = [str(Path(p).resolve()) for p in list(sources) + list(extra)]
        built = subprocess.run(["gfortran", "-pg", "-O0", "-fno-inline", "-fwrapv", "-w", *files, "-o", "profiled"], cwd=work,
                               capture_output=True, text=True)
        if built.returncode != 0:
            raise PlanError(f"the program could not be built for profiling: {built.stderr.strip()[:400]}")
        for stdin in inputs:
            done = subprocess.run([str(work / "profiled")], cwd=work, input=stdin if stdin is not None else b"", capture_output=True,
                                  timeout=TIMEOUT)
            if done.returncode != 0:
                raise PlanError(f"the profiled run failed: {done.stderr.decode(errors='replace')[-300:]}")
            report = subprocess.run(["gprof", "-b", "-q", "profiled", "gmon.out"], cwd=work, capture_output=True, text=True)
            if report.returncode != 0:
                raise PlanError(f"gprof failed: {report.stderr.strip()[:300]}")
            for edge, count in parse_gprof(report.stdout).items():
                edges[edge] = edges.get(edge, 0) + count
    return edges


def conversion_order(names: Sequence[str], calls: Dict[str, Set[str]]) -> List[List[str]]:
    """Groups of procedures, callees first. Procedures that call each other share a group and are converted together."""
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: Set[str] = set()
    groups: List[List[str]] = []

    def visit(name: str) -> None:
        index[name] = low[name] = len(index)
        stack.append(name)
        on_stack.add(name)
        for callee in sorted(calls.get(name, ())):
            if callee not in index:
                visit(callee)
                low[name] = min(low[name], low[callee])
            elif callee in on_stack:
                low[name] = min(low[name], index[callee])
        if low[name] == index[name]:
            group = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                group.append(member)
                if member == name:
                    break
            groups.append(sorted(group))

    for name in sorted(names):
        if name not in index:
            visit(name)
    return groups


def reaching_library(calls: Dict[str, Set[str]], library: Dict[str, Set[str]]) -> Dict[str, List[str]]:
    """Procedure -> the library intrinsics it reaches, directly or through what it calls."""
    reached = {name: set(found) for name, found in library.items()}
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            for callee in callees:
                extra = reached.get(callee, set()) - reached.setdefault(name, set())
                if extra:
                    reached[name] |= extra
                    changed = True
    return {name: sorted(found) for name, found in reached.items() if found}


def build_plan(sources: Sequence[Path], target: str, stdin_paths: Sequence[str], driver: Optional[Path]) -> Dict[str, Any]:
    procs = {name: proc for name, proc in ic.parse_dump(ic.dump_fortran(sources)).items() if not name.startswith("__")}
    calls, library = static_links(procs)
    has_program = any(proc.is_main for proc in procs.values())
    if not has_program and driver is None:
        raise PlanError("no PROGRAM to profile: give --profile-driver, a program that exercises these procedures")
    inputs: List[Optional[bytes]] = [Path(p).read_bytes() for p in stdin_paths] or [None]
    edges = profile(sources, [driver] if driver else [], inputs)
    program = next((name.lower() for name, proc in procs.items() if proc.is_main), None)
    entries: List[Dict[str, Any]] = []
    known = set(calls)
    for caller, callee in edges:                # a link the source does not show (a pointer, a dummy procedure) counts too
        caller = program if caller == "main" and program else caller
        if caller in known and callee in known and caller != callee:
            calls[caller].add(callee)
    executed = {name for edge in edges for name in edge}
    units = list(procs)
    groups = conversion_order([u.lower() for u in units], calls)
    reach = reaching_library(calls, library)
    files = _defined_in(sources, units)
    for position, group in enumerate(groups):
        for name in group:
            proc = next(p for n, p in procs.items() if n.lower() == name)
            entries.append({"name": name, "order": position, "kind": _unit_kind(proc, procs), "host": (proc.host or "").lower() or None,
                            "file": files.get(name), "calls": sorted(calls.get(name, ())), "profiled": name in executed or proc.is_main,
                            "library": reach.get(name, []), "group": group})
    return {"schema": SCHEMA, "target": target, "sources": {str(p): ve.sha256_file(p) for p in sources}, "functions": entries,
            "profile": [{"caller": a, "callee": b, "calls": c} for (a, b), c in sorted(edges.items())]}


def _unit_kind(proc: ic.Proc, procs: Dict[str, ic.Proc]) -> str:
    if proc.is_main:
        return "program"
    if not proc.code and not proc.host and proc.declares.get(proc.name) == "module":
        return "module"
    if proc.host and procs.get(proc.host) is not None and procs[proc.host].declares.get(proc.host) != "module":
        return "internal"
    for owner in (proc, procs.get(proc.host or "")):
        kind = owner.declares.get(proc.name) if owner else None
        if kind in ("subroutine", "function"):
            return kind
    return "module" if not proc.code else "procedure"


def _defined_in(sources: Sequence[Path], units: Sequence[str]) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        for unit in units:
            if re.search(rf"(?im)^[ \t\d]*(?:[\w()*=,: ]*\s)?(?:subroutine|function|module|program)\s+{re.escape(unit)}\b", text):
                found.setdefault(unit.lower(), str(path))
    return found


def function_list(plan: Dict[str, Any]) -> str:
    """FORTRAN_FUNCTION_LIST.md: the complete inventory."""
    lines = ["# Fortran functions and modules", "", f"{len(plan['functions'])} units, from gfortran's parse tree.", "",
             "| Unit | Kind | Defined in | Inside | Calls or uses | Reached by the profiled run |", "| --- | --- | --- | --- | --- | --- |"]
    for entry in sorted(plan["functions"], key=lambda e: (e["file"] or "", e["name"])):
        lines.append(f"| {entry['name']} | {entry['kind']} | {entry['file'] or '?'} | {entry['host'] or '-'} | "
                     f"{', '.join(entry['calls']) or '-'} | {'yes' if entry['profiled'] else 'NO'} |")
    return "\n".join(lines) + "\n"


def markdown(plan: Dict[str, Any]) -> str:
    """PLAN.md. A human approves exactly this text: any later change voids the approval."""
    large = len(plan["functions"]) >= LARGE_SCOPE
    lines = ["# Fortran conversion plan", "", f"Target: {plan['target']}. {len(plan['functions'])} functions and modules.", ""]
    if large:
        lines += ["**Large scope: review this plan with care before approving it.**", ""]
    lines += ["## Conversion order", "", "Callees first. Units in the same step call each other and are converted together.", "",
              "| Step | Unit | Kind | Calls or uses | Reached by the profiled run | Library (machine precision allowed) |",
              "| --- | --- | --- | --- | --- | --- |"]
    for entry in plan["functions"]:
        lines.append(f"| {entry['order'] + 1} | {entry['name']} | {entry['kind']} | {', '.join(entry['calls']) or '-'} | "
                     f"{'yes' if entry['profiled'] else 'NO'} | {', '.join(entry['library']) or 'none: byte identical'} |")
    unreached = [e["name"] for e in plan["functions"] if not e["profiled"] and e["kind"] != "module"]
    if unreached:
        lines += ["", f"Not reached by the profiled run: {', '.join(unreached)}. Their links come from the source alone, and the golden data "
                  "must reach them."]
    lines += ["", "## Rules", "",
              "1. Conversion is per function and per module, in the order above.",
              "2. Each converted function has a unit test between the Fortran and the target language (`unit_compare.py`).",
              "3. A unit test passes only when the results are byte identical. Machine precision passes only where the table above shows a "
              "Fortran-specific library, because byte identity is not guaranteed there.",
              "4. Golden data covers nominal and corner cases, made from the Fortran before conversion starts (`regression_test.py record`).",
              "5. Each function conversion is followed by at least one regression test (`regression_test.py run --after FUNCTION`).",
              f"6. After the conversion the final regression runs at least {MINIMUM_FINAL_CASES} different cases (`regression_test.py run --final`), "
              "under rule 3.", "",
              "## Golden data", "",
              "Nominal cases follow normal use. Corner cases reach the boundaries: empty or single-element input, zero, negative and very "
              "large or small values, the first and last iteration, every branch the profiled run did not take.", "", "| Case | Class | Covers | Input |",
              "| --- | --- | --- | --- |", "| (to be filled in before approval) | nominal or corner | function or program | file |", "",
              "## Approval", "", "No conversion begins until a human approves this exact file."]
    return "\n".join(lines) + "\n"


def plan_code(text: bytes) -> str:
    """What a human types to approve this PLAN.md."""
    return hashlib.sha256(text).hexdigest()[:8]


def approval_problem(root: Path = Path(".")) -> Optional[str]:
    """None if a human approved the current PLAN.md under `root`, else why not."""
    plan, approval = root / "PLAN.md", root / ".verification" / "plan_approval.json"
    if not plan.is_file():
        return "there is no PLAN.md: run conversion_plan.py first"
    try:
        recorded = json.loads(approval.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"PLAN.md is not approved: a human has to type APPROVE PLAN {plan_code(plan.read_bytes())}"
    if recorded.get("sha256") != ve.sha256_file(plan):
        return f"PLAN.md changed since it was approved: a human has to type APPROVE PLAN {plan_code(plan.read_bytes())}"
    return None


def load_plan(path: Path) -> Dict[str, Any]:
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"cannot read the plan {path}: {exc}") from exc
    if plan.get("schema") != SCHEMA:
        raise PlanError(f"{path} is not a conversion plan")
    return plan


def stale_sources(plan: Dict[str, Any], base: Path = Path(".")) -> List[str]:
    """Sources that changed since the plan was made: the list of functions may no longer be right."""
    return [name for name, digest in plan["sources"].items() if ve.sha256_file(base / name) != digest]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("original", nargs="+", help="Fortran source files or directories")
    parser.add_argument("--target", choices=("python", "cpp"), required=True)
    parser.add_argument("--out", default=".verification/conversion_plan.json", help="plan file (a .md view is written beside it)")
    parser.add_argument("--stdin", nargs="*", default=[], help="input files; the program is profiled once for each")
    parser.add_argument("--profile-driver", help="a PROGRAM to profile with, when the sources are a library")
    parser.add_argument("--replace-plan", action="store_true", help="overwrite an existing PLAN.md, which voids its approval")
    parser.add_argument("--approval-code", action="store_true", help="print the code that approves the current PLAN.md, and stop")
    ve.add_cli_options(parser)
    args = parser.parse_args(argv)
    out = Path(args.out)
    if args.approval_code:
        print(plan_code((out.resolve().parent.parent / "PLAN.md").read_bytes()))
        return 0
    try:
        sources = sources_under(args.original)
        plan = build_plan(sources, args.target, args.stdin, Path(args.profile_driver) if args.profile_driver else None)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        root = out.resolve().parent.parent
        (root / "FORTRAN_FUNCTION_LIST.md").write_text(function_list(plan), encoding="utf-8")
        if not (root / "PLAN.md").is_file() or args.replace_plan:
            (root / "PLAN.md").write_text(markdown(plan), encoding="utf-8")
    except (PlanError, ic.Unchecked, OSError, subprocess.TimeoutExpired) as exc:
        print(f"conversion_plan: nothing was established: {exc}", file=sys.stderr)
        return 2
    text = (root / "PLAN.md").read_bytes()
    print(text.decode("utf-8"))
    print(f"Wrote FORTRAN_FUNCTION_LIST.md and PLAN.md. Fill in the golden data table, then have a human review PLAN.md. "
          f"No conversion begins until a human types, as their own message: APPROVE PLAN {plan_code(text)}")
    print("(editing PLAN.md changes that code: run this again, or read it with --approval-code)")
    evidence = ve.record_cli(args, tool="conversion_plan", plugin="fortranbridge", kind="plan", name="conversion_plan", key="plan", passed=True,
                             tolerance={}, subject_paths=[str(p) for p in sources] + [str(out), str(root / "FORTRAN_FUNCTION_LIST.md")],
                             detail={"functions": [e["name"] for e in plan["functions"]], "plan": str(out),
                                     "programs": [e["name"] for e in plan["functions"] if e["kind"] == "program"],
                                     "modules": [e["name"] for e in plan["functions"] if e["kind"] == "module"],
                                     "library": {e["name"]: e["library"] for e in plan["functions"] if e["library"]}})
    if evidence and evidence.get("error"):
        print(evidence["error"], file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
