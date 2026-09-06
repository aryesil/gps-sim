from __future__ import annotations

from dataclasses import dataclass

from backend.synth import fs_policy, signals

_QUANT = {"int8": 0, "int12": 1, "int16": 2}

_L1_CENTRE_HZ = 1_575_420_000.0
_G1_CENTRE_HZ = 1_602_000_000.0

# reverse map: Signal instance -> its SIGNALS key (Signal is a frozen dataclass)
_KEY_BY_SIGNAL = {sig: key for key, sig in signals.SIGNALS.items()}


@dataclass
class BandPlan:
    id: str
    centre_hz: float
    fs: float
    quant: int
    entries: list
    out_file: str


def _signal_key(sig) -> str:
    return _KEY_BY_SIGNAL[sig]


def plan_bands(entries, req) -> list[BandPlan]:
    """Group ``constellation_multi`` entries into per-RF-band synthesis plans.

    L1 (1575.42 MHz) is emitted first as ``gpssim.bin`` (back-compat); the
    GLONASS G1 FDMA band (1602 MHz) follows as ``gpssim_g1.bin``. A band with
    no entries is omitted.
    """
    # quant selects the on-wire scale/width: int8->0, int12->1, int16->2 --
    # the same lookup the Phase-1 run used (RunSpec.quant = _QUANT[fmt]).
    quant = _QUANT[req.sample_format]
    l1 = [e for e in entries if e["signal_id"].band == "L1"]
    g1 = [e for e in entries if e["signal_id"].band == "G1"]

    plans: list[BandPlan] = []
    if l1:
        sig_ids = sorted({_signal_key(e["signal_id"]) for e in l1})
        fs = fs_policy.validate_fs(req.sample_rate, sig_ids)
        floor = fs_policy.band_floor("L1", sig_ids)
        if fs < floor:                       # validate_fs already guards, belt+braces
            raise ValueError(f"L1 sample_rate {fs:.0f} < floor {floor:.0f}")
        plans.append(BandPlan("L1", _L1_CENTRE_HZ, fs, quant, l1, "gpssim.bin"))
    if g1:
        floor = fs_policy.band_floor("G1", ["GLO_G1"], ks=range(-7, 7))
        fs = max(getattr(req, "g1_sample_rate", None) or 0.0, floor)
        plans.append(BandPlan("G1", _G1_CENTRE_HZ, fs, quant, g1, "gpssim_g1.bin"))
    return plans
