"""Weighted least-squares single-point positioning with explicit
weighting, full DOP metrics and a formal solution covariance.

This is a standalone solver used by the validation path. The historical
``receiver.solve_position`` (unweighted Gauss-Newton) is unchanged and
still used by ``receiver.fix_from_iq``; this module is what the error
budget and the validation CLI build on.
"""
from __future__ import annotations

import math

import numpy as np

from backend import config

_A = 6378137.0
_E2 = 6.69437999014e-3


def _enu_basis(ecef) -> np.ndarray:
    x, y, z = ecef
    lon = math.atan2(y, x)
    lat = math.atan2(z, math.hypot(x, y) * (1.0 - _E2))
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    e = [-so, co, 0.0]
    n = [-sl * co, -sl * so, cl]
    u = [cl * co, cl * so, sl]
    return np.array([e, n, u])


def elevation_weight(el_deg: float, sigma0_m: float = 3.0) -> float:
    """1/sigma^2 weight from an elevation-dependent measurement sigma
    ``sigma0 / sin(el)`` (clamped at 5 deg). Common, defensible default."""
    el = math.radians(max(el_deg, 5.0))
    sigma = sigma0_m / math.sin(el)
    return 1.0 / (sigma * sigma)


def solve(pseudoranges: dict, sat_positions: dict, *, weights: dict | None = None,
          x0=None, max_iter: int = 15) -> dict:
    """Weighted least-squares fix.

    ``pseudoranges``  PRN -> pseudorange (m), sat-clock already applied.
    ``sat_positions`` PRN -> ECEF (m).
    ``weights``       PRN -> w (1/sigma^2). Defaults to all 1.0 (== OLS).

    Returns ecef, clock_bias_s, per-PRN residuals, residual RMS,
    weighted RMS, DOP block, position covariance (ECEF and ENU), and the
    1-sigma horizontal / vertical formal errors.
    """
    prns = sorted(pseudoranges)
    if len(prns) < 4:
        raise ValueError(f"need >= 4 satellites, got {len(prns)}")
    S = np.array([sat_positions[p] for p in prns], float)
    pr = np.array([pseudoranges[p] for p in prns], float)
    w = np.array([1.0 if weights is None else weights[p] for p in prns], float)
    W = np.diag(w)

    X = np.zeros(4) if x0 is None else np.array(x0, float)
    it = 0
    for it in range(1, max_iter + 1):
        rng = np.linalg.norm(S - X[:3], axis=1)
        pred = rng + config.C * X[3]
        dz = pr - pred
        H = np.column_stack([(X[:3] - S) / rng[:, None],
                             config.C * np.ones(len(prns))])
        N = H.T @ W @ H
        dX = np.linalg.solve(N, H.T @ W @ dz)
        X += dX
        if np.linalg.norm(dX[:3]) < 1e-4:
            break

    rng = np.linalg.norm(S - X[:3], axis=1)
    resid = pr - (rng + config.C * X[3])
    H = np.column_stack([(X[:3] - S) / rng[:, None], config.C * np.ones(len(prns))])

    # unit-weight DOP (W = I): pure geometry
    Qxx = np.linalg.inv(H.T @ H)
    R = _enu_basis(X[:3])
    Qenu = R @ Qxx[:3, :3] @ R.T
    dop = {
        "gdop": float(math.sqrt(np.trace(Qxx))),
        "pdop": float(math.sqrt(np.trace(Qxx[:3, :3]))),
        "hdop": float(math.sqrt(Qenu[0, 0] + Qenu[1, 1])),
        "vdop": float(math.sqrt(Qenu[2, 2])),
        "tdop": float(math.sqrt(Qxx[3, 3])),
    }

    # a-priori formal covariance straight from the supplied weights
    # (weights are 1/sigma^2 in metres^-2, so this is in metres^2).
    dof = len(prns) - 4
    sigma0_sq = float(resid @ (w * resid) / dof) if dof > 0 else float("nan")
    cov = np.linalg.inv(H.T @ W @ H)
    cov_enu = R @ cov[:3, :3] @ R.T
    # a-posteriori variant, rescaled by the observed unit-weight variance
    cov_post_enu = cov_enu * sigma0_sq if dof > 0 else cov_enu

    return {
        "ecef": X[:3].tolist(),
        "clock_bias_s": float(X[3]),
        "iterations": it,
        "prns": prns,
        "residuals_m": {p: float(r) for p, r in zip(prns, resid)},
        "residual_rms_m": float(np.sqrt(np.mean(resid ** 2))),
        "weighted_residual_rms_m": float(np.sqrt(np.sum(w * resid ** 2) / np.sum(w))),
        "dop": dop,
        "dof": dof,
        "sigma0": float(math.sqrt(sigma0_sq)) if dof > 0 else None,
        "cov_ecef": cov[:3, :3].tolist(),
        "cov_enu": cov_enu.tolist(),
        "sigma_horizontal_m": float(math.sqrt(cov_enu[0, 0] + cov_enu[1, 1])),
        "sigma_vertical_m": float(math.sqrt(cov_enu[2, 2])),
        "sigma_horizontal_post_m": float(math.sqrt(cov_post_enu[0, 0] + cov_post_enu[1, 1])),
        "sigma_vertical_post_m": float(math.sqrt(cov_post_enu[2, 2])),
    }
