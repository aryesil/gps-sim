"""Per-satellite pseudorange error budget.

Collects the individual error contributions the simulator can actually
account for -- the ones driven by the models in this package plus a set
of conservative nominal 1-sigma figures for effects the software-only
stack does not model in detail -- into a machine-readable block for
meta.json.

Every value is a one-sigma range-equivalent in metres. ``total_m`` is
the root-sum-square of the components (they are treated as independent);
``modelled`` marks whether a component came from a model in this run or
from a nominal constant.

Nominal 1-sigma defaults (broadcast navigation, single frequency):
    orbit (broadcast)      0.8 m
    satellite clock        0.3 m   (after af0/af1/af2)
    ionosphere (residual)  4.0 m   when no iono model is applied
    troposphere (residual) 0.2 m   when no tropo model is applied
    multipath              0.5 m   open-sky nominal
    receiver thermal noise 0.5 m   C/N0 ~ 45 dB-Hz, narrow correlator
These are documentation-grade, not a calibration of this simulator.
"""
from __future__ import annotations

import math

_NOMINAL = {
    "orbit": 0.8,
    "satellite_clock": 0.3,
    "ionosphere": 4.0,
    "troposphere": 0.2,
    "multipath": 0.5,
    "receiver_thermal": 0.5,
    "sagnac": 0.0,          # applied exactly in geometry; no residual
}


def budget_for_prn(prn: int, *, elevation_deg: float | None = None,
                   iono_delay_m: float | None = None,
                   tropo_delay_m: float | None = None,
                   multipath_bias_m: float | None = None,
                   receiver_clock_bias_m: float | None = None,
                   overrides: dict | None = None) -> dict:
    """One PRN's contributions. A supplied delay/bias is entered as a
    modelled 1-sigma of ``0.1 * |delay|`` (residual after applying the
    model) rather than the full delay, since the model removes the bulk
    of it. ``overrides`` replaces nominal sigmas by name."""
    nom = dict(_NOMINAL)
    if overrides:
        nom.update(overrides)

    comps: dict[str, dict] = {}

    def put(name, sigma, modelled):
        comps[name] = {"sigma_m": float(sigma), "modelled": bool(modelled)}

    put("orbit", nom["orbit"], False)
    put("satellite_clock", nom["satellite_clock"], False)

    if iono_delay_m is not None:
        put("ionosphere", 0.1 * abs(iono_delay_m), True)
    else:
        put("ionosphere", nom["ionosphere"], False)

    if tropo_delay_m is not None:
        put("troposphere", 0.1 * abs(tropo_delay_m), True)
    else:
        put("troposphere", nom["troposphere"], False)

    if multipath_bias_m is not None:
        put("multipath", abs(multipath_bias_m), True)
    else:
        put("multipath", nom["multipath"], False)

    put("receiver_thermal", nom["receiver_thermal"], False)
    put("sagnac", nom["sagnac"], False)

    if receiver_clock_bias_m is not None:
        # common to all PRNs; absorbed by the clock state in the fix, so
        # it does not enter the per-PRN RSS -- reported for transparency
        comps["receiver_clock_common"] = {
            "sigma_m": float(abs(receiver_clock_bias_m)),
            "modelled": True, "in_rss": False}

    rss = math.sqrt(sum(c["sigma_m"] ** 2 for k, c in comps.items()
                        if c.get("in_rss", True)))
    return {
        "prn": prn,
        "elevation_deg": elevation_deg,
        "components": comps,
        "total_m": rss,
    }


def summarize(per_prn: list[dict]) -> dict:
    """Constellation-level roll-up for meta.json."""
    if not per_prn:
        return {"n_sats": 0, "per_prn": [], "uere_rms_m": None}
    totals = [b["total_m"] for b in per_prn]
    return {
        "n_sats": len(per_prn),
        "per_prn": per_prn,
        "uere_rms_m": math.sqrt(sum(t * t for t in totals) / len(totals)),
        "uere_max_m": max(totals),
        "note": "1-sigma range-equivalent, components assumed independent; "
                "nominal values are documentation-grade, not a calibration.",
    }
