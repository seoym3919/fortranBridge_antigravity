// ============================================================================
// Translated from FORTRAN to Modern C++17/20 by fortranBridge
// ============================================================================

#include <algorithm>
#include <array>
#include <cmath>
#include <iostream>
#include <vector>


// ============================================================================
// Encapsulated Fortran COMMON Blocks
// ============================================================================

// Encapsulated Fortran COMMON block: /ORBIT_STATE/
struct OrbitStateBlock {
    double ecc{0};
    double semi_major{0};
    double inclination{0};
    double energy{0};
};

void propagate_orbit(std::vector<double>& r0, std::vector<double>& v0, double& dt, double& gm, std::vector<double>& rf, std::vector<double>& vf, double& n_steps, OrbitStateBlock& orbit_state_state) {
    std::vector<double> r_curr(3, 0.0);
    std::vector<double> v_curr(3, 0.0);
    constexpr double tol = 1.0e-14;
    constexpr double softening = 1.0e-6;
    for (int i = 0; i < 3; ++i) {
        r_curr[i] = r0[i];
        v_curr[i] = v0[i];
    }
label_10:
    for (int step = 0; step < n_steps; ++step) {
        double r_mag = std::sqrt((r_curr[0] * r_curr[0]) + (r_curr[1] * r_curr[1]) + (r_curr[2] * r_curr[2]) + (softening * softening));
        if (r_mag  <  1.0e-4) goto label_999;
        double acc_fac = -gm / (r_mag * r_mag * r_mag);
        for (int i = 0; i < 3; ++i) {
            v_curr[i] = v_curr[i] + dt * acc_fac * r_curr[i];
            r_curr[i] = r_curr[i] + dt * v_curr[i];
        }
label_50:
    }
label_100:
    for (int i = 0; i < 3; ++i) {
        rf[i] = r_curr[i];
        vf[i] = v_curr[i];
    }
label_150:
    double v_sq = (v_curr[0] * v_curr[0]) + (v_curr[1] * v_curr[1]) + (v_curr[2] * v_curr[2]);
    orbit_state_state.energy = 0.5e0 * v_sq - gm / r_mag;
    return;
    std::cout << 'fatal: singular close encounter detected' << std::endl;
label_999:
    return;
}
