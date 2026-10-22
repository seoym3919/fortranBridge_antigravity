# Fortran Translation Audit Report: `legacy_orbit.f`

**Source Format**: Fixed-Format (FORTRAN 66/77)  
**Total Logical Statements**: 29  
**COMMON Blocks Encapsulated**: 1

## 1. Statement Classification Inventory

| Statement Type | Count | Translation Strategy |
|---|---|---|
| `ASSIGNMENT` | 10 | Translated with 0-based indexing and intrinsic conversions |
| `DO` | 4 | Converted to 0-based for loop (C++: `for (int i=0; i<N; ++i)`, Python: `range(N)`) |
| `CONTINUE` | 4 | Direct semantic translation |
| `RETURN` | 2 | Direct semantic translation |
| `SUBROUTINE` | 1 | Translated to free function or encapsulated class method |
| `IMPLICIT` | 1 | Direct semantic translation |
| `TYPE_DECL` | 1 | Direct semantic translation |
| `DIMENSION` | 1 | Direct semantic translation |
| `COMMON` | 1 | Encapsulated into typed C++ struct or Python @dataclass state object |
| `PARAMETER` | 1 | Converted to C++ `constexpr` or Python module-level constant |
| `IF_STMT` | 1 | Direct semantic translation |
| `IO` | 1 | Direct semantic translation |
| `END` | 1 | Direct semantic translation |

## 2. COMMON Block State Encapsulations

| Block Name | Struct / Class Name | Encapsulated Members |
|---|---|---|
| `/ORBIT_STATE/` | `OrbitStateBlock` | `ecc` (REAL*8), `semi_major` (REAL*8), `inclination` (REAL*8), `energy` (REAL*8) |

## 3. Recommended Modernization Best Practices

1. **Verify Index Offsets**: Ensure all 1-based Fortran arrays are accessed with 0-based indices in C++ / Python.
2. **Loop Memory Contiguity**: In C++, outer loops should index row coordinates and inner loops column coordinates for cache efficiency.
3. **Pass-by-Reference Constness**: Mark non-mutated parameters as `const` in C++ signatures.
4. **Equivalence Testing**: Execute `scripts/translation_verifier.py` to assert numerical output equivalence.