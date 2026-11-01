# fortranBridge: Fortran to Modern C++ & Python Translation Engine

[![Antigravity Plugin](https://img.shields.io/badge/Antigravity-Plugin%20v1.1.0-blue.svg)](https://github.com/seoym3919/fortranBridge_antigravity)
[![Agent Plugins 1.0.0](https://img.shields.io/badge/Agent%20Plugins-1.0.0%20Compliant-brightgreen.svg)](https://agent-plugins.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**fortranBridge** is an Antigravity plugin engineered to **translate legacy and modern Fortran codebases, directories, and routines (FORTRAN 66/77/90/95/2003) into idiomatic, high-performance C++ (C++17/20) and scientific Python (NumPy), with identical equations and algorithm.**

It operates on entire codebase directories by default, automating the most error-prone aspects of scientific code migration: **topological dependency linking (`MODULE`, `USE`, `CALL`), 1-based to 0-based array subscript offsets, COMMON block encapsulation, spaghetti GOTO deconstruction, CMakeLists.txt/Python package generation, and closed-loop equivalence verification. `scripts/port_completeness.py` requires every equation to be translated, every procedure to have a bitwise-identical unit test, and one bitwise-identical regression of the whole original mathematics. If those values differ, investigate the translation.**

---

## 1. Installation & How to Use

### Installation

Install `fortranBridge` globally or locally in Google Antigravity:

```bash
# 1. Validate compliance with Agent Plugins 1.0.0 (Strict mode)
agy plugin validate /home/seo3919/AI_skills/fortranBridge_antigravity --strict

# 2. Install globally in Antigravity
agy plugin install /home/seo3919/AI_skills/fortranBridge_antigravity

# Or install from GitHub:
# agy plugin install seoym3919/fortranBridge_antigravity
```

Verify installation status:
```bash
agy plugin list
# - fortranbridge (enabled, 1.1.0)
#   Components: 10 skills, 2 agents, 2 rules, 2 hooks
```

---

### How to Use

#### Method A: Interactive Slash Commands in Antigravity

In the Antigravity chat, interact directly with Fortran translation and auditing skills targeting entire directories or files:

| Command | Action & Modernization Capability | Example Prompt |
| :--- | :--- | :--- |
| **`/translate-fortran`** | **Master codebase-wide translation**: converts an entire directory or routine to modern C++ or Python. | `/translate-fortran [directory] --target cpp` |
| **`/fortran-to-cpp`** | Ports Fortran to clean C++17/20 with `std::vector`, `std::span`, and cache-friendly loop order. | `/fortran-to-cpp src/ --out-dir cpp_src/` |
| **`/fortran-to-python`** | Ports Fortran to vectorized scientific Python with `@dataclass` state and NumPy arrays. | `/fortran-to-python src/ --out-dir py_pkg/` |
| **`/link-project`** | Computes topological dependency DAGs from `MODULE`/`USE` and generates `CMakeLists.txt`. | `/link-project /path/to/fortran_project` |
| **`/audit-indexing`** | Rigorous numerical audit of 1-based vs 0-based subscripts, memory layout, and slice ranges across files. | `/audit-indexing Audit array subscripts across src/` |
| **`/encapsulate-common`**| Encapsulates legacy `COMMON` blocks and `EQUIVALENCE` into thread-safe structs/dataclasses. | `/encapsulate-common src/` |
| **`/restructure-goto`** | De-spaghettifies backward loops, computed `GOTO`s, and arithmetic `IF`s into structured control flow. | `/restructure-goto legacy_solver.f` |
| **`/verify-translation`**| Compares the Fortran program's output file with the translation's output (text with Fortran `D` exponents, or `.npy`) and exits non-zero unless relative $L_2$ and $L_\infty$ are both $\le 10^{-12}$. | `/verify-translation --ref fortran.out --test python.out` |
| **`/decompile-format`** | Translates Fortran `FORMAT` descriptors into C++20 `std::format` and Python f-strings. | `/decompile-format 100 FORMAT(1X, 'E=', 1PE15.7)` |
| **`/infer-intents`** | Performs dataflow analysis to infer parameter intents (`const T&` in C++, return tuples in Python). | `/infer-intents src/` |

#### Method B: Subagent Autonomous Orchestration

Antigravity automatically delegates tasks across specialized subagents:
- **`fortran-translator`** (`Model: "pro"` — `claude-opus-5-5-high` / `claude-sonnet-5-5-high` / `gemini-4-argon-high`): Master translation coordinator. Ingests entire directories, coordinates multi-file DAG linking, and modernizes Fortran into C++17/20 or vectorized Python.
- **`indexing-auditor`** (`Model: "pro"` — `claude-opus-5-5-high`): Mathematical and numerical offset auditor ($L_2 < 10^{-12}$).

#### Method C: Standalone Zero-Token CLI (Offline Execution)

Translate and audit codebases directly in your terminal at zero LLM token cost:

```bash
# 1. Modernize entire Fortran codebase directory to modern C++ with CMakeLists.txt
python3 scripts/fortran_bridge_cli.py /path/to/fortran_repo --target cpp

# 2. Modernize entire Fortran codebase directory to vectorized Python with __init__.py
python3 scripts/fortran_bridge_cli.py /path/to/fortran_repo --target python --out-dir modernized_py

# 3. Modernize an individual Fortran file
python3 scripts/fortran_bridge_cli.py orbit.f --target cpp --out orbit.cpp

# 4. Analyze multi-file project dependencies and generate CMakeLists.txt
python3 scripts/project_linker.py /path/to/fortran_project

# 5. Run numerical equivalence self-test suite
python3 scripts/translation_verifier.py --self-test      # checks the verifier itself, not a translation
python3 scripts/translation_verifier.py --ref fortran.out --test translated.out   # verifies a translation

# 6. Run the 57-test unit suite (parser semantics, verifier, NumPy/C++ equivalence, CLI flags & Python 3.10 portability)
python3 -B -m unittest discover -s tests -v
```

---

## 2. The Challenge of Fortran Modernization

Decades of mission-critical scientific software exist in legacy Fortran. Manual porting typically introduces severe bugs:
- **Off-by-One Disasters**: Fortran defaults to 1-based indexing (`A(1)`), whereas C++ and Python are 0-based (`a[0]`).
- **Memory Stride Degradation**: Fortran is column-major (`order='F'`), whereas C/C++ is row-major (`order='C'`). Naively ported nested loops cause massive CPU cache thrashing.
- **Global `COMMON` Bloat**: Shared memory blocks bypass function interfaces, creating unthread-safe global state.
- **Spaghetti Control Flow**: Arithmetic `IF (A-B) 10, 20, 30` and computed `GOTO (10, 20, 30) I` defeat structured programming.

**fortranBridge eliminates these hazards.**

---

## 3. Architecture & Translation Pipeline

```
   Legacy Fortran (F66 / F77 / F90 / F95 / F2003)
              │
              ├──► [fortran_lexer]           ──► Normalizes Fixed/Free Format & Statement AST
              │
              ├──► [fortran_semantics]       ──► Expression Parser, Symbol Table, Static Typing & Per-Unit Analysis
              │
              ├──► [fortran_emit_base]       ──► Shared Statement Walker & Unsupported Construct Guards
              │
              ├──► [common_block_analyzer]   ──► Encapsulates COMMON -> C++ Structs / Python Dataclasses
              │
              ├──► [equivalence_analyzer]    ──► De-aliases EQUIVALENCE -> std::span / NumPy Views
              │
              ├──► [goto_restructurer]       ──► Replaces GOTOs -> While Loops & Switches
              │
              ├──► [intent_analyzer]         ──► Infers INTENT(IN/OUT) -> const T& & Return Tuples
              │
              ├──► [format_decompiler]       ──► FORMAT Descriptors -> C++20 std::format / f-strings
              │
              ├──► [project_linker]          ──► Multi-File DAG Linker & CMakeLists.txt Generator
              │
              ├──► [parallel_modernizer]     ──► !$OMP / DO CONCURRENT -> #pragma omp / Numba prange
              │
              ├──► [fortran_to_cpp]          ──► Emits C++17/20, std::span, Loop Transposition, <cmath>
              │
              ├──► [fortran_to_python]       ──► Emits Vectorized NumPy & Type Annotations
              │
              ├──► [f2py_harness]            ──► Automated Direct f2py vs. Python Assertions
              │
              └──► [indexing-auditor]        ──► Validates 0-based bounds & Relative L_2 Error < 1e-12
```

---

## 4. Specialized Subagents

| Subagent | Model | Role |
|---|---|---|
| [`fortran-translator`](./agents/fortran-translator.md) | `Model: "pro"` (`claude-opus-5-5-high` / `claude-sonnet-5-5-high` / `gemini-4-argon-high`) | Lead translation architect. Manages AST decomposition, C++17/20 & vectorized Python generation, interface intents, and multi-file workflows. |
| [`indexing-auditor`](./agents/indexing-auditor.md) | `Model: "pro"` (`claude-opus-5-5-high`) | Mathematical & numerical auditor. Verifies 0-based offsets, memory stride order, and relative error norms ($L_2 < 10^{-12}$). |

---

## 5. Translation Walkthrough Example

### Input: Legacy FORTRAN 77 Orbital Stepper
```fortran
      SUBROUTINE PROPAGATE(R0, V0, DT, GM, RF, VF, N)
      DOUBLE PRECISION R0, V0, DT, GM, RF, VF, R_CURR, V_CURR, R_MAG
      DIMENSION R0(3), V0(3), RF(3), VF(3), R_CURR(3), V_CURR(3)
      COMMON /ORBIT/ ENERGY
      DO 10 I = 1, 3
         R_CURR(I) = R0(I)
         V_CURR(I) = V0(I)
  10  CONTINUE
      DO 100 STEP = 1, N
         R_MAG = DSQRT(R_CURR(1)**2 + R_CURR(2)**2 + R_CURR(3)**2)
         DO 50 I = 1, 3
            V_CURR(I) = V_CURR(I) - DT * GM / (R_MAG**3) * R_CURR(I)
            R_CURR(I) = R_CURR(I) + DT * V_CURR(I)
  50     CONTINUE
  100 CONTINUE
      RETURN
      END
```

### Output 1: Modern C++17/20
```cpp
#include <vector>
#include <array>
#include <cmath>

struct OrbitBlock {
    double energy{0};
};

void propagate(std::vector<double>& r0, std::vector<double>& v0, double dt, double gm, 
               std::vector<double>& rf, std::vector<double>& vf, int n, OrbitBlock& orbit_state) {
    std::vector<double> r_curr(3, 0.0);
    std::vector<double> v_curr(3, 0.0);
    for (int i = 0; i < 3; ++i) {
        r_curr[i] = r0[i];
        v_curr[i] = v0[i];
    }
    for (int step = 0; step < n; ++step) {
        double r_mag = std::sqrt((r_curr[0] * r_curr[0]) + (r_curr[1] * r_curr[1]) + (r_curr[2] * r_curr[2]));
        double fac = dt * gm / (r_mag * r_mag * r_mag);
        for (int i = 0; i < 3; ++i) {
            v_curr[i] -= fac * r_curr[i];
            r_curr[i] += dt * v_curr[i];
        }
    }
}
```

### Output 2: Vectorized Python / NumPy
```python
from dataclasses import dataclass
import numpy as np
import math

@dataclass
class OrbitBlock:
    energy: float = 0.0

def propagate(r0: np.ndarray, v0: np.ndarray, dt: float, gm: float, n: int, orbit_state: OrbitBlock):
    r_curr = r0.copy()
    v_curr = v0.copy()
    for _ in range(n):
        r_mag = np.linalg.norm(r_curr)
        acc = -gm / (r_mag**3) * r_curr
        v_curr += dt * acc
        r_curr += dt * v_curr
    return r_curr, v_curr
```

---

## 6. Interoperability with Ecosystem Plugins

- **scienceStack**: Forward-engineers high-order scientific solvers and symplectic integrators from first-principles continuous physics.
- **theoryExtractor**: Disassembles unannotated code into continuous PDEs, dimensional null spaces, and literature citations.
- **fortranBridge**: Ports legacy Fortran models directly into modern C++ or Python for seamless integration with both stacks.

---

## 7. Scope & Limitations of the Bundled Translators

`fortran_to_python.py` and `fortran_to_cpp.py` share one front end (`fortran_semantics.py`: expression parser, symbol table, static typing). They translate a structured subset and **fail loudly** on the rest.

| Supported | Not translated (emitted as `raise NotImplementedError` / `throw std::logic_error`) |
| :--- | :--- |
| SUBROUTINE / FUNCTION / PROGRAM (several per file), MODULE with CONTAINS and USE (ONLY, renaming), internal procedures (CONTAINS in a unit), fixed and free form | `BACKSPACE`, `ENDFILE`, unformatted and direct-access files, `NAMELIST`, `INQUIRE` other than `EXIST=` |
| Typed declarations with kinds (`REAL(dp)`, `INTEGER*8`, `COMPLEX`, `CHARACTER(n)`), implicit typing, `DIMENSION`, `ALLOCATABLE` and assumed-shape arrays, non-unit lower bounds, `PARAMETER`, derived types | Derived-type components that are `POINTER` or `ALLOCATABLE` (linked structures), type-bound procedures, inheritance, generic `INTERFACE` (overloading), `USE` of a module defined elsewhere |
| `DO`, `DO WHILE`, `EXIT`, `CYCLE`, `IF / ELSE IF / ELSE`, `SELECT CASE`, `GO TO`, computed `GO TO`, arithmetic `IF`, `END=` / `ERR=` / `IOSTAT=` | `EQUIVALENCE` of arrays in C++ (Python supports it); a pointer to an array section in C++; derived-type arrays above rank 1; `READ` of complex values |
| `CALL` (scalar results returned / passed by reference), `COMMON`, `DATA` / `SAVE`, `EQUIVALENCE`, intrinsics, whole-array expressions and sections, array constructors, `POINTER` / `TARGET` / `ALLOCATE` / `DEALLOCATE`, `PRINT` / `WRITE` / `READ` / `OPEN` / `CLOSE` / `REWIND` with `FORMAT` | |

The CLI validates every output: Python must parse (exit code 3 otherwise) and the number of untranslated constructs is reported on stderr.

## 8. How a Conversion Runs

Every codebase conversion follows these steps, and the tools refuse to skip one:

1. **Plan.** `scripts/conversion_plan.py SOURCES --target python|cpp` lists every function and module in `FORTRAN_FUNCTION_LIST.md`, runs the Fortran under `gprof` to see how they link, and writes `PLAN.md`: the order of conversion (callees first), the rules, and the golden data table.
2. **Approval.** Nothing converts or tests until a human approves that exact `PLAN.md` by typing `APPROVE PLAN <code>` as their own message. The `PreInvocation` hook (`scripts/plan_approval.py`) reads the message. Any edit to `PLAN.md` voids the approval, and a plan of 25 units or more is flagged as large scope.
3. **Golden data.** `scripts/regression_test.py record` stores the Fortran's results as bytes before conversion, so a later change cannot redefine the expected answer. Every function must be covered, with nominal and corner cases and more than 10 distinct whole-program cases.
4. **Convert function by function**, in plan order. After each function comes a unit test (`scripts/unit_compare.py` runs both sides itself and compares raw bytes), then at least one regression test. A function is refused until everything earlier has passed both.
5. **Final regression.** `regression_test.py run --final` runs every case.

Byte identical is the rule throughout. Machine precision (eps relative, 2 ULP) is accepted only for what the plan shows reaching a library such as `sin`, `exp` or `MATMUL`, where Fortran and the target need not share the last bit. `sqrt` is not on that list: IEEE 754 fixes it, so a difference there is a bug.

## 9. Identical Equations and Algorithm

A translation is checked three ways, and a claim needs all of them. Each one catches what the others cannot.

| Check | Compares | Catches |
| --- | --- | --- |
| `scripts/identity_checker.py` | gfortran's own parse tree with the translation (Python through `ast`, C++ through clang's AST), statement by statement; `GO TO` programs by walking both control-flow graphs in lockstep | Reassociated or reordered arithmetic, changed loops, bounds, branches, kinds, literals, declarations, returned results |
| `scripts/trace_checker.py` | The bits of every assignment, in order, between instrumented copies of both programs; every statement must execute (`gcov` and the probes) | A helper that matches its table entry but computes something else; a statement no input reached |
| `scripts/translation_verifier.py` | The printed results | Values that are printed but never assigned |

Verdicts are enforced by the `Stop` hook (`scripts/claim_guard.py`), which repeats the static check itself instead of trusting the log. The final message of a turn that states a result ends with one `VERDICT:` line (`equivalent`, `identical algorithm`, `complete` or `unverified`).

## 10. Tests

```bash
python3 -B -m unittest discover -s tests -v   # 348 tests: parser, lexer, numerics vs NumPy, C++ structure, CLI, documented flags, the conversion strategy, identity and trace checks, the PreInvocation & Stop hooks
```

Manual: [`docs/fortranBridge_manual.pdf`](./docs/fortranBridge_manual.pdf) (LaTeX source alongside).

## 11. License

MIT License. Designed and maintained by `seoym3919` as part of the Antigravity scientific ecosystem alongside `scienceStack`, `theoryExtractor`, and `cppBridge`.
