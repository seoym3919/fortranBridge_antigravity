---
name: encapsulate-common
description: Encapsulates Fortran COMMON blocks and EQUIVALENCE statements into modern C++ classes/structs and Python dataclasses.
disable-model-invocation: true
argument-hint: "<file>"
---

# /encapsulate-common

Analyzes all subroutines in a Fortran file or project to detect shared `COMMON /NAME/` blocks and `EQUIVALENCE` aliases, converting them into thread-safe, encapsulated state structures.

## Usage

```text
/encapsulate-common Extract all COMMON blocks in simulation.f into C++ structs.
```

Or via CLI:

```bash
python3 scripts/common_block_analyzer.py simulation.f
```
