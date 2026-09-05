"""Optional atmospheric propagation-delay models.

Disabled by default. When enabled these add a positive extra path delay
(metres) to the geometric range for a given line of sight, together with
diagnostic metadata. Nothing here is baked into ``geometry`` -- callers
opt in and add the delay exactly once.

Models
------
* ``klobuchar`` -- the IS-GPS-200 broadcast ionospheric model (L1), driven
  by the 8 alpha/beta coefficients a receiver would decode from subframe 4.
* ``saastamoinen`` -- Saastamoinen zenith tropospheric delay (hydrostatic +
  wet) from a standard atmosphere at the receiver height, with a
  ``1/sin(el)`` obliquity mapping. Good to a few cm above ~15 deg
  elevation; not a Niell/VMF1 mapping.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

_C = 299792458.0
_SC = 1.0 / math.pi          # semicircles per radian

# A plausible mid-latitude broadcast set (IS-GPS-200 worked example order:
# alpha0..3, beta0..3). Callers should pass the set decoded for the day.
DEFAULT_KLOBUCHAR_ALPHA = (1.02e-8, 2.24e-8, -1.19e-7, -1.19e-7)
DEFAULT_KLOBUCHAR_BETA = (9.83e4, 1.31e5, -6.55e4, -3.93e5)


@dataclass
class AtmosphereConfig:
    ionosphere: str = "off"          # "off" | "klobuchar"
    troposphere: str = "off"         # "off" | "saastamoinen"
    klobuchar_alpha: tuple = DEFAULT_KLOBUCHAR_ALPHA
    klobuchar_beta: tuple = DEFAULT_KLOBUCHAR_BETA
    # standard-atmosphere surface values for Saastamoinen
    pressure_hpa: float = 1013.25
    temperature_k: float = 288.15
    humidity: float = 0.5

    @property
    def enabled(self) -> bool:
        return self.ionosphere != "off" or self.troposphere != "off"

    @classmethod
    def from_dict(cls, d: dict | None) -> "AtmosphereConfig":
        if not d:
            return cls()
        f = {k: d[k] for k in (
            "ionosphere", "troposphere", "klobuchar_alpha", "klobuchar_beta",
            "pressure_hpa", "temperature_k", "humidity") if k in d}
        if "klobuchar_alpha" in f:
            f["klobuchar_alpha"] = tuple(f["klobuchar_alpha"])
        if "klobuchar_beta" in f:
            f["klobuchar_beta"] = tuple(f["klobuchar_beta"])
        cfg = cls(**f)
        for name, val in (("ionosphere", cfg.ionosphere), ("troposphere", cfg.troposphere)):
            if val not in {"off", "klobuchar", "saastamoinen"}:
                raise ValueError(f"atmosphere.{name}: unknown model {val!r}")
        if cfg.ionosphere == "saastamoinen" or cfg.troposphere == "klobuchar":
            raise ValueError("atmosphere: model assigned to the wrong layer")
        return cfg


def klobuchar_delay_m(alpha, beta, gps_sow: float,
                      rx_lat_rad: float, rx_lon_rad: float,
                      az_rad: float, el_rad: float) -> dict:
    """IS-GPS-200 20.3.3.5.2.5 ionospheric delay on L1, in metres.

    ``el_rad`` / ``az_rad`` are the satellite's elevation and azimuth at the
    receiver. Returns ``{delay_m, vertical_delay_m, iono_latitude_deg,
    slant_factor, period_s, model}``.
    """
    a0, a1, a2, a3 = alpha
    b0, b1, b2, b3 = beta
    el_sc = el_rad * _SC
    az = az_rad
    lat_u = rx_lat_rad * _SC
    lon_u = rx_lon_rad * _SC

    psi = 0.0137 / (el_sc + 0.11) - 0.022                       # earth-centred angle
    phi_i = lat_u + psi * math.cos(az)
    phi_i = max(-0.416, min(0.416, phi_i))
    lam_i = lon_u + psi * math.sin(az) / math.cos(phi_i * math.pi)
    phi_m = phi_i + 0.064 * math.cos((lam_i - 1.617) * math.pi)

    t = 4.32e4 * lam_i + gps_sow
    t = t % 86400.0
    if t < 0:
        t += 86400.0

    slant = 1.0 + 16.0 * (0.53 - el_sc) ** 3                    # obliquity factor F

    amp = a0 + phi_m * (a1 + phi_m * (a2 + phi_m * a3))
    amp = max(amp, 0.0)
    per = b0 + phi_m * (b1 + phi_m * (b2 + phi_m * b3))
    per = max(per, 72000.0)

    x = 2.0 * math.pi * (t - 50400.0) / per
    if abs(x) < 1.57:
        vert = 5e-9 + amp * (1 - x * x / 2.0 + x ** 4 / 24.0)
    else:
        vert = 5e-9
    delay_s = slant * vert
    return {
        "model": "klobuchar",
        "delay_m": delay_s * _C,
        "vertical_delay_m": vert * _C,
        "slant_factor": slant,
        "iono_latitude_deg": phi_m / _SC * 180.0 / math.pi,
        "period_s": per,
        "local_time_s": t,
    }


def saastamoinen_delay_m(el_rad: float, height_m: float,
                         pressure_hpa: float = 1013.25,
                         temperature_k: float = 288.15,
                         humidity: float = 0.5) -> dict:
    """Saastamoinen zenith tropospheric delay with a ``1/sin(el)`` mapping.

    Returns ``{delay_m, zenith_hydrostatic_m, zenith_wet_m, mapping,
    model}``. Below 3 deg elevation the mapping is clamped (the model is
    not valid there).
    """
    # height-scaled standard atmosphere if caller left the defaults
    h_km = max(height_m, 0.0) / 1000.0
    p = pressure_hpa * (1.0 - 2.26e-5 * height_m) ** 5.225 if pressure_hpa == 1013.25 else pressure_hpa
    t = temperature_k - 6.5 * h_km if temperature_k == 288.15 else temperature_k
    rh = humidity
    # saturation vapour pressure (hPa), Magnus form
    es = 6.108 * math.exp((17.15 * (t - 273.15)) / (t - 38.25))
    e = rh * es

    zhd = 0.0022768 * p / (1.0 - 0.00266 * math.cos(0.0) - 0.00028 * h_km)
    zwd = 0.0022768 * (1255.0 / t + 0.05) * e

    el = max(el_rad, math.radians(3.0))
    m = 1.0 / math.sin(el)
    delay = (zhd + zwd) * m
    return {
        "model": "saastamoinen",
        "delay_m": delay,
        "zenith_hydrostatic_m": zhd,
        "zenith_wet_m": zwd,
        "mapping": m,
        "surface_pressure_hpa": p,
        "surface_temperature_k": t,
    }


def delays_for_los(cfg: AtmosphereConfig, gps_sow: float,
                   rx_lat_rad: float, rx_lon_rad: float, rx_height_m: float,
                   az_rad: float, el_rad: float) -> dict:
    """Total extra one-way path delay (m) for one line of sight, plus a
    per-model breakdown. ``{total_m, ionosphere{...}|None,
    troposphere{...}|None}``. Each model contributes at most once.
    """
    out = {"total_m": 0.0, "ionosphere": None, "troposphere": None}
    if cfg.ionosphere == "klobuchar":
        iono = klobuchar_delay_m(cfg.klobuchar_alpha, cfg.klobuchar_beta,
                                 gps_sow, rx_lat_rad, rx_lon_rad, az_rad, el_rad)
        out["ionosphere"] = iono
        out["total_m"] += iono["delay_m"]
    if cfg.troposphere == "saastamoinen":
        tropo = saastamoinen_delay_m(el_rad, rx_height_m, cfg.pressure_hpa,
                                     cfg.temperature_k, cfg.humidity)
        out["troposphere"] = tropo
        out["total_m"] += tropo["delay_m"]
    return out
