# ============================================================================
# Translated from FORTRAN to Modern Python / NumPy by fortranBridge
# Scaffold: review every `raise NotImplementedError` (constructs that were not
# translated) and verify numerically against the Fortran reference before use.
# ============================================================================
from __future__ import annotations
from dataclasses import dataclass, field
import math
import numpy as np


# Encapsulated Fortran COMMON block: /ORBIT_STATE/
@dataclass
class OrbitStateBlock:
    ecc: float = 0.0
    semi_major: float = 0.0
    inclination: float = 0.0
    energy: float = 0.0


def propagate_orbit(r0: np.ndarray, v0: np.ndarray, dt: float, gm: float, rf: np.ndarray, vf: np.ndarray, n_steps: int, orbit_state_common: OrbitStateBlock):
    r"""Translated from Fortran SUBROUTINE propagate_orbit."""
    r_curr = np.zeros(3, dtype=np.float64)
    v_curr = np.zeros(3, dtype=np.float64)
    tol = 1.0e-14
    softening = 1.0e-6
    for i in range(1, 4):
        r_curr[i - 1] = r0[i - 1]
        v_curr[i - 1] = v0[i - 1]
    for step in range(1, n_steps + 1):
        r_mag = math.sqrt(r_curr[0] ** 2 + r_curr[1] ** 2 + r_curr[2] ** 2 + softening ** 2)
        if r_mag < 1.0e-4:
            raise NotImplementedError('fortranBridge: GO TO is not translated; restructure the control flow first (restructure-goto skill): GO TO 999')
        acc_fac = -gm / (r_mag * r_mag * r_mag)
        for i in range(1, 4):
            v_curr[i - 1] = v_curr[i - 1] + dt * acc_fac * r_curr[i - 1]
            r_curr[i - 1] = r_curr[i - 1] + dt * v_curr[i - 1]
    for i in range(1, 4):
        rf[i - 1] = r_curr[i - 1]
        vf[i - 1] = v_curr[i - 1]
    v_sq = v_curr[0] ** 2 + v_curr[1] ** 2 + v_curr[2] ** 2
    orbit_state_common.energy = 0.5 * v_sq - gm / r_mag
    return
    # fortran label 999
    print('FATAL: SINGULAR CLOSE ENCOUNTER DETECTED')
    return
