---
name: verify-translation
description: Compare the output of translated C++/Python against the original Fortran program's output (elementwise atol + rtol, ULP and sign bit). Use after any Fortran translation, or when asked to prove it is equivalent.
argument-hint: "--ref FORTRAN_OUT --test TRANSLATED_OUT [--tol 1e-12] [--report FILE]"
---

# /fortranbridge:verify-translation

Runs closed-loop numerical verification: the translated program must reproduce the original Fortran program's output.
The script decides; Claude must not judge equivalence by reading numbers itself. A numeric pass is not a complete port. Every equation translated, every procedure unit tested, and one regression of the whole mathematics is `${CLAUDE_PLUGIN_ROOT}/scripts/port_completeness.py --target python|cpp`. Only exit 0 from that command completes the port (rules/directives.md item 4). That regression and those unit tests pass only when the values are bitwise identical. If they differ, investigate the translation instead of widening a tolerance.

## Usage

1. Run the original Fortran program and the translation on the **same inputs** (a parameter sweep, fixed seeds)
   and write each program's numbers to a file: text with numbers separated by spaces, commas or lines (Fortran
   `D` exponents such as `1.0D-3` are accepted; lines starting with `#` or `!` are ignored), or a `.npy` array.
2. Compare:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/translation_verifier.py" --ref fortran.out --test translated.out --tol 1e-12 --report verification.md
```

`--self-test` checks the verifier itself on built-in functions (including negative controls that must be
rejected). **It does not verify a translation.**

## Verdict, exit status and what to report

- **Pass rule:** every element satisfies `|test - ref| <= atol + rtol*|ref|` (rtol is `--tol`, default 1e-12 for float64; atol default 0), the ULP distance is within the dtype budget, and +0 versus -0 fails. `.npy` shape and float32/float64 width must match; comparison is by index, so the same logical values in C or F order agree. A C-order flat of `a[i,j] = i + 1000*j` must not match the F-order flat. Relative L2 and Linf are printed and are not the verdict.
- **Always a failure:** a different shape or length, a dtype mismatch, no values at all, any NaN or Inf.
- **Sources:** a numeric pass must name `--ref-source`, `--test-source` and `--inputs`. A numeric mismatch is still exit 1. A numeric match without them is exit 2 and is not recorded as a pass.
- **Exit status:** 0 compared and equal; 1 compared and different; 2 nothing compared (missing arguments,
  unreadable or non-numeric files, unbound sources on a numeric match, or a pass whose evidence could not be recorded); 3 verdict withheld (see below).
- **Report to the user:** say "equivalent" only if the exit status was 0, and quote the status, the number of points and the
  norms. Never describe a `--self-test` result as evidence about a translation.

## Stronger: produce the outputs instead of trusting files

This script compares two output files it is given. For a guarantee that the reference really came from the Fortran program and the port's
output from the port, use the scienceStack plugin's manifest feature: a `compare` check in `reference-checks.json` with
`ref_from` (compile and run the legacy code, sources and inputs pinned by SHA-256, the committed reference must reproduce) and
`test_from` (run the port). See `/sciencestack:check-against-reference`, "Building both sides". Its steps are commands: run only a
manifest you trust.

## Evidence log and exit status 3

Every comparison is appended as one line to `./.verification/log.jsonl` (`--evidence-dir` or `$VERIFICATION_EVIDENCE_DIR` to move it,
`--no-evidence` to skip it): the verdict, the tolerance, and the SHA-256 of both output files. Lines are hash-chained; check them with
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/verification_evidence.py" verify`.

- **Exit 3, verdict withheld:** the tolerance is looser than in the previous *failed* run (including a dropped numeric tolerance key) and the primary files did not change. Fix the code. A `--justify` sentence is recorded and does not release the verdict. Release needs `tolerance-approvals.json` (schema `verification-tolerance-approvals/1`) naming the check key, the SHA-256 of the previous failed log line, the old tolerance, the new tolerance, and a reason of at least 10 characters. If a primary file did change, the run is allowed and only flagged. Repeating the command does not release it, and files named with `--subject` cannot release it. A new `--evidence-dir` starts an empty history; a clean re-run in CI is the gate.
- If the log cannot be written, a pass becomes exit 2 (a pass without evidence is not a pass); a failure stays 1.
- Before telling the user the result is equivalent, `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/verification_evidence.py" fresh --subject <file>`
  must exit 0: the latest record for that file is a pass and the file is unchanged since. A stale or missing record is not evidence.
- The log is a local file: anything running as the user can rewrite all of it, and a truncated tail is not detected. A clean re-run in CI
  is the only unforgeable gate.

## Metrics Checked

- Maximum Absolute Error: $L_\infty = \max |y_{\text{ref}} - y_{\text{test}}|$
- Relative $L_2$ Error: $\frac{\|y_{\text{ref}} - y_{\text{test}}\|_2}{\|y_{\text{ref}}\|_2 + \varepsilon}$ (printed, not the pass rule)
- Relative $L_\infty$ Error: $\frac{\max |y_{\text{ref}} - y_{\text{test}}|}{\max |y_{\text{ref}}| + \varepsilon}$ (printed, not the pass rule)

Conservation of physical invariants (energy, mass, momentum) is not checked by this script; if it matters, write the
invariant as an output column of both programs so that it is compared too.
