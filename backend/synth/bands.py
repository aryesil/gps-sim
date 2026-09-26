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
    fs_note: str = ""       # set when the requested rate had to be raised


def _signal_key(sig) -> str:
    return _KEY_BY_SIGNAL[sig]


def band_centre(band_id: str, sig_ids: list[str]) -> float:
    """RF centre of one output band. Normally the registry centre; when the
    band's signals sit on different carriers (BeiDou B1I with GPS/Galileo on
    L1) the centre moves to the midpoint so both fit in the smallest fs."""
    cs = [signals.SIGNALS[k].carrier_hz for k in sig_ids]
    if cs and max(cs) != min(cs):
        return 0.5 * (max(cs) + min(cs))
    if cs and band_id == "L1":
        return cs[0]                  # B1I alone: centre on its own carrier
    return full_band_registry()[band_id].centre_hz


def _band_fs(band_id: str, sig_ids: list[str], req) -> float:
    """Resolve the sample rate for one band: an explicit per-band override
    on the request (floored by the policy), else the policy default."""
    if band_id == "L1":
        fs = req.sample_rate
        if fs is not None and float(fs) < fs_policy.fs_min(sig_ids):
            # Too low for these signals (BeiDou B1I sharing the L1 output
            # with GPS/Galileo needs ~18.5 MHz to span both carriers): raise
            # it to the policy default rather than fail the run; plan_bands
            # reports the change.
            return fs_policy.default_fs(sig_ids)
        return fs_policy.validate_fs(fs, sig_ids)
    override = getattr(req, f"{band_id.lower()}_sample_rate", None)
    ks = range(-7, 7) if band_id in ("G1", "G2") else ()
    floor = fs_policy.band_floor(band_id, sig_ids, ks=ks)
    return max(float(override or 0.0), floor)


def full_band_registry() -> dict[str, "Band"]:
    """BAND_REGISTRY plus GLONASS's G2 (its own FDMA band id for L2OF,
    reached via signals.signals_for's L2->G2 alias rather than a
    user-facing band choice of its own). Single source of truth for
    anything that needs a band id's centre frequency / out_file, in or out
    of plan_bands. 1246.00 MHz is GLONASS's own L2 FDMA plan (k=0 channel,
    ICD L1/L2) -- an entirely different physical carrier from GPS L2C's
    1227.60 MHz, the same relationship G1's 1602.00 MHz has to GPS L1's
    1575.42 MHz."""
    reg = dict(BAND_REGISTRY)
    reg.setdefault("G2", Band("G2", 1_246_000_000.0, "gpssim_g2.bin"))
    return reg


def plan_bands(entries, req) -> list[BandPlan]:
    """Group ``constellation_multi`` entries into per-RF-band synthesis
    plans, one per entry in ``BAND_REGISTRY`` (L1 first -> ``gpssim.bin``
    back-compat). A band with no entries is omitted. GLONASS L2 rides its
    own ``G2`` FDMA band id, handled alongside ``G1``."""
    quant = _QUANT[req.sample_format]
    reg = full_band_registry()
    plans: list[BandPlan] = []
    for band_id, band in reg.items():
        group = [e for e in entries if e["signal_id"].band == band_id]
        if not group:
            continue
        sig_ids = sorted({_signal_key(e["signal_id"]) for e in group})
        fs = _band_fs(band_id, sig_ids, req)
        note = ""
        if (band_id == "L1" and req.sample_rate is not None
                and fs != float(req.sample_rate)):
            span = fs_policy.carrier_span_hz(sig_ids)
            why = (f"BeiDou B1I and GPS/Galileo L1 share one output spanning "
                   f"{span / 1e6:.1f} MHz" if span > 0.0 else
                   f"the selected signals need at least "
                   f"{fs_policy.fs_min(sig_ids) / 1e6:.3f} MHz")
            note = (f"sample rate raised from {float(req.sample_rate) / 1e6:g} "
                    f"to {fs / 1e6:g} MSPS: {why}")
        plans.append(BandPlan(band_id, band_centre(band_id, sig_ids), fs,
                              quant, group, band.out_file, note))
    return plans
