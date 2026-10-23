---
name: link-project
description: Analyzes multi-file Fortran codebases, builds a Directed Acyclic Graph (DAG) of MODULE, USE, and CALL dependencies, topologically sorts compilation order, and generates CMakeLists.txt.
disable-model-invocation: true
---

# /link-project

Scans an entire directory of Fortran source files (`.f`, `.f90`, etc.), builds a Directed Acyclic Graph (DAG) of inter-file dependencies, and determines the optimal compilation and inclusion order.

## Usage

```text
/link-project Scan src/ directory and generate a topological dependency graph and CMakeLists.txt.
```

Or via CLI:

```bash
python3 scripts/project_linker.py /path/to/project
```

## Features

- **Dependency Discovery**: Maps `MODULE`, `USE`, `SUBROUTINE`, `CALL`, and `INCLUDE` statements across files.
- **Topological Sorting**: Resolves cyclic dependencies and emits the strict linear order required by C++ compilers and linkers.
- **Build System Generation**: Emits modern `CMakeLists.txt` configured with C++20 standard, native architecture optimization (`-O3 -march=native`), and modular header/source structure (`include/` and `src/`).
