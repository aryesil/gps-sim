// backend/synth/native/ephem.cpp
// Near-verbatim port of backend/geometry.py _orbit / _ecef_from_orbit /
// sat_state. Bounded by tests/synth/test_ephem_parity.py (sub-mm parity).
#include "abi.h"
#include "constants.hpp"
#include <cmath>

namespace {
double keplerE(double m, double e) {
    double E = m;
    for (int i = 0; i < 30; ++i) {
        double dE = (E - e * std::sin(E) - m) / (1.0 - e * std::cos(E));
        E -= dE;
        if (std::fabs(dE) < 1e-13) break;
    }
    return E;
}

struct OrbitPt { double xp, yp, i, Omega, E; };

OrbitPt orbit(const KeplerEph *e, double tk) {
    double A = e->sqrtA * e->sqrtA;
    double n0 = std::sqrt(gs::kMu / (A * A * A));
    double n = n0 + e->delta_n;
    double M = e->m0 + n * tk;
    double E = keplerE(M, e->e);
    double sE = std::sin(E), cE = std::cos(E);
    double nu = std::atan2(std::sqrt(1.0 - e->e * e->e) * sE, cE - e->e);
    double phi = nu + e->omega;
    double s2 = std::sin(2.0 * phi), c2 = std::cos(2.0 * phi);
    double u = phi + e->cus * s2 + e->cuc * c2;
    double r = A * (1.0 - e->e * cE) + e->crs * s2 + e->crc * c2;
    double i = e->i0 + e->idot * tk + e->cis * s2 + e->cic * c2;
    double xp = r * std::cos(u);
    double yp = r * std::sin(u);
    double Omega = e->omega0 + (e->omega_dot - gs::kOmegaEDot) * tk
                   - gs::kOmegaEDot * e->toe;
    return {xp, yp, i, Omega, E};
}

void ecef(const OrbitPt &o, double *out3) {
    double cO = std::cos(o.Omega), sO = std::sin(o.Omega);
    double ci = std::cos(o.i), si = std::sin(o.i);
    out3[0] = o.xp * cO - o.yp * ci * sO;
    out3[1] = o.xp * sO + o.yp * ci * cO;
    out3[2] = o.yp * si;
}
}  // namespace

void synth_sat_state(const KeplerEph *e, double t_gps,
                     double *pos3, double *vel3, double *clk1) {
    double tk = t_gps - e->toe;
    if (tk > 302400.0) tk -= 604800.0;
    else if (tk < -302400.0) tk += 604800.0;

    OrbitPt o = orbit(e, tk);
    ecef(o, pos3);

    const double dt = 0.5;
    double p0[3], p1[3];
    ecef(orbit(e, tk - dt), p0);
    ecef(orbit(e, tk + dt), p1);
    for (int k = 0; k < 3; ++k) vel3[k] = (p1[k] - p0[k]) / (2.0 * dt);

    double tsv = t_gps - e->toc;
    *clk1 = e->af0 + e->af1 * tsv + e->af2 * tsv * tsv
            + gs::kFRel * e->e * e->sqrtA * std::sin(o.E);
}
