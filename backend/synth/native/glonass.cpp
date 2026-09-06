// backend/synth/native/glonass.cpp
// Verbatim port of backend/synth/glonass.py: _accel / _rk4_step and the ~60 s
// RK4 stepping loop of the GLONASS ICD "simplified equations of motion" in the
// rotating PZ-90.11 frame. `double` throughout. Bounded by
// tests/synth/test_glonass_state.py::test_cpp_glonass_matches_python.
#include "abi.h"
#include <cmath>

namespace gs {

namespace {
constexpr double MU_PZ90 = 3.9860044e14;
constexpr double A_E = 6378136.0;
constexpr double J2 = 1.0826257e-3;
constexpr double OMEGA = 7.292115e-5;
constexpr double STEP_S = 60.0;

// y is a 6-vector [x, y, z, vx, vy, vz]; acc_luni is the frame-constant
// luni-solar acceleration [ax, ay, az]. Writes the derivative to out[0..5].
void accel(const double y[6], const double acc_luni[3], double out[6]) {
    double x = y[0], yy = y[1], z = y[2];
    double r = std::sqrt(x * x + yy * yy + z * z);
    double mu_r3 = MU_PZ90 / (r * r * r);
    double zr = z / r;
    double zr2 = zr * zr;
    double aer = A_E / r;
    double j2f = 1.5 * J2 * mu_r3 * (aer * aer);
    double ax = -mu_r3 * x + j2f * x * (5.0 * zr2 - 1.0) + OMEGA * OMEGA * x
                + 2.0 * OMEGA * y[4] + acc_luni[0];
    double ay = -mu_r3 * yy + j2f * yy * (5.0 * zr2 - 1.0) + OMEGA * OMEGA * yy
                - 2.0 * OMEGA * y[3] + acc_luni[1];
    double az = -mu_r3 * z + j2f * z * (5.0 * zr2 - 3.0) + acc_luni[2];
    out[0] = y[3];
    out[1] = y[4];
    out[2] = y[5];
    out[3] = ax;
    out[4] = ay;
    out[5] = az;
}

void rk4_step(const double y[6], double dt, const double acc_luni[3],
              double out[6]) {
    double k1[6], k2[6], k3[6], k4[6], tmp[6];
    accel(y, acc_luni, k1);
    for (int i = 0; i < 6; ++i) tmp[i] = y[i] + 0.5 * dt * k1[i];
    accel(tmp, acc_luni, k2);
    for (int i = 0; i < 6; ++i) tmp[i] = y[i] + 0.5 * dt * k2[i];
    accel(tmp, acc_luni, k3);
    for (int i = 0; i < 6; ++i) tmp[i] = y[i] + dt * k3[i];
    accel(tmp, acc_luni, k4);
    for (int i = 0; i < 6; ++i)
        out[i] = y[i] + (dt / 6.0) * (k1[i] + 2.0 * k2[i] + 2.0 * k3[i] + k4[i]);
}
}  // namespace

// Propagate the GLONASS broadcast state in `e` to `t_gps`. Writes ECEF
// position (m) to pos3, ECEF velocity (m/s) to vel3, SV clock correction (s)
// to clk1. dt_total == 0 returns the broadcast state unchanged.
void glonass_state(const GloEph *e, double t_gps, double *pos3, double *vel3,
                   double *clk1) {
    double y[6] = {e->x_m, e->y_m, e->z_m, e->vx, e->vy, e->vz};
    double acc[3] = {e->ax, e->ay, e->az};
    double dt_total = t_gps - e->toe_ref;
    double step = (dt_total >= 0.0) ? STEP_S : -STEP_S;
    double remaining = dt_total;
    while (std::fabs(remaining) > 1e-9) {
        double h = (std::fabs(remaining) > std::fabs(step)) ? step : remaining;
        double next[6];
        rk4_step(y, h, acc, next);
        for (int i = 0; i < 6; ++i) y[i] = next[i];
        remaining -= h;
    }
    pos3[0] = y[0];
    pos3[1] = y[1];
    pos3[2] = y[2];
    vel3[0] = y[3];
    vel3[1] = y[4];
    vel3[2] = y[5];
    *clk1 = -e->tau + e->gamma * dt_total;
}

}  // namespace gs

// C-linkage from the declaration in abi.h (mirrors ephem.cpp's
// synth_sat_state_sys style -- no local extern "C" wrapper needed).
void synth_glonass_state(const GloEph *e, double t_gps, double *pos3,
                         double *vel3, double *clk1) {
    gs::glonass_state(e, t_gps, pos3, vel3, clk1);
}
