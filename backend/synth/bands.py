from __future__ import annotations

from dataclasses import dataclass

from backend.synth import fs_policy, signals

_QUANT = {"int8": 0, "int12": 1, "int16": 2}


@dataclass(frozen=True)
class Band:
    id: str
    centre_hz: float
    out_file: str


# Insertion order matters: L1 is emitted first as ``gpssim.bin`` (back-compat).
BAND_REGISTRY: dict[str, "Band"] = {
    "L1": Band("L1", 1_575_420_000.0, "gpssim.bin"),
    "G1": Band("G1", 1_602_000_000.0, "gpssim_g1.bin"),
    "L2": Band("L2", 1_227_600_000.0, "gpssim_l2.bin"),
    "L5": Band("L5", 1_176_450_000.0, "gpssim_l5.bin"),
}

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


def _band_fs(band_id: str, sig_ids: list[str], req) -> float:
    """Resolve the sample rate for one band: an explicit per-band override
    on the request (floored by the policy), else the policy default."""
    if band_id == "L1":
        return fs_policy.validate_fs(req.sample_rate, sig_ids)
    override = getattr(req, f"{band_id.lower()}_sample_rate", None)
    ks = range(-7, 7) if band_id in ("G1", "G2") else ()
    floor = fs_policy.band_floor(band_id, sig_ids, ks=ks)
    return max(float(override or 0.0), floor)


def plan_bands(entries, req) -> list[BandPlan]:
    """Group ``constellation_multi`` entries into per-RF-band synthesis
    plans, one per entry in ``BAND_REGISTRY`` (L1 first -> ``gpssim.bin``
    back-compat). A band with no entries is omitted. GLONASS L2 rides its
    own ``G2`` FDMA band id, handled alongside ``G1``."""
    quant = _QUANT[req.sample_format]
    reg = dict(BAND_REGISTRY)
    reg.setdefault("G2", Band("G2", 1_227_600_000.0, "gpssim_g2.bin"))
    plans: list[BandPlan] = []
    for band_id, band in reg.items():
        group = [e for e in entries if e["signal_id"].band == band_id]
        if not group:
            continue
        sig_ids = sorted({_signal_key(e["signal_id"]) for e in group})
        fs = _band_fs(band_id, sig_ids, req)
        plans.append(BandPlan(band_id, band.centre_hz, fs, quant, group,
                              band.out_file))
    return plans
