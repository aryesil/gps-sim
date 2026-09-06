"""Glue between the optional physical-channel / receiver models
(``backend.models.atmosphere``, ``backend.models.receiver_clock``, ``backend.models.multipath``)
and the code that consumes them (``/api/preview``, ``backend.analysis.truth`` and,
for the opt-in IQ post-processing, ``backend.generator``).

The three model modules are deliberately standalone and RNG-free. This
module only parses request dicts into their configs and turns an epoch
into the handful of numbers ``geometry.observables`` accepts. Nothing
here is enabled unless a request carries a non-"off" model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from backend.models import atmosphere, multipath, receiver_clock


@dataclass
class ChannelModels:
    atmosphere: atmosphere.AtmosphereConfig
    receiver_clock: receiver_clock.ReceiverClockConfig
    multipath: multipath.MultipathConfig

    @property
    def any_enabled(self) -> bool:
        return (self.atmosphere.enabled or self.receiver_clock.enabled
                or self.multipath.enabled)

    @classmethod
    def from_request(cls, req_or_body) -> "ChannelModels":
        """Accepts a ScenarioRequest or a raw request-body dict."""
        def _get(name):
            if isinstance(req_or_body, dict):
                return req_or_body.get(name)
            return getattr(req_or_body, name, None)
        return cls(
            atmosphere=atmosphere.AtmosphereConfig.from_dict(_get("atmosphere")),
            receiver_clock=receiver_clock.ReceiverClockConfig.from_dict(
                _get("receiver_clock")),
            multipath=multipath.MultipathConfig.from_dict(_get("multipath")),
        )

    # --- observable-level effects (metadata / preview / truth) ----------
    def observable_args(self, gps_sow: float, rx_lat_deg: float,
                        rx_lon_deg: float, rx_height_m: float) -> dict:
        """``{atmo_delay_fn, rx_clock_range_m, mp_code_bias_m}`` ready to
        splat into ``geometry.observables`` / ``geometry.constellation``."""
        atmo_fn = None
        if self.atmosphere.enabled:
            lat = math.radians(rx_lat_deg)
            lon = math.radians(rx_lon_deg)
            cfg = self.atmosphere

            def atmo_fn(az_rad, el_rad, _cfg=cfg, _sow=gps_sow, _lat=lat,
                        _lon=lon, _h=rx_height_m):
                return atmosphere.delays_for_los(
                    _cfg, _sow, _lat, _lon, _h, az_rad, el_rad)["total_m"]

        rx_clock_range_m = receiver_clock.state(
            self.receiver_clock, gps_sow)["range_bias_m"]
        mp_code_bias_m = multipath.tracking_bias(
            self.multipath, 0.0)["code_bias_m"]
        return {
            "atmo_delay_fn": atmo_fn,
            "rx_clock_range_m": float(rx_clock_range_m),
            "mp_code_bias_m": float(mp_code_bias_m),
        }

    def summary(self, gps_sow: float, rx_lat_deg: float, rx_lon_deg: float,
                rx_height_m: float, sample_az_el=(180.0, 30.0)) -> dict:
        """Human-facing one-epoch breakdown for the UI. ``sample_az_el`` is
        the az/el (deg) the zenith-scaled atmospheric delay is reported
        at -- a mid-sky reference, not any particular satellite."""
        out = {
            "ionosphere_model": self.atmosphere.ionosphere,
            "troposphere_model": self.atmosphere.troposphere,
            "receiver_clock_model": self.receiver_clock.model,
            "multipath_model": self.multipath.model,
            "any_enabled": self.any_enabled,
        }
        if self.atmosphere.enabled:
            az = math.radians(sample_az_el[0])
            el = math.radians(sample_az_el[1])
            d = atmosphere.delays_for_los(
                self.atmosphere, gps_sow, math.radians(rx_lat_deg),
                math.radians(rx_lon_deg), rx_height_m, az, el)
            out["atmosphere_sample_el_deg"] = sample_az_el[1]
            out["ionosphere_delay_m"] = (
                d["ionosphere"]["delay_m"] if d["ionosphere"] else 0.0)
            out["troposphere_delay_m"] = (
                d["troposphere"]["delay_m"] if d["troposphere"] else 0.0)
        if self.receiver_clock.enabled:
            st = receiver_clock.state(self.receiver_clock, gps_sow)
            out["receiver_clock_offset_s"] = st["clock_offset_s"]
            out["receiver_clock_range_bias_m"] = st["range_bias_m"]
            out["receiver_clock_carrier_offset_hz"] = st["carrier_offset_hz"]
        if self.multipath.enabled:
            tb = multipath.tracking_bias(self.multipath, 0.0)
            out["multipath_n_reflections"] = tb["n_reflections"]
            out["multipath_code_bias_m"] = tb["code_bias_m"]
            out["multipath_carrier_bias_m"] = tb["carrier_bias_m"]
        return out
