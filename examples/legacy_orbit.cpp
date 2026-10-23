// ============================================================================
// Translated from FORTRAN to Modern C++17/20 by fortranBridge
// Scaffold: review every `throw std::logic_error` (constructs that were not
// translated) and verify numerically against the Fortran reference before use.
// ============================================================================

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

// Encapsulated Fortran COMMON block: /ORBIT_STATE/
struct OrbitStateBlock {
    double ecc{0};
    double semi_major{0};
    double inclination{0};
    double energy{0};
};

void propagate_orbit(std::vector<double>& r0, std::vector<double>& v0, double dt, double gm, std::vector<double>& rf, std::vector<double>& vf, int n_steps, OrbitStateBlock& orbit_state_common) {
    // Translated from Fortran SUBROUTINE propagate_orbit.
    constexpr double tol = 1.0e-14;
    constexpr double softening = 1.0e-6;
    int step{};
    int i{};
    std::vector<double> r_curr(3);
    std::vector<double> v_curr(3);
    double r_mag{};
    double acc_fac{};
    double v_sq{};
    for (i = 1; i <= 3; ++i) {
        r_curr[i - 1] = r0[i - 1];
        v_curr[i - 1] = v0[i - 1];
    }
    for (step = 1; step <= n_steps; ++step) {
        r_mag = std::sqrt((r_curr[0] * r_curr[0]) + (r_curr[1] * r_curr[1]) + (r_curr[2] * r_curr[2]) + (softening * softening));
        if (r_mag < 1.0e-4) {
            goto label_999;
        }
        acc_fac = -gm / (r_mag * r_mag * r_mag);
        for (i = 1; i <= 3; ++i) {
            v_curr[i - 1] = v_curr[i - 1] + dt * acc_fac * r_curr[i - 1];
            r_curr[i - 1] = r_curr[i - 1] + dt * v_curr[i - 1];
        }
    }
    for (i = 1; i <= 3; ++i) {
        rf[i - 1] = r_curr[i - 1];
        vf[i - 1] = v_curr[i - 1];
    }
    v_sq = (v_curr[0] * v_curr[0]) + (v_curr[1] * v_curr[1]) + (v_curr[2] * v_curr[2]);
    orbit_state_common.energy = 0.5 * v_sq - gm / r_mag;
    return;
    label_999: ;
    std::cout << "FATAL: SINGULAR CLOSE ENCOUNTER DETECTED" << std::endl;
    return;
}
