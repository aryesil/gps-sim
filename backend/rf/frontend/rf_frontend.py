"""Pure LO-offset planning for the AD936x TX RF frontend.

No hardware access here -- see backend/rf/frontend/capabilities.py and
calibration.py for that. `plan()` turns a user-facing target RF frequency
plus an offset strategy into the pair a real device needs (tx_lo_hz fed to
dual_tx.acquire()'s lo_hz, baseband_offset_hz fed to transmit.TxParams for
the streaming NCO), always satisfying:

    target_rf_hz == tx_lo_hz + baseband_offset_hz

Sign convention (matches the feature request's own worked examples):
a positive lo_offset_hz means "place the LO above the target RF frequency,
recover the target by mixing the baseband content down" --
tx_lo_hz = target + lo_offset_hz, baseband_offset_hz = -lo_offset_hz.
"""
from __future__ import annotations

from dataclasses import dataclass

# Magnitude(s) tried by AUTO mode, in Hz. Config-driven (not hard-coded into
# the algorithm) so a deployment can widen/narrow the search without a code
# change; -candidate is tried before +candidate for each magnitude, so the
# request's own "AUTO ... Offset: -1 MHz" worked example is AUTO's actual
# first try, not a coincidence.
_AUTO_OFFSET_CANDIDATES_HZ: tuple[int, ...] = (1_000_000,)

# GPS L1 C/A occupied bandwidth (main lobe, chip-rate-derived): 2 * CA_CHIP_HZ.
_DEFAULT_SIGNAL_BANDWIDTH_HZ = 2_046_000


class RFFrontendError(Exception):
    pass


@dataclass
class RFFrontendConfig:
    target_rf_frequency_hz: int
    lo_offset_mode: str = "DISABLED"        # "DISABLED" | "AUTO" | "MANUAL"
    lo_offset_hz: int = 0                   # used verbatim in MANUAL; ignored otherwise
    tx_rf_bandwidth_hz: int | None = None    # AD936x analog TX filter bandwidth, if set
    tx_sample_rate_hz: int = 0
    signal_bandwidth_hz: int = _DEFAULT_SIGNAL_BANDWIDTH_HZ


@dataclass
class RFPlan:
    target_rf_hz: int
    tx_lo_hz: int
    baseband_offset_hz: int


def _validate_offset(baseband_offset_hz: int, cfg: RFFrontendConfig) -> None:
    if cfg.tx_sample_rate_hz <= 0:
        raise RFFrontendError(
            "tx_sample_rate_hz must be set and > 0 to validate an LO offset")
    nyquist = cfg.tx_sample_rate_hz / 2.0
    if abs(baseband_offset_hz) >= nyquist:
        raise RFFrontendError(
            f"baseband offset {baseband_offset_hz} Hz meets or exceeds "
            f"Nyquist ({nyquist:.0f} Hz) for sample rate "
            f"{cfg.tx_sample_rate_hz} Hz")
    if cfg.tx_rf_bandwidth_hz:
        half_bw = cfg.tx_rf_bandwidth_hz / 2.0
        half_sig = cfg.signal_bandwidth_hz / 2.0
        if abs(baseband_offset_hz) + half_sig > half_bw:
            raise RFFrontendError(
                f"baseband offset {baseband_offset_hz} Hz plus half the "
                f"signal bandwidth ({half_sig:.0f} Hz) exceeds half the "
                f"configured TX RF bandwidth ({half_bw:.0f} Hz of "
                f"{cfg.tx_rf_bandwidth_hz} Hz)")


def _plan_for_lo_offset(target: int, lo_offset_hz: int, cfg: RFFrontendConfig) -> RFPlan:
    baseband_offset_hz = -lo_offset_hz
    _validate_offset(baseband_offset_hz, cfg)
    return RFPlan(target_rf_hz=target, tx_lo_hz=target + lo_offset_hz,
                  baseband_offset_hz=baseband_offset_hz)


def plan(cfg: RFFrontendConfig) -> RFPlan:
    target = int(cfg.target_rf_frequency_hz)
    mode = cfg.lo_offset_mode.upper()

    if mode == "DISABLED":
        result = RFPlan(target_rf_hz=target, tx_lo_hz=target, baseband_offset_hz=0)
    elif mode == "MANUAL":
        result = _plan_for_lo_offset(target, int(cfg.lo_offset_hz), cfg)
    elif mode == "AUTO":
        result = None
        last_error: Exception | None = None
        for magnitude in _AUTO_OFFSET_CANDIDATES_HZ:
            for lo_offset_hz in (-magnitude, magnitude):
                try:
                    result = _plan_for_lo_offset(target, lo_offset_hz, cfg)
                except RFFrontendError as ex:
                    last_error = ex
                    continue
                break
            if result is not None:
                break
        if result is None:
            raise RFFrontendError(
                "AUTO lo_offset_mode found no valid offset for "
                f"sample_rate={cfg.tx_sample_rate_hz} Hz, "
                f"tx_rf_bandwidth={cfg.tx_rf_bandwidth_hz} Hz among "
                f"candidates {_AUTO_OFFSET_CANDIDATES_HZ}"
            ) from last_error
    else:
        raise RFFrontendError(f"unknown lo_offset_mode {cfg.lo_offset_mode!r}")

    if result.tx_lo_hz + result.baseband_offset_hz != result.target_rf_hz:
        raise RFFrontendError(
            "internal invariant violated: "
            f"tx_lo_hz({result.tx_lo_hz}) + baseband_offset_hz("
            f"{result.baseband_offset_hz}) != target_rf_hz({result.target_rf_hz})")
    return result
