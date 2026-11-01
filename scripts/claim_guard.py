#!/usr/bin/env python3
"""Stop hook: refuse an unverified "the translation is equivalent / the port is complete" claim.

rules/directives.md items 3 and 4 say Claude may claim numerical equivalence only when translation_verifier.py exited 0.
That is only a request to the model. This hook makes it mechanical: when the assistant's final message of a turn asserts
equivalence of a Fortran translation, the hook looks for a passing, unchanged `compare` record in the evidence log
(./.verification/log.jsonl, written by translation_verifier.py). Without one it blocks the stop and tells the model to run
the verifier. The hook never judges numbers itself; it only checks the log, whose records come from the Python verifier.

Three ways a claim is recognised: free-text patterns (CLAIM), the structured line `VERDICT: equivalent|complete|identical
algorithm|unverified`
(always gated, whatever the surrounding words), and, in a Fortran project (a .verification directory or Fortran files
under the working directory), any matching sentence even without the words "Fortran/translation". "port complete" claims
additionally need a fresh passing `completeness` record, which port_completeness.py writes. The guard remembers the last
log line it saw in .verification/guard_head, so later truncation or editing of the last record is detected too.

Hook input (stdin JSON): last_assistant_message and/or transcript_path, cwd, stop_hook_active.
Output: {"decision": "block", "reason": ...} to block, nothing to allow. Always exits 0 (a hook error must not wedge a session).
`python3 claim_guard.py --text "..." [--cwd DIR]` prints the verdict for manual use; exit 1 when the claim is unsupported.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verification_evidence as ve  # noqa: E402

CLAIM = re.compile(
    r"\b(?:numerically\s+equivalent|mathematically\s+(?:identical|equivalent)|bit(?:wise)?[- ]?(?:for[- ]bit\s+)?identical|"
    r"equivalent\s+to\s+the\s+(?:original|fortran)|(?:matches|reproduces)\s+(?:the\s+)?(?:original\s+)?fortran|"
    r"(?:is|are|now)\s+(?:fully\s+)?equivalent|(?:translation|port)\s+(?:is|was)\s+(?:complete|correct|verified)|"
    r"(?:complete|verified)\s+port|port\s+(?:is\s+)?complete|verified\s+(?:equivalent|against\s+(?:the\s+)?fortran)|"
    r"reproduces?\s+(?:the\s+)?(?:fortran|original)[^.]{0,40}\bexactly|same\s+(?:results|output|values)\s+as\s+(?:the\s+)?(?:original|fortran)|"
    r"agrees?\s+with\s+(?:the\s+)?(?:original|fortran)|identical\s+(?:results|output|values)|"
    r"(?:validated|verified)\s+against\s+(?:the\s+)?original|passes?\s+(?:the\s+)?verification)\b",
    re.IGNORECASE)
COMPLETE = re.compile(r"(?:translation|port)\s+(?:is\s+)?complete|complete\s+port|port\s+is\s+complete|mathematically\s+identical",
                      re.IGNORECASE)
VERDICT = re.compile(r"^[ \t>*_-]*VERDICT:\s*(equivalent|complete|identical algorithm|unverified|not equivalent|incomplete)\b",
                     re.IGNORECASE | re.MULTILINE)
ALGORITHM = "identical algorithm"           # same equations and algorithm, a library differs in the last bits of the results
MACHINE_EPSILON = {"float64": 2.0 ** -52, "float32": 2.0 ** -23}
MACHINE_ULP = 2                             # |test - ref| <= eps*|ref| spans at most two units in the last place
FORTRAN_FILES = (".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08")
CONTEXT = re.compile(r"fortran|translat|\bport(?:ed|s)?\b|\.f(?:90|95|03)?\b|transpil", re.IGNORECASE)
# A sentence that conditions, negates, asks or quotes a rule is not a claim.
HEDGE = re.compile(
    r"\b(?:not|n't|cannot|can't|never|no|unless|only\s+(?:if|when)|if|whether|until|before|must|should|would|could|may|might|"
    r"need(?:s)?|require[sd]?|rule|directive|withheld|fail(?:s|ed)?|differ(?:s|ent)?|unverified|pending|todo|goal|check(?:s|ed)?\s+whether)\b",
    re.IGNORECASE)


def sentences(text: str) -> List[str]:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", " ", text)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def fortran_project(cwd: Path) -> bool:
    """A .verification directory, or Fortran sources within two directory levels of cwd."""
    if (cwd / ".verification").is_dir():
        return True
    depth = len(cwd.parts)
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
        if len(Path(root).parts) - depth >= 2:
            dirs[:] = []
        if any(f.lower().endswith(FORTRAN_FILES) for f in files):
            return True
    return False


def unsupported_claims(text: str, strict: bool = False) -> List[str]:
    """Sentences that assert equivalence/completeness. `strict` drops the need to mention Fortran or translating."""
    found = []
    for sentence in sentences(text):
        if sentence.endswith("?") or not CLAIM.search(sentence):
            continue
        if not strict and not CONTEXT.search(sentence):
            continue
        if HEDGE.search(sentence):
            continue
        found.append(sentence)
    return found


def verdict_claims(text: str) -> List[str]:
    """Levels asserted on a `VERDICT:` line: "equivalent" and/or "complete"."""
    levels = []
    for match in VERDICT.finditer(re.sub(r"```.*?```", " ", text, flags=re.DOTALL)):
        word = match.group(1).lower()
        if word in ("equivalent", "complete", ALGORITHM):
            levels.append(word)
    return levels


def last_assistant_text(payload: Dict[str, Any]) -> str:
    message = payload.get("last_assistant_message")
    if isinstance(message, str) and message.strip():
        return message
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if not transcript:
        return ""
    text = ""
    try:
        for line in Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "PLANNER_RESPONSE" and isinstance(entry.get("content"), str) and entry["content"].strip():
                text = entry["content"]
                continue
            body = entry.get("message")
            if not isinstance(body, dict) or body.get("role") != "assistant":
                continue
            content = body.get("content")
            parts = [content] if isinstance(content, str) else [
                c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
            if any(parts):
                text = "\n".join(p for p in parts if p)
    except OSError:
        return ""
    return text


def sync_head(cwd: Path, records: List[Dict[str, Any]]) -> Optional[str]:
    """Trust-on-first-use check of the log head. Returns a problem string if a line seen before has changed or gone."""
    state = cwd / ".verification" / "guard_head"
    problem = None
    try:
        seen = json.loads(state.read_text())
        count, head = int(seen["count"]), seen["head"]
        if len(records) < count or (count and records[count - 1].get("_line") != head):
            problem = f"the evidence log no longer contains the {count} record(s) seen earlier (truncated or edited)"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if problem is None and records:
        try:
            state.write_text(json.dumps({"count": len(records), "head": records[-1]["_line"]}))
        except OSError:
            pass
    return problem


def _fresh_pass(record: Dict[str, Any], base: Path) -> Optional[str]:
    """None if `record` is a pass whose bound files are unchanged, else why not."""
    if record.get("exit_status") != 0 or record.get("passed") is not True:
        return f"the latest {record.get('kind')} record failed (exit_status {record.get('exit_status')})"
    stale = [s.get("path") for s in record.get("subjects", []) if s.get("sha256") != ve._current_digest(s, base)]
    if stale:
        return f"files changed since the passing {record.get('kind')} record: {', '.join(map(str, stale))}"
    return None


def _bitwise_problem(record: Dict[str, Any]) -> Optional[str]:
    """None if a `compare` record demanded bitwise identity (atol 0, max_ulp 0), else why not. The dtype default rtol is not
    enough: a pass within 1e-12 says the numbers are close, not that the equations and the algorithm are identical."""
    tolerance = record.get("tolerance") or {}
    try:
        atol, max_ulp = float(tolerance["atol"]), float(tolerance["max_ulp"])
    except (KeyError, TypeError, ValueError):
        return "the latest compare record does not state atol and max_ulp"
    if atol != 0.0 or max_ulp != 0.0:
        return f"the latest compare record is not bitwise (atol {atol:g}, max_ulp {max_ulp:g}; both must be 0)"
    return None


def _precision_problem(record: Dict[str, Any]) -> Optional[str]:
    """None if a `compare` record demanded agreement within machine precision: atol 0, rtol at most eps of its dtype."""
    tolerance = record.get("tolerance") or {}
    eps = MACHINE_EPSILON.get((record.get("detail") or {}).get("dtype") or "float64")
    if eps is None:
        return f"the latest compare record has dtype {(record.get('detail') or {}).get('dtype')}, not float32 or float64"
    try:
        rtol, atol, max_ulp = float(tolerance["rtol"]), float(tolerance["atol"]), float(tolerance["max_ulp"])
    except (KeyError, TypeError, ValueError):
        return "the latest compare record does not state rtol, atol and max_ulp"
    if atol != 0.0 or rtol > eps or max_ulp > MACHINE_ULP:
        return (f"the latest compare record is looser than machine precision (rtol {rtol:g}, atol {atol:g}, max_ulp {max_ulp:g}; "
                f"limits rtol {eps:g}, atol 0, max_ulp {MACHINE_ULP})")
    return None


def _binding_problem(compare: Dict[str, Any], completeness: Dict[str, Any]) -> Optional[str]:
    """None if the compare record covers the very files the completeness record checked: each record is fresh on its own,
    but only together do they say something about one translation."""
    detail = completeness.get("detail") or {}
    checked = list(detail.get("originals") or []) + list(detail.get("translated") or [])
    if not checked:
        return "the latest completeness record does not name the files it checked; re-run port_completeness.py"
    bound = {s.get("path"): s.get("sha256") for s in compare.get("subjects", [])}
    wanted = {s.get("path"): s.get("sha256") for s in completeness.get("subjects", [])}
    missing = [path for path in checked if path not in bound or bound[path] != wanted.get(path)]
    if missing:
        return f"the latest compare record does not cover what completeness checked: {', '.join(missing)}"
    return None


def _trace_problem(record: Dict[str, Any], machine_allowed: bool) -> Optional[str]:
    """A trace compared within machine precision does not support a bitwise claim."""
    if (record.get("detail") or {}).get("machine_precision") and not machine_allowed:
        return "the latest trace record was compared within machine precision, not bitwise"
    return None


def _plan_problem(plan: Dict[str, Any], records: List[Dict[str, Any]], base: Path, need_units: bool, need_library: bool) -> Optional[str]:
    """The rules of a conversion. Every claim stands on a human-approved PLAN.md, golden data and a final regression of more than
    10 cases, nominal and corner. Machine precision needs a Fortran-specific library in the plan. A finished port needs a unit
    test and a regression for every function of the plan."""
    detail = plan.get("detail") or {}
    library = detail.get("library") or {}
    latest = {r.get("name"): r for r in records}
    plan_file = base / "PLAN.md"
    approval = latest.get("plan_approval")
    if approval is None or _fresh_pass(approval, base):
        return f"{plan_file} has no fresh human approval"
    if "golden" not in latest or _fresh_pass(latest["golden"], base):
        return "there is no fresh golden data (regression_test.py record)"
    final = latest.get("regression:final")
    if final is None or _fresh_pass(final, base):
        return "no fresh passing final regression (regression_test.py run --final)"
    summary = final.get("detail") or {}
    if int(summary.get("program_cases") or 0) <= 10:
        return f"the final regression ran {summary.get('program_cases')} whole-program cases: more than 10 are needed"
    if set(summary.get("classes") or []) != {"nominal", "corner"}:
        return "the final regression needs nominal and corner cases"
    if summary.get("mode") != "byte" and not library:
        return "the final regression passed within machine precision with no library in the plan"
    if summary.get("mode") != "byte" and not need_library:
        return "the final regression passed within machine precision, which supports only VERDICT: identical algorithm"
    if need_library and not library:
        return "the plan shows no Fortran-specific library, so results must be byte identical: machine precision is not available"
    if not need_units:
        return None
    skipped = set(detail.get("programs") or []) | set(detail.get("modules") or [])
    missing, machine = [], []
    for function in detail.get("functions") or []:
        if function in skipped:
            continue
        unit, regression = latest.get(f"unit:{function}"), latest.get(f"regression:{function}")
        if unit is None or regression is None or _fresh_pass(unit, base) or _fresh_pass(regression, base):
            missing.append(function)
        elif (unit.get("detail") or {}).get("mode") != "byte" and function not in library:
            machine.append(function)
    if missing:
        return f"no fresh passing unit test and regression for: {', '.join(missing)}"
    if machine:
        return f"passed within machine precision with no library in the plan: {', '.join(machine)}"
    return None


def _recheck_identity(record: Dict[str, Any], base: Path) -> Optional[str]:
    """Run the static check again here. The log is a plain file: this hook does not take a passing identity record on trust."""
    detail = record.get("detail") or {}
    originals = [base / path for path in detail.get("originals") or []]
    translated = [base / path for path in detail.get("translated") or []]
    if not originals or not translated:
        return "the latest identity record does not name its files"
    try:
        if all(path.suffix == ".py" for path in translated):
            import identity_checker as ic
            report = ic.check_python(originals, translated)
        else:
            import identity_cpp
            report = identity_cpp.check_cpp(originals, translated)
    except Exception as exc:
        return f"the identity check could not be repeated: {exc}"
    if not report.ok:
        return "the identity check fails when repeated, whatever the log says"
    return None


def evidence_status(cwd: Path, need_complete: bool = False, need_algorithm: bool = False) -> Dict[str, Any]:
    """{"ok": bool, "why": str}. ok only if the log is intact, its latest `compare` record is a fresh bitwise pass and, for
    completeness claims, its latest `completeness` record (port_completeness.py) is too and covers the same files.
    A Python port also needs the `identity` and `trace` records, on the same files. An "identical algorithm" claim stands on
    those, so its compare record may differ in the last bits, within machine precision: a library is the only thing left."""
    log = ve.log_path(str(cwd / ".verification"))
    records, problems = ve.read_log(log)
    if problems:
        return {"ok": False, "why": f"{log} has problems: {problems[0]}"}
    tamper = sync_head(cwd, records)
    if tamper:
        return {"ok": False, "why": tamper}
    base = log.parent.parent
    wanted = [("compare", "fortran_vs_translation", "translation_verifier compare")]
    if need_complete:
        wanted.append(("completeness", "port_completeness", "port_completeness completeness"))
    compare = next((r for r in reversed(records) if r.get("kind") == "compare" and r.get("name") == "fortran_vs_translation"), None)
    wanted.append(("plan", "conversion_plan", "conversion_plan plan"))
    wanted.append(("identity", "fortran_vs_translation", "identity_checker identity"))
    python_port = compare is not None and any(str(s.get("path", "")).endswith(".py") for s in compare.get("subjects", []))
    if python_port:                                 # the trace reads Python only so far
        wanted.append(("trace", "fortran_vs_translation", "trace_checker trace"))
    latest = {}
    for kind, name, label in wanted:
        found = [r for r in records if r.get("kind") == kind and r.get("name") == name]
        if not found:
            return {"ok": False, "why": f"no {label} record in {log}"}
        why = _fresh_pass(found[-1], base)
        if why:
            return {"ok": False, "why": why}
        latest[kind] = found[-1]
    why = _precision_problem(latest["compare"]) if need_algorithm and not need_complete else _bitwise_problem(latest["compare"])
    if not why and need_complete:
        why = _binding_problem(latest["compare"], latest["completeness"])
    if not why:
        why = _binding_problem(latest["compare"], latest["identity"]) or _recheck_identity(latest["identity"], base)
    if not why and python_port:
        why = _binding_problem(latest["compare"], latest["trace"]) or _trace_problem(latest["trace"], need_algorithm and not need_complete)
    if not why:
        why = _plan_problem(latest["plan"], records, base, need_complete, need_algorithm)
    if why:
        return {"ok": False, "why": why}
    return {"ok": True, "why": "fresh passing records"}


def verdict(text: str, cwd: Path) -> Optional[str]:
    levels = verdict_claims(text)
    claims = unsupported_claims(text, strict=fortran_project(cwd))
    if not levels and not claims:
        return None
    need_complete = "complete" in levels or any(COMPLETE.search(c) for c in claims)
    need_algorithm = ALGORITHM in levels
    status = evidence_status(cwd, need_complete, need_algorithm)
    if status["ok"]:
        return None
    shown = f"VERDICT: {levels[0]}" if levels else claims[0][:200]
    how = ("Start from scripts/conversion_plan.py, test every function of the plan with scripts/unit_compare.py, run "
           "scripts/identity_checker.py and, for a Python port, scripts/trace_checker.py on the original and the translation, and "
           "scripts/translation_verifier.py --ref FORTRAN_OUT --test TRANSLATED_OUT --ref-source ORIGINAL "
           "--test-source TRANSLATED --inputs INPUTS --atol 0 --max-ulp 0")
    if need_algorithm:
        how = ("For VERDICT: identical algorithm run scripts/identity_checker.py --original ORIGINAL --translated TRANSLATED, "
               "scripts/trace_checker.py --original ORIGINAL --translated TRANSLATED --machine-precision, and "
               "scripts/translation_verifier.py with --atol 0 --tol EPS --max-ulp 2 (EPS is the machine epsilon of the data). " + how)
    if need_complete:
        how += ", then scripts/port_completeness.py --original ORIGINAL --translated TRANSLATED --target python|cpp --unit-tests TESTS"
    shown = f"VERDICT: {ALGORITHM}" if need_algorithm else shown
    return ("Unverified claim: \"" + shown + "\" but " + status["why"] + ". " + how + ", quote the exit status, and "
            "only claim equivalence/completeness if it is 0; otherwise restate the result as unverified "
            "(VERDICT: unverified). Do not loosen a tolerance to get a pass.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--text", help="check this text instead of reading a hook payload from stdin")
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args(argv)
    try:
        if args.text is not None:
            reason = verdict(args.text, Path(args.cwd or ".").resolve())
            print(reason or "ok")
            return 1 if reason else 0
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("stop_hook_active"):
            return 0
        is_antigravity = "terminationReason" in payload or "fullyIdle" in payload or "transcriptPath" in payload
        workspaces = payload.get("workspacePaths")
        cwd_raw = payload.get("cwd") or (workspaces[0] if isinstance(workspaces, list) and workspaces else ".")
        cwd = Path(cwd_raw).resolve()
        records, problems = ve.read_log(ve.log_path(str(cwd / ".verification")))
        if records and not problems:
            sync_head(cwd, records)
        reason = verdict(last_assistant_text(payload), cwd)
        if reason:
            print(json.dumps({"decision": "continue" if is_antigravity else "block", "reason": reason}))
        elif is_antigravity:
            print("{}")
    except Exception as exc:  # a broken guard must never wedge the session
        print(f"claim_guard: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
