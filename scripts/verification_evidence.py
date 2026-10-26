#!/usr/bin/env python3
r"""
verification_evidence.py - an append-only record of verification runs, shared by the scienceStack, cppBridge,
fortranBridge and theoryExtractor verifiers. Standard library only. The canonical copy lives in scienceStack; the
other plugins ship an identical file.

Every verifier run appends one JSON line to <evidence dir>/log.jsonl (default: ./.verification/log.jsonl; override with
--evidence-dir or $VERIFICATION_EVIDENCE_DIR):

    {"schema": "verification-evidence/1", "tool": ..., "plugin": ..., "kind": "benchmark" | "compare" | "roundtrip",
     "name": ..., "key": <identifies "the same check" across runs>, "passed": bool, "exit_status": int,
     "tolerance": {"tol": 1e-12} | {"rtol": .., "atol": ..}, "subjects": [{"path": ..., "sha256": ...}],
     "flags": [...], "justification": str|null, "detail": {...}, "timestamp": ISO-8601 UTC, "prev": sha256 of the
     previous line}

What this gives, and what it does not:
  * Each record is bound to the SHA-256 of the files the verdict depends on (the solver, or the reference and test
    outputs), so evidence goes stale the moment one of them changes.
  * Lines are hash-chained: editing or deleting a line in the middle is detected by `verify`. Truncating the tail,
    or rewriting the whole file, is not: the only unforgeable gate is a clean re-run in CI.
  * A run whose tolerance is LOOSER than the previous failed run of the same check, on unchanged primary subjects, is
    withheld (exit status 3) unless the caller records a justification. This targets "raise the tolerance until it
    passes". It is a deterministic comparison of numbers and hashes, not a judgement. Withheld runs are never used as
    the "previous run", so repeating the command stays withheld. Extra subjects (--subject) make evidence stale when
    they change but cannot release a withheld verdict; only a change in the primary subjects (the solver, or the
    files compared) or a justification can. A new --evidence-dir starts an empty history, which a local file cannot
    prevent: a clean re-run in CI is the gate.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA = "verification-evidence/1"
# Generated caches are not part of "the files": a test run that writes them must not make its own evidence stale.
CACHE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".tox", ".nox", ".eggs",
               ".verification"}
CACHE_SUFFIXES = (".pyc", ".pyo")
ENV_DIR = "VERIFICATION_EVIDENCE_DIR"
GENESIS = "0" * 64
EXIT_WITHHELD = 3


class EvidenceError(Exception):
    """The evidence log cannot be read or written."""


def evidence_dir(override: Optional[str] = None) -> Path:
    return Path(override or os.environ.get(ENV_DIR) or Path.cwd() / ".verification").resolve()


def log_path(override: Optional[str] = None) -> Path:
    return evidence_dir(override) / "log.jsonl"


def sha256_file(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def subject(path: str, base: Path, role: str = "primary") -> Dict[str, Any]:
    """{"path", "sha256", "role"}: the path relative to `base` (the directory holding .verification) when it lies below
    it. role "primary" = what the verdict is about; "extra" = a dependency named with --subject."""
    resolved = Path(path).resolve()
    try:
        shown = str(resolved.relative_to(base.resolve()))
    except ValueError:
        shown = str(resolved)
    return {"path": shown, "sha256": sha256_file(resolved), "role": role}


def worktree_digest(start: Path) -> Tuple[Path, str, int]:
    """(git top level, SHA-256 over every tracked and untracked-not-ignored file, file count).

    Each entry is "relative path NUL sha256-of-content" (a symbolic link contributes its target, a tracked file that
    was deleted contributes "deleted"), sorted by path, so the digest changes exactly when some file's content, name,
    presence or link target changes. Caches (CACHE_PARTS, *.pyc) and the evidence directory are left out.
    """
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(start), *args], capture_output=True, env=env, timeout=120)
    top = git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise EvidenceError(f"{start} is not inside a git repository, so the work tree cannot be hashed")
    root = Path(top.stdout.decode().strip())
    listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--full-name", ":/")
    if listed.returncode != 0:
        raise EvidenceError("git ls-files failed: " + listed.stderr.decode(errors="replace").strip())
    entries = []
    for rel in sorted({r for r in listed.stdout.decode(errors="surrogateescape").split("\0") if r}):
        parts = rel.split("/")
        if any(part in CACHE_PARTS for part in parts) or rel.endswith(CACHE_SUFFIXES):
            continue
        path = root / rel
        if path.is_symlink():
            digest = "link:" + os.readlink(path)
        elif path.is_file():
            digest = sha256_file(path) or "unreadable"
        else:
            digest = "deleted"
        entries.append(f"{rel}\0{digest}\n")
    return root, hashlib.sha256("".join(entries).encode(errors="surrogateescape")).hexdigest(), len(entries)


def tree_subject(start: Path, base: Path) -> Dict[str, Any]:
    """The git work tree containing `start` as one subject; its path is the top level relative to `base` (the directory
    holding the evidence log), so the log may live outside the project (for example in a CI temp directory)."""
    root, digest, count = worktree_digest(start)
    try:
        shown = os.path.relpath(root, base.resolve())
    except ValueError:
        shown = str(root)
    return {"path": shown, "sha256": digest, "role": "tree", "files": count}


def _current_digest(entry: Dict[str, Any], base: Path) -> Optional[str]:
    path = Path(entry.get("path", ""))
    target = path if path.is_absolute() else base / path
    if entry.get("role") == "tree":
        try:
            return worktree_digest(target)[1]
        except EvidenceError:
            return None
    return sha256_file(target)


def looser(key: str, new: float, old: float) -> bool:
    """Is `new` a weaker requirement than `old` for the tolerance entry `key`? Entries are upper limits (a larger value
    is looser) except keys ending in "_min", which are minimums (a smaller value is looser: repeat_min, order_min)."""
    return new < old if key.endswith("_min") else new > old


def _numbers(tolerance: Dict[str, Any]) -> Dict[str, float]:
    return {k: float(v) for k, v in tolerance.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}


def read_log(log: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(records, problems). A line that is not a JSON object, lacks the schema, or breaks the hash chain is a problem."""
    records: List[Dict[str, Any]] = []
    problems: List[str] = []
    if not log.exists():
        return records, problems
    if log.is_symlink():
        return records, [f"{log.name} is a symbolic link; refusing to read it"]
    previous = GENESIS
    try:
        raw_lines = log.read_bytes().split(b"\n")
    except OSError as exc:
        return records, [f"cannot read {log}: {exc.strerror or exc}"]
    for number, raw in enumerate(raw_lines, 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            problems.append(f"line {number}: not valid JSON")
            previous = hashlib.sha256(raw).hexdigest()
            continue
        if not isinstance(record, dict) or record.get("schema") != SCHEMA:
            problems.append(f"line {number}: not a {SCHEMA} record")
        elif record.get("prev") != previous:
            problems.append(f"line {number}: hash chain broken (a line before it was edited, removed or reordered)")
        previous = hashlib.sha256(raw).hexdigest()
        if isinstance(record, dict):
            records.append(record)
    return records, problems


def append(log: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    """Add schema, timestamp and the chain hash, then append one line under an exclusive lock."""
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.is_symlink() or log.parent.is_symlink():
        raise EvidenceError("the evidence directory or log is a symbolic link; refusing to write to it")
    with open(log, "ab+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        lines = [line for line in handle.read().split(b"\n") if line.strip()]
        previous = hashlib.sha256(lines[-1]).hexdigest() if lines else GENESIS
        full = dict(record, schema=SCHEMA, prev=previous,
                    timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        handle.write((json.dumps(full, sort_keys=True) + "\n").encode())
        handle.flush()
        os.fsync(handle.fileno())
    return full


def assess_tolerance(records: Sequence[Dict[str, Any]], key: str, tolerance: Dict[str, Any],
                     subjects: Sequence[Dict[str, Any]], justification: Optional[str]) -> Dict[str, Any]:
    """Compare this run with the latest earlier run of the same check.

    "loosened": the latest earlier run FAILED and some tolerance is now weaker than it was (larger limit, or for a
                key ending in "_min" a smaller minimum). Withheld runs (exit status
                3) are skipped when looking for that run, so repeating a withheld command is withheld again.
    "unchanged": every primary subject has the same path and SHA-256 as in that earlier run. Extra subjects are not
                 compared: a throwaway file must not be able to release a withheld verdict.
    "withheld": loosened on unchanged subjects without a justification.
    """
    previous = next((r for r in reversed(records)
                     if r.get("key") == key and r.get("exit_status") != EXIT_WITHHELD), None)
    result: Dict[str, Any] = {"loosened": False, "unchanged": None, "withheld": False, "previous": None}
    if previous is None or previous.get("passed") is not False:
        return result
    old, new = _numbers(previous.get("tolerance", {})), _numbers(tolerance)
    result["previous"] = {"tolerance": old, "timestamp": previous.get("timestamp")}
    result["loosened"] = any(looser(k, new[k], old[k]) for k in new if k in old)
    before = {s.get("path"): s.get("sha256") for s in previous.get("subjects", []) if s.get("role") != "extra"}
    now = {s.get("path"): s.get("sha256") for s in subjects if s.get("role") != "extra"}
    result["unchanged"] = bool(now) and now == before and all(v is not None for v in now.values())
    result["withheld"] = result["loosened"] and result["unchanged"] and not (justification or "").strip()
    return result


def record_run(log: Path, *, tool: str, plugin: str, kind: str, name: str, key: str, passed: bool,
               tolerance: Dict[str, Any], subjects: Sequence[Dict[str, Any]], justification: Optional[str] = None,
               detail: Optional[Dict[str, Any]] = None, extra_flags: Sequence[str] = ()) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Assess against the log, append, and return (record, assessment). exit_status is 3 when withheld."""
    records, problems = read_log(log)
    assessment = assess_tolerance(records, key, tolerance, subjects, justification)
    flags = list(extra_flags)
    if assessment["loosened"]:
        flags.append("tolerance_loosened_after_failure")
        flags.append("subjects_unchanged" if assessment["unchanged"] else "subjects_changed")
    if assessment["withheld"]:
        flags.append("withheld")
    if problems:
        flags.append("log_chain_problem")
    exit_status = EXIT_WITHHELD if assessment["withheld"] else (0 if passed else 1)
    record = append(log, {"tool": tool, "plugin": plugin, "kind": kind, "name": name, "key": key, "passed": bool(passed),
                          "exit_status": exit_status, "tolerance": dict(tolerance), "subjects": list(subjects),
                          "flags": flags, "justification": (justification or "").strip() or None,
                          "detail": detail or {}})
    return record, assessment


def withheld_message(assessment: Dict[str, Any]) -> str:
    old = assessment["previous"]["tolerance"]
    return ("verdict withheld: the tolerance was loosened after a failure on unchanged files "
            f"(previous failed run used {old}). Fix the solver, or record why with --justify \"<reason>\". "
            "Rerunning, or naming extra files with --subject, does not release it.")


def fresh(records: Sequence[Dict[str, Any]], path: str, base: Path, name: Optional[str] = None,
          kind: Optional[str] = None) -> Dict[str, Any]:
    """Latest record that lists `path` as a subject (optionally of this name / kind): is it a pass whose subjects (files,
    or the whole work tree for a tree subject) still match? Pass "." (or the project root) for the work tree."""
    resolved = Path(path).resolve()
    if resolved.is_dir():
        try:
            target = os.path.relpath(worktree_digest(resolved)[0], base.resolve())
        except EvidenceError:
            target = subject(path, base)["path"]
    else:
        target = subject(path, base)["path"]
    for record in reversed(records):
        entries = {s.get("path"): s for s in record.get("subjects", [])}
        if target in entries and (name is None or record.get("name") == name) and (kind is None or record.get("kind") == kind):
            stale = [p for p, entry in entries.items() if entry.get("sha256") != _current_digest(entry, base)]
            ok = record.get("passed") is True and record.get("exit_status") == 0 and not stale
            return {"fresh_pass": ok, "record_timestamp": record.get("timestamp"), "name": record.get("name"),
                    "kind": record.get("kind"), "passed": record.get("passed"), "stale_subjects": stale,
                    "flags": record.get("flags", []), "detail": record.get("detail", {})}
    return {"fresh_pass": False, "record_timestamp": None, "reason": "no evidence for this file"}


# --------------------------------------------------------------------------------------------------------------
# What every verifier command line shares
# --------------------------------------------------------------------------------------------------------------

def add_cli_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence-dir", help="directory for the evidence log (default ./.verification)")
    parser.add_argument("--no-evidence", action="store_true", help="do not record this run")
    parser.add_argument("--justify", help="why a tolerance was loosened after a failure (recorded in the log)")
    parser.add_argument("--subject", action="append", help="extra file whose content the verdict depends on (repeatable)")
    parser.add_argument("--check-id", help=argparse.SUPPRESS)


def record_cli(args: argparse.Namespace, *, tool: str, plugin: str, kind: str, name: str, key: str, passed: bool,
               tolerance: Dict[str, Any], subject_paths: Sequence[str], detail: Dict[str, Any],
               tree: bool = False) -> Optional[Dict[str, Any]]:
    """Record the run for a verifier that used add_cli_options. None under --no-evidence; otherwise a summary dict
    ('error' set when the log could not be written; 'withheld' and 'message' when the verdict is withheld).
    tree=True also binds the record to the whole git work tree (see worktree_digest)."""
    if args.no_evidence:
        return None
    log = log_path(args.evidence_dir)
    try:
        base = log.parent.parent
        subjects = [subject(p, base) for p in subject_paths] + [subject(p, base, "extra") for p in (args.subject or [])]
        if tree:
            subjects.insert(0, tree_subject(Path.cwd(), base))
        if args.check_id:
            key = f"manifest:{args.check_id}"
            detail = dict(detail, manifest_id=args.check_id)
        record, assessment = record_run(log, tool=tool, plugin=plugin, kind=kind, name=name, key=key, passed=passed,
                                        tolerance=tolerance, subjects=subjects, justification=args.justify, detail=detail)
    except (EvidenceError, OSError) as exc:
        return {"log": str(log), "error": f"{exc} (use --no-evidence to run without a log)"}
    summary = {"log": str(log), "flags": record["flags"], "exit_status": record["exit_status"],
               "justification": record["justification"], "withheld": assessment["withheld"]}
    if assessment["withheld"]:
        summary["message"] = withheld_message(assessment)
    return summary


def finish(prog: str, passed: bool, summary: Optional[Dict[str, Any]]) -> int:
    """Exit status: the verdict (0/1), 2 for a pass whose evidence could not be recorded, 3 for a withheld verdict."""
    if summary is None:
        return 0 if passed else 1
    if summary.get("error"):
        print(f"{prog}: evidence NOT recorded: {summary['error']}", file=sys.stderr)
        return 1 if not passed else 2
    if summary.get("withheld"):
        print(f"{prog}: {summary['message']}", file=sys.stderr)
    return summary["exit_status"]


# --------------------------------------------------------------------------------------------------------------
# Command line: python3 verification_evidence.py verify|show|fresh
# --------------------------------------------------------------------------------------------------------------

def evidence_command(action: str, evidence_dir_override: Optional[str] = None, last: int = 10,
                     subject_path: Optional[str] = None, name: Optional[str] = None, kind: Optional[str] = None) -> int:
    """verify: exit 1 if the hash chain is broken. show: print the last records. fresh: exit 0 only if the latest
    record for `subject_path` is a pass and the files it was bound to are unchanged (and the log is intact)."""
    log = log_path(evidence_dir_override)
    records, problems = read_log(log)
    if action == "verify":
        for line in problems:
            print(f"{log}: {line}", file=sys.stderr)
        print(f"{len(records)} record(s), {len(problems)} problem(s)")
        return 1 if problems else 0
    if action == "show":
        print(json.dumps(records[-last:], indent=2))
        return 0
    if not subject_path:
        print("evidence fresh needs --subject FILE", file=sys.stderr)
        return 2
    answer = fresh(records, subject_path, log.parent.parent, name, kind)
    answer["log_problems"] = problems
    print(json.dumps(answer, indent=2))
    return 0 if answer["fresh_pass"] and not problems else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the verification evidence log (./.verification/log.jsonl).")
    parser.add_argument("action", choices=("verify", "show", "fresh"))
    parser.add_argument("--evidence-dir")
    parser.add_argument("--last", type=int, default=10, help="show: number of records (default 10)")
    parser.add_argument("--subject", help="fresh: the solver or output file to look up")
    parser.add_argument("--name", help="fresh: restrict to this benchmark/check name")
    parser.add_argument("--kind", help="fresh: restrict to this kind (benchmark, compare, roundtrip, tests, ratchet)")
    args = parser.parse_args(argv)
    return evidence_command(args.action, args.evidence_dir, args.last, args.subject, args.name, args.kind)


if __name__ == "__main__":
    sys.exit(main())
