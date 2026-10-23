# fortranBridge: Fortran to Modern C++ & Python Translation Engine

[![Antigravity Plugin](https://img.shields.io/badge/Antigravity-Plugin%20v0.1.0-blue.svg)](https://github.com/seoym3919/fortranBridge_antigravity)
[![Agent Plugins 1.0.0](https://img.shields.io/badge/Agent%20Plugins-1.0.0%20Compliant-brightgreen.svg)](https://agent-plugins.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**fortranBridge** is an Antigravity plugin engineered to **translate legacy and modern Fortran (FORTRAN 66/77/90/95/2003) into idiomatic, high-performance C++ (C++17/20) and vectorized scientific Python (NumPy, SciPy, Numba).**

It automates the most error-prone aspects of scientific code migration: **1-based to 0-based array subscript offsets, memory layout transposition, COMMON block encapsulation, spaghetti GOTO deconstruction, and closed-loop floating-point equivalence verification ($L_2 < 10^{-12}$).**

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

In the Antigravity chat, interact directly with Fortran translation and auditing skills:

| Command | Action & Modernization Capability | Example Prompt |
| :--- | :--- | :--- |
| **`/translate-fortran`** | Master translation command: converts Fortran routines or codebases to C++ or Python. | `/translate-fortran solver.f --target cpp` |
| **`/fortran-to-cpp`** | Ports Fortran to clean C++17/20 with `std::vector`, `std::span`, and cache-friendly loop order. | `/fortran-to-cpp orbit.f --out orbit.cpp` |
| **`/fortran-to-python`** | Ports Fortran to vectorized scientific Python with `@dataclass` state and NumPy arrays. | `/fortran-to-python orbit.f --out orbit.py` |
| **`/audit-indexing`** | Rigorous numerical audit of 1-based vs 0-based subscripts, memory layout, and slice ranges. | `/audit-indexing Audit array subscripts in orbit.cpp against orbit.f` |
| **`/encapsulate-common`**| Encapsulates legacy `COMMON` blocks and `EQUIVALENCE` into thread-safe structs/dataclasses. | `/encapsulate-common global_blocks.f` |
| **`/restructure-goto`** | De-spaghettifies backward loops, computed `GOTO`s, and arithmetic `IF`s into structured control flow. | `/restructure-goto legacy_solver.f` |
| **`/verify-translation`**| Closed-loop numerical equivalence testing asserting relative $L_2$ error $< 10^{-12}$. | `/verify-translation orbit.f orbit.py` |
| **`/decompile-format`** | Translates Fortran `FORMAT` descriptors into C++20 `std::format` and Python f-strings. | `/decompile-format 100 FORMAT(1X, 'E=', 1PE15.7)` |
| **`/link-project`** | Computes topological dependency DAGs from `MODULE`/`USE` and generates `CMakeLists.txt`. | `/link-project /path/to/fortran_project` |
| **`/infer-intents`** | Performs dataflow analysis to infer parameter intents (`const T&` in C++, return tuples in Python). | `/infer-intents subroutines.f` |

#### Method B: Subagent Autonomous Orchestration

Antigravity automatically delegates tasks across 4 specialized subagents:
- **`fortran-translator`** (`Model: "pro"`): Master translation coordinator and AST analysis lead.
- **`cpp-modernizer`** (`Model: "flash"`): High-performance C++17/20 engineer ensuring cache contiguity.
- **`python-vectorizer`** (`Model: "flash"`): Scientific Python and NumPy array vectorization specialist.
- **`indexing-auditor`** (`Model: "pro"`): Mathematical and numerical offset auditor ($L_2 < 10^{-12}$).

#### Method C: Standalone Zero-Token CLI (Offline Execution)

Translate and audit code directly in your terminal at zero LLM token cost:

```bash
# 1. Run numerical equivalence self-test suite
python3 scripts/translation_verifier.py --self-test

# 2. De-spaghettify legacy GOTO jumps
python3 scripts/goto_restructurer.py examples/legacy_code.f

# 3. Analyze multi-file project dependencies and generate CMakeLists.txt
python3 scripts/project_linker.py /path/to/fortran_project

# 4. Decompile FORMAT descriptors to modern format strings
python3 scripts/format_decompiler.py "100 FORMAT(1X, 'ENERGY = ', 1P, E15.7, 2X, 'STEP = ', I6)"
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
| [`fortran-translator`](./agents/fortran-translator.md) | `Model: "pro"` (`claude-opus-4-6-thinking`) | Lead translation architect. Manages AST decomposition, interface intents, and multi-file workflows. |
| [`cpp-modernizer`](./agents/cpp-modernizer.md) | `Model: "flash"` (`gemini-3.8-flash-high`) | C++ performance engineer. Emits zero-overhead C++17/20 with contiguous cache memory layouts. |
| [`python-vectorizer`](./agents/python-vectorizer.md) | `Model: "flash"` (`gemini-3.8-flash-high`) | Python specialist. Emits vectorized NumPy operations, dataclasses, and Pythonic return tuples. |
| [`indexing-auditor`](./agents/indexing-auditor.md) | `Model: "pro"` (`claude-opus-4-6-thinking`) | Mathematical & numerical auditor. Verifies 0-based offsets and relative error norms ($L_2 < 10^{-12}$). |

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
- **theoryReverse**: Disassembles unannotated code into continuous PDEs, dimensional null spaces, and literature citations.
- **fortranBridge**: Ports legacy Fortran models directly into modern C++ or Python for seamless integration with both stacks.

---

## 7. License

MIT License. Designed and maintained by `seoym3919` as part of the Antigravity scientific ecosystem alongside `scienceStack`, `theoryReverse`, and `cppBridge`.
