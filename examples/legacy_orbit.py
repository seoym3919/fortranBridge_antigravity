# ============================================================================
# Translated from FORTRAN to Modern Python / NumPy by fortranBridge
# ============================================================================
from __future__ import annotations
from dataclasses import dataclass, field
import math
import numpy as np


# ============================================================================
# Encapsulated Fortran COMMON Blocks
# ============================================================================

# Encapsulated Fortran COMMON block: /ORBIT_STATE/
@dataclass
class OrbitStateBlock:
    ecc: float = 0.0
    semi_major: float = 0.0
    inclination: float = 0.0
    energy: float = 0.0

def propagate_orbit(r0: np.ndarray, v0: np.ndarray, dt: float, gm: float, rf: np.ndarray, vf: np.ndarray, n_steps: float, orbit_state_state: OrbitStateBlock):
    r"""Translated from Fortran propagate_orbit."""
    r_curr = np.zeros(3, dtype=np.float64)
    v_curr = np.zeros(3, dtype=np.float64)
    tol = 1.0e-14
    softening = 1.0e-6
    for i in range(int(3)):
        r_curr[i] = r0[i]
        v_curr[i] = v0[i]
    for step in range(int(n_steps)):
        r_mag = math.sqrt(r_curr[0]**2 + r_curr[1]**2 + r_curr[2]**2 + softening**2)
        if r_mag  <  1.0e-4:
            # goto label_999
            return
        acc_fac = -gm / (r_mag * r_mag * r_mag)
        for i in range(int(3)):
            v_curr[i] = v_curr[i] + dt * acc_fac * r_curr[i]
            r_curr[i] = r_curr[i] + dt * v_curr[i]
    for i in range(int(3)):
        rf[i] = r_curr[i]
        vf[i] = v_curr[i]
    v_sq = v_curr[0]**2 + v_curr[1]**2 + v_curr[2]**2
    orbit_state_state.energy = 0.5e0 * v_sq - gm / r_mag
    return
    print('fatal: singular close encounter detected')
    return
