# fortranBridge Directives

1. **Directory-First Scope**: Translate entire Fortran directories/codebases by default via `python3 ~/.gemini/config/plugins/fortranbridge/scripts/fortran_bridge_cli.py <dir> --target cpp|python|both --out-dir <out>`. Delegate multi-module porting to `fortran-translator` (`Model: "pro"`) and offset verification to `indexing-auditor` (`Model: "pro"`).
2. **Indexing, Memory & COMMONs**: Convert all 1-based Fortran subscripts to 0-based indexing (`a[i]` for 0-based loops, `a[i - 1]` for 1-based literals). Transpose nested loops in C++ so the innermost loop iterates over the contiguous row-major dimension. Encapsulate `COMMON`/`EQUIVALENCE` blocks into C++ structs/classes or Python `@dataclass` objects.
3. **Closed-Loop Verification**: Verify translated code against original Fortran with `scripts/translation_verifier.py` ($L_2 < 10^{-12}$).
