---
name: decompile-format
description: Decompiles legacy Fortran FORMAT edit descriptors (Iw, Fw.d, Ew.d, nX, 1P) and WRITE statements into modern C++20 std::format and Python f-strings.
---

# /decompile-format

Decompiles Fortran `FORMAT` edit descriptors and `WRITE` statements into clean, modern C++20 `std::format` or Python f-strings.

## Usage

```text
/decompile-format 100 FORMAT(1X, 'ENERGY = ', 1P, E15.7, 2X, 'STEP = ', I6)
```

Or via CLI:

```bash
python3 scripts/format_decompiler.py
```

## Supported Descriptors

- `Iw`, `Iw.m`: Decimal integers $\to$ `{:wd}`
- `Fw.d`: Fixed-point floats $\to$ `{:w.df}`
- `Ew.d`, `ESw.d`: Exponential scientific notation $\to$ `{:w.de}`
- `Gw.d`: General floating-point numbers $\to$ `{:w.dg}`
- `Aw`, `A`: Alphanumeric characters / strings $\to$ `{:s}`
- `nX`: Horizontal space padding $\to$ `n` spaces
- `'literal'`: Literal strings preserved as text
- `/`: Record separator (newline) $\to$ `\n`
- Repetition factors: `3F10.5` expanded to three consecutive floats
