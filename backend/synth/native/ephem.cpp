// backend/synth/native/ephem.cpp
// Near-verbatim port of backend/geometry.py _orbit / _ecef_from_orbit /
// _bds_geo_ecef / sat_state. Bounded by tests/synth/test_ephem_parity.py
// (GPS parity) and tests/synth/test_keplerian_variants.py (per-system parity).
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

// Per-system Keplerian parameters, mirrors geometry.SYS_PARAMS. Indexed by the
// PROPAGATION sys-int passed to synth_sat_state_sys:
//   0 GPS/QZSS, 1 Galileo, 2 BeiDou MEO/IGSO, 3 BeiDou GEO.
// (This enum is unrelated to the code-generation `sys` int used elsewhere.)
// BeiDou shares Galileo's mu, so it shares the same f_rel = -2*sqrt(mu)/c^2.
struct SysP { double mu, wdot, frel; };
static const SysP kSysP[4] = {
    {gs::kMu,        gs::kOmegaEDot,  gs::kFRel},         // 0 GPS/QZSS
    {3.986004418e14, 7.2921151467e-5, -4.442807309e-10},  // 1 Galileo
    {3.986004418e14, 7.2921150e-5,    -4.442807309e-10},  // 2 BeiDou MEO/IGSO
    {3.986004418e14, 7.2921150e-5,    -4.442807309e-10},  // 3 BeiDou GEO
};

struct OrbitPt { double xp, yp, i, Omega, E; };

OrbitPt orbit(const KeplerEph *e, double tk, const SysP &sp) {
    double A = e->sqrtA * e->sqrtA;
    double n0 = std::sqrt(sp.mu / (A * A * A));
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
    double Omega = e->omega0 + (e->omega_dot - sp.wdot) * tk
                   - sp.wdot * e->toe;
    return {xp, yp, i, Omega, E};
}

void ecef(const OrbitPt &o, double *out3) {
    double cO = std::cos(o.Omega), sO = std::sin(o.Omega);
    double ci = std::cos(o.i), si = std::sin(o.i);
    out3[0] = o.xp * cO - o.yp * ci * sO;
    out3[1] = o.xp * sO + o.yp * ci * cO;
    out3[2] = o.yp * si;
}

// Port of geometry._bds_geo_ecef (BDS-SIS-ICD-B1I section 5.2.4.12). GEO
// satellites are propagated with the node kept at the full omega_dot*tk term
// (Omega_geo), then carried into CGCS2000 by Rz(omega_e_dot*tk) . Rx(-5 deg).
void bds_geo_ecef(const OrbitPt &o, double Omega_geo, double wdot_tk,
                  double *out3) {
    OrbitPt g = o;
    g.Omega = Omega_geo;
    double xyz[3];
    ecef(g, xyz);
    const double phi = -5.0 * M_PI / 180.0;
    double cx = std::cos(phi), sx = std::sin(phi);
    double x1 = xyz[0];
    double y1 = cx * xyz[1] + sx * xyz[2];
    double z1 = -sx * xyz[1] + cx * xyz[2];
    double cz = std::cos(wdot_tk), sz = std::sin(wdot_tk);
    out3[0] = cz * x1 + sz * y1;
    out3[1] = -sz * x1 + cz * y1;
    out3[2] = z1;
}

void propagate(const KeplerEph *e, const SysP &sp, bool geo, double tk,
               double *out3, double *E_out) {
    OrbitPt o = orbit(e, tk, sp);
    if (E_out) *E_out = o.E;
    if (geo) {
        double wdot_tk = sp.wdot * tk;
        bds_geo_ecef(o, o.Omega + wdot_tk, wdot_tk, out3);
    } else {
        ecef(o, out3);
    }
}
}  // namespace

// sys: 0 GPS/QZSS, 1 Galileo, 2 BeiDou MEO/IGSO, 3 BeiDou GEO. Selects mu /
// omega_e_dot / f_rel and (sys==3) the GEO reference-frame rotation.
void synth_sat_state_sys(const KeplerEph *e, int sys, double t_gps,
                         double *pos3, double *vel3, double *clk1) {
    if (sys < 0 || sys > 3) sys = 0;
    const SysP &sp = kSysP[sys];
    const bool geo = (sys == 3);

    double tk = t_gps - e->toe;
    if (tk > 302400.0) tk -= 604800.0;
    else if (tk < -302400.0) tk += 604800.0;

    double E = 0.0;
    propagate(e, sp, geo, tk, pos3, &E);

    const double dt = 0.5;
    double p0[3], p1[3];
    propagate(e, sp, geo, tk - dt, p0, nullptr);
    propagate(e, sp, geo, tk + dt, p1, nullptr);
    for (int k = 0; k < 3; ++k) vel3[k] = (p1[k] - p0[k]) / (2.0 * dt);

    double tsv = t_gps - e->toc;
    *clk1 = e->af0 + e->af1 * tsv + e->af2 * tsv * tsv
            + sp.frel * e->e * e->sqrtA * std::sin(E);
}

void synth_sat_state(const KeplerEph *e, double t_gps,
                     double *pos3, double *vel3, double *clk1) {
    synth_sat_state_sys(e, 0, t_gps, pos3, vel3, clk1);
}
