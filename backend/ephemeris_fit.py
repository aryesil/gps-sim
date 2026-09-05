# backend/ephemeris_fit.py
"""Fit a RINEX-2 broadcast ephemeris record to a precise (SP3) orbit/clock
track so that ``gps-sdr-sim`` -- which only ever consumes broadcast
Keplerian nav -- generates a signal whose satellite states match the
precise product.

This is the Strategy B addendum to the precise-ephemeris work: unlike
``ephemeris.align_epochs`` (which only stamps toc/toe and leaves M0 and
the perturbations untouched), the parameters here are *solved* against
sampled precise positions with Gauss-Newton, so the standard broadcast
propagation in ``backend/geometry._orbit`` -- the same algorithm
gps-sdr-sim's ``eph2pos`` implements -- reproduces the SP3 track to well
under a metre over a +/-1 h window.

The forward model is ``geometry._orbit`` / ``geometry._ecef_from_orbit``
verbatim; nothing about the propagation math is re-derived here, only the
inverse problem.
"""
from __future__ import annotations

import math

import numpy as np

from backend import config, geometry
from backend.gpstime import GPSTime

# 15 solved parameters. Order is fixed -- the Jacobian, the step scales and
# the fitted-eph assembly all index it.
_PARAMS = (
    "m0", "e", "sqrtA", "delta_n", "i0", "idot",
    "omega0", "omega_dot", "omega",
    "cuc", "cus", "crc", "crs", "cic", "cis",
)
# Typical magnitude of each parameter. Gauss-Newton runs in coordinates
# divided by this scale so every solved variable is O(1); without it the
# normal-equations matrix mixes sqrtA (~5e3) with delta_n (~5e-9) and the
# finite-difference Jacobian is hopelessly conditioned.
_SCALE = np.array([
    1.0, 1e-2, 5.0e3, 5e-9, 1.0, 1e-10,
    1.0, 8e-9, 1.0,
    1e-6, 1e-6, 1e2, 1e2, 1e-6, 1e-6,
])
# Finite-difference step in the normalised coordinates.
_DN = 1e-6

# Default fit arc: 4 h (toe +/- 2 h), the same interval the GPS control
# segment fits a broadcast record over. Shorter arcs (<= 2 h) leave the
# harmonic terms unobservable and the fit stalls hundreds of metres out;
# a real scenario is minutes long, so one 4 h fit covers every segment.
DEFAULT_WINDOW_S = 14400.0
DEFAULT_SAMPLES = 97
# A fit that cannot get under this is reported as a failure rather than
# silently feeding a bad nav file to signal generation.
DEFAULT_POS_TOL_M = 2.0


class EphemerisFitError(RuntimeError):
    """A precise->broadcast fit did not converge below the position
    tolerance (or a satellite had no usable precise samples)."""


def _eph_from_theta(theta: np.ndarray, toe: float, gps_week: int) -> dict:
    e = dict(zip(_PARAMS, (float(x) for x in theta)))
    e["toe"] = toe
    e["toc"] = toe
    e["gps_week"] = int(gps_week)
    return e


def _model_positions(theta: np.ndarray, tks: np.ndarray, toe: float,
                     gps_week: int) -> np.ndarray:
    """Vectorised copy of ``geometry._orbit`` + ``_ecef_from_orbit`` over an
    array of ``tk``. The scalar versions in ``backend/geometry`` remain the
    reference; this exists only so the fit's grid search and Jacobian run
    over ~100 epochs without a Python-level loop. The pure-Kepler test
    checks the two stay in agreement.
    """
    (m0, e, sqrtA, delta_n, i0, idot, omega0, omega_dot, omega,
     cuc, cus, crc, crs, cic, cis) = (float(v) for v in theta)
    A = sqrtA ** 2
    n = np.sqrt(config.MU / A ** 3) + delta_n
    M = m0 + n * tks
    E = M.copy()
    for _ in range(15):
        E = E - (E - e * np.sin(E) - M) / (1.0 - e * np.cos(E))
    sinE, cosE = np.sin(E), np.cos(E)
    nu = np.arctan2(np.sqrt(1.0 - e * e) * sinE, cosE - e)
    phi = nu + omega
    s2, c2 = np.sin(2.0 * phi), np.cos(2.0 * phi)
    u = phi + cus * s2 + cuc * c2
    r = A * (1.0 - e * cosE) + crs * s2 + crc * c2
    inc = i0 + idot * tks + cis * s2 + cic * c2
    xp, yp = r * np.cos(u), r * np.sin(u)
    Omega = omega0 + (omega_dot - config.OMEGA_E_DOT) * tks - config.OMEGA_E_DOT * toe
    cO, sO, ci, si = np.cos(Omega), np.sin(Omega), np.cos(inc), np.sin(inc)
    return np.column_stack([
        xp * cO - yp * ci * sO,
        xp * sO + yp * ci * cO,
        yp * si,
    ])


def _seed(pos: np.ndarray, vel_ecef: np.ndarray, toe: float) -> np.ndarray:
    """Osculating-element seed for Gauss-Newton.

    ``a``/``e``/``i`` come from the inertial state (ECEF velocity plus
    omega_E x r); the node longitude is taken directly in ECEF so it maps
    onto the broadcast ``omega0`` without an absolute sidereal angle.
    Perturbations seed to zero, ``omega_dot`` to a nominal GPS value.
    """
    we = np.array([0.0, 0.0, config.OMEGA_E_DOT])
    vel_in = vel_ecef + np.cross(we, pos)
    r = float(np.linalg.norm(pos))
    v = float(np.linalg.norm(vel_in))
    a = 1.0 / (2.0 / r - v * v / config.MU)
    sqrtA = math.sqrt(max(a, 1.0))

    h_in = np.cross(pos, vel_in)
    i0 = math.acos(max(-1.0, min(1.0, h_in[2] / np.linalg.norm(h_in))))
    evec = np.cross(vel_in, h_in) / config.MU - pos / r
    e = float(np.linalg.norm(evec))
    e = min(max(e, 1e-4), 0.05)

    h_ec = np.cross(pos, vel_ecef)
    node = np.cross([0.0, 0.0, 1.0], h_ec)
    nn = float(np.linalg.norm(node))
    if nn < 1e-6:
        node = np.array([1.0, 0.0, 0.0])
        nn = 1.0
    n_hat = node / nn
    h_hat = h_ec / np.linalg.norm(h_ec)
    m_hat = np.cross(h_hat, n_hat)
    u = math.atan2(float(pos @ m_hat), float(pos @ n_hat))
    lon_node = math.atan2(node[1], node[0])
    omega0 = lon_node + config.OMEGA_E_DOT * toe

    theta = np.zeros(len(_PARAMS))
    theta[_PARAMS.index("m0")] = u
    theta[_PARAMS.index("e")] = e
    theta[_PARAMS.index("sqrtA")] = sqrtA
    theta[_PARAMS.index("i0")] = i0
    theta[_PARAMS.index("omega0")] = omega0
    theta[_PARAMS.index("omega_dot")] = -0.75e-8
    return theta


def _fit_clock(sow: np.ndarray, clk: np.ndarray, toe: float) -> tuple:
    """Least-squares af0/af1/af2 for clk(t) ~ af0 + af1 dt + af2 dt^2."""
    dtv = sow - toe
    good = np.isfinite(clk)
    if good.sum() < 3:
        return 0.0, 0.0, 0.0, float("nan")
    A = np.vstack([np.ones(good.sum()), dtv[good], dtv[good] ** 2]).T
    coef, *_ = np.linalg.lstsq(A, clk[good], rcond=None)
    resid = float(np.max(np.abs(A @ coef - clk[good])))
    return float(coef[0]), float(coef[1]), float(coef[2]), resid


def evaluate_fit(eph: dict, state_fn, epoch: GPSTime, *,
                 window_s: float = DEFAULT_WINDOW_S,
                 n_dense: int = 241,
                 clock_terms=None) -> dict:
    """Dense, independent post-fit residual analysis.

    The optimiser in :func:`fit_satellite` minimises ``_model_positions``
    (its own vectorised Kepler copy) at 97 sample points. This function is
    the *separate* validator the audit requires: it propagates the fitted
    record with :func:`geometry.sat_state` -- the exact production path that
    feeds ``gps-sdr-sim`` and the receiver -- at ``n_dense`` points across
    the arc (offset from the fit grid), and reports the 3D error plus its
    radial / along-track / cross-track decomposition in the precise orbit
    frame.

    A fit is only trustworthy if it is small *here*, not merely where the
    optimiser looked.
    """
    toe = float(epoch.sow)
    half = window_s / 2.0
    # half a step in from each end, so the dense grid never coincides with
    # the fit grid's sample points.
    step = window_s / n_dense
    sow = toe - half + step / 2.0 + step * np.arange(n_dense)

    d3 = np.empty(n_dense)
    rac = np.empty((n_dense, 3))
    clk_err = np.empty(n_dense)
    for k, s in enumerate(sow):
        p_true, v_true, c_true = state_fn(float(s))
        p_true = np.asarray(p_true, float)
        v_true = np.asarray(v_true, float)
        p_fit, _, c_fit = geometry.sat_state(eph, float(s))
        dv = np.asarray(p_fit, float) - p_true
        d3[k] = float(np.linalg.norm(dv))
        r_hat = p_true / np.linalg.norm(p_true)
        c_vec = np.cross(p_true, v_true)
        c_hat = c_vec / np.linalg.norm(c_vec)
        a_hat = np.cross(c_hat, r_hat)
        rac[k] = (dv @ r_hat, dv @ a_hat, dv @ c_hat)
        clk_err[k] = c_fit - c_true

    def _stats(a):
        a = np.abs(a)
        return {
            "mean": float(a.mean()), "rms": float(np.sqrt(np.mean(a ** 2))),
            "max": float(a.max()),
            "p50": float(np.percentile(a, 50)),
            "p95": float(np.percentile(a, 95)),
            "p99": float(np.percentile(a, 99)),
        }

    return {
        "n_dense": int(n_dense), "window_s": float(window_s),
        "pos_3d_m": _stats(d3),
        "radial_m": _stats(rac[:, 0]),
        "along_track_m": _stats(rac[:, 1]),
        "cross_track_m": _stats(rac[:, 2]),
        "clock_s": _stats(clk_err),
        "range_impact_m": _stats(rac[:, 0]),          # radial ~ line-of-sight range error
        "pseudorange_impact_m": _stats(rac[:, 0] - config.C * clk_err),
    }


def fit_satellite(state_fn, epoch: GPSTime, *, prn: int, source: str,
                  window_s: float = DEFAULT_WINDOW_S,
                  n_samples: int = DEFAULT_SAMPLES,
                  pos_tol_m: float = DEFAULT_POS_TOL_M,
                  strict: bool = True) -> dict:
    """Fit one broadcast record to ``state_fn`` (a precise
    ``f(sow_seconds) -> (pos, vel, clk)``) centred on ``epoch``.

    Returns a parsed-ephemeris dict in the exact shape
    ``ephemeris.parse_rinex`` produces (so ``to_rinex2_nav`` consumes it),
    with an extra ``_fit`` sub-dict carrying the residual summary. Raises
    ``EphemerisFitError`` if the position fit stays above ``pos_tol_m``
    and ``strict`` is set.
    """
    toe = float(epoch.sow)
    half = window_s / 2.0
    sow = toe + np.linspace(-half, half, n_samples)
    tks = sow - toe

    pos = np.empty((n_samples, 3))
    vel = np.empty((n_samples, 3))
    clk = np.empty(n_samples)
    for k, s in enumerate(sow):
        p, v, c = state_fn(float(s))
        pos[k] = p
        vel[k] = v
        clk[k] = c
    if not np.isfinite(pos).all():
        raise EphemerisFitError(f"PRN {prn}: precise track has non-finite samples")

    mid = n_samples // 2
    theta = _seed(pos[mid], vel[mid], toe)

    # The osculating seed can be ~0.5 rad off in mean anomaly and node
    # longitude -- outside Gauss-Newton's linearisation range. A coarse
    # 2-D grid over just those two angles brings the seed close enough.
    im, io = _PARAMS.index("m0"), _PARAMS.index("omega0")
    grid = np.linspace(-0.8, 0.8, 33)
    best = None
    for dm in grid:
        for do in grid:
            t2 = theta.copy()
            t2[im] += dm
            t2[io] += do
            rr = (_model_positions(t2, tks, toe, epoch.week) - pos).reshape(-1)
            c = float(rr @ rr)
            if best is None or c < best[0]:
                best = (c, dm, do)
    theta[im] += best[1]
    theta[io] += best[2]

    # Solve in normalised coordinates x = theta / _SCALE.
    def residual(x: np.ndarray) -> np.ndarray:
        return (_model_positions(x * _SCALE, tks, toe, epoch.week) - pos).reshape(-1)

    x = theta / _SCALE
    r = residual(x)
    cost = float(r @ r)
    lam = 1e-3
    for _ in range(200):
        J = np.empty((r.size, len(_PARAMS)))
        for j in range(len(_PARAMS)):
            xp = x.copy()
            xp[j] += _DN
            xm = x.copy()
            xm[j] -= _DN
            J[:, j] = (residual(xp) - residual(xm)) / (2.0 * _DN)
        cscale = np.linalg.norm(J, axis=0)
        cscale[cscale < 1e-30] = 1.0
        improved = False
        for _ in range(20):
            # Levenberg-Marquardt solved on J directly (not J^T J): a +/-2 h
            # position arc leaves delta_n / idot / omega_dot and the harmonic
            # terms weakly separable, so J is near rank-deficient; stacking
            # sqrt(lam)*diag(colnorm) under J and least-squares-solving keeps
            # the condition number at sqrt of the normal-equations form and
            # damps the degenerate directions.
            aug = np.vstack([J, np.sqrt(lam) * np.diag(cscale)])
            rhs = np.concatenate([-r, np.zeros(len(_PARAMS))])
            step, *_ = np.linalg.lstsq(aug, rhs, rcond=None)
            cand = x + step
            rc = residual(cand)
            cc = float(rc @ rc)
            if cc < cost:
                x, r, cost = cand, rc, cc
                lam = max(lam * 0.3, 1e-14)
                improved = True
                break
            lam *= 5.0
        if not improved or cost < 1e-16:
            break
    theta = x * _SCALE

    pos_resid = np.linalg.norm(r.reshape(-1, 3), axis=1)
    max_pos = float(pos_resid.max())
    rms_pos = float(np.sqrt(np.mean(pos_resid ** 2)))

    # geometry.sat_state re-adds the relativistic eccentricity term from the
    # fitted orbit, so fit the clock polynomial to the precise bias with
    # that term (evaluated on the fitted orbit) already removed.
    e_fit = theta[_PARAMS.index("e")]
    sqrta_fit = theta[_PARAMS.index("sqrtA")]
    rel = np.array([
        config.F_REL * e_fit * sqrta_fit
        * math.sin(geometry._orbit(_eph_from_theta(theta, toe, epoch.week), float(tk))[4])
        for tk in tks])
    af0, af1, af2, clk_resid = _fit_clock(sow, clk - rel, toe)

    eph = _eph_from_theta(theta, toe, epoch.week)
    eph.update({
        "af0": af0, "af1": af1, "af2": af2,
        "tgd": 0.0, "iode": 0.0, "iodc": 0.0, "health": 0.0,
        "codes_l2": 0.0,
    })

    # Independent dense post-fit validation with the production propagator.
    dense = evaluate_fit(eph, state_fn, epoch, window_s=window_s)
    dense_max = dense["pos_3d_m"]["max"]

    # The gate is the dense max (production path over the whole arc), not the
    # optimiser's residual at its own 97 points -- a fit that is small only
    # where the optimiser looked is not trustworthy.
    if strict and dense_max > pos_tol_m:
        raise EphemerisFitError(
            f"PRN {prn}: precise->broadcast fit rejected -- dense post-fit "
            f"3D residual max = {dense_max:.2f} m (optimiser grid max "
            f"{max_pos:.2f} m), configured threshold = {pos_tol_m:.2f} m, "
            f"fit interval = +/-{window_s / 2:.0f} s, source = {source}")

    eph["_fit"] = {
        "source": source, "prn": prn,
        "window_s": float(window_s), "n_samples": int(n_samples),
        "pos_tol_m": float(pos_tol_m),
        "optimiser_grid_max_pos_resid_m": max_pos,
        "optimiser_grid_rms_pos_resid_m": rms_pos,
        "max_clock_resid_s": clk_resid,
        # dense, production-path residual analysis:
        "max_pos_resid_m": dense_max,
        "rms_pos_resid_m": dense["pos_3d_m"]["rms"],
        "dense": dense,
    }
    return eph


def build_precise_broadcast(provider, prns, epoch: GPSTime, *,
                            window_s: float = DEFAULT_WINDOW_S,
                            n_samples: int = DEFAULT_SAMPLES,
                            pos_tol_m: float = DEFAULT_POS_TOL_M,
                            strict: bool = True) -> tuple[dict, list]:
    """Fit every requested PRN present in the loaded precise product.

    PRNs absent from the product are skipped and named in the returned
    warning list -- never substituted with a broadcast record. Raises
    ``EphemerisFitError`` if not one PRN could be fitted.
    """
    have = set(provider.satellites())
    out: dict[int, dict] = {}
    warnings: list[str] = []
    fits: list[dict] = []
    for prn in sorted(prns):
        if prn not in have:
            warnings.append(f"PRN {prn} absent from precise product; omitted")
            continue
        state_fn = provider.state_fn(prn, week=epoch.week)
        eph = fit_satellite(state_fn, epoch, prn=prn, source=provider.product.source,
                            window_s=window_s, n_samples=n_samples,
                            pos_tol_m=pos_tol_m, strict=strict)
        out[prn] = eph
        fits.append(eph["_fit"])
    if not out:
        raise EphemerisFitError("no requested PRN is in the precise product")
    worst = max(f["max_pos_resid_m"] for f in fits)
    warnings.append(
        f"precise->broadcast fit: {len(out)} sat, worst {worst:.2f} m, "
        f"source {provider.product.source}")
    return out, warnings
