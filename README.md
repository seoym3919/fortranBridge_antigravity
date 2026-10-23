# fortranBridge: Fortran to Modern C++ & Python Translation Engine

[![Antigravity Plugin](https://img.shields.io/badge/Antigravity-Plugin%20v0.1.0-blue.svg)](https://github.com/seoym3919/fortranBridge_antigravity)
[![Agent Plugins 1.0.0](https://img.shields.io/badge/Agent%20Plugins-1.0.0%20Compliant-brightgreen.svg)](https://agent-plugins.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**fortranBridge** is an Antigravity plugin engineered to **translate legacy and modern Fortran codebases, directories, and routines (FORTRAN 66/77/90/95/2003) into idiomatic, high-performance C++ (C++17/20) and vectorized scientific Python (NumPy, SciPy, Numba).**

It operates on entire codebase directories by default, automating the most error-prone aspects of scientific code migration: **topological dependency linking (`MODULE`, `USE`, `CALL`), 1-based to 0-based array subscript offsets, memory layout transposition, COMMON block encapsulation, spaghetti GOTO deconstruction, CMakeLists.txt/Python package generation, and closed-loop floating-point equivalence verification ($L_2 < 10^{-12}$).**

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
# - fortranbridge (enabled, 0.1.0)
#   Components: 10 skills, 4 agents, 1 rule
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
| **`/verify-translation`**| Closed-loop numerical equivalence testing asserting relative $L_2$ error $< 10^{-12}$. | `/verify-translation legacy/ modernized/` |
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
python3 scripts/translation_verifier.py --self-test
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
              ├──► [cpp-modernizer]          ──► Emits C++17/20, std::span, Loop Transposition, <cmath>
              │
              ├──► [python-vectorizer]       ──► Emits Vectorized NumPy & Type Annotations
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

## 7. License

MIT License. Designed and maintained by `seoym3919` as part of the Antigravity scientific ecosystem alongside `scienceStack`, `theoryExtractor`, and `cppBridge`.
