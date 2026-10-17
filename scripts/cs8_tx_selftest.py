"""TX complex 8-bit (cs8) self-test over the AD9361's internal digital loopback.

Needs a Tezuka LibreSDR booted with the CS8TX bitstream. No RF connection is
used: the chip's BIST loopback feeds the TX samples straight back into the RX
path, and the TX gain is muted.

    python scripts/cs8_tx_selftest.py --uri ip:192.168.3.1 --rates 4e6,8e6
    python scripts/cs8_tx_selftest.py --mode cs16      # baseline through the stock 16-bit path

Per rate it prints: tone frequency found, image rejection (the mirror tone at
-f; a dead Q path gives ~0 dB), SNR, tone level, and the achieved TX push rate
against the requested one (below ~0.98 the link cannot keep up).
"""
from __future__ import annotations

import argparse
import threading
import time

import numpy as np


def analyze(iq: np.ndarray, rate: float, tone_hz: float) -> dict:
    """Spectral figures of a captured complex block that should hold one tone."""
    n = len(iq)
    w = np.blackman(n)
    spec = np.abs(np.fft.fftshift(np.fft.fft((iq - iq.mean()) * w))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / rate))
    k = int(np.argmax(spec))
    guard = 8

    def band(f):
        i = int(np.argmin(np.abs(freqs - f)))
        return spec[max(i - guard, 0):i + guard + 1].sum()

    sig = band(freqs[k])
    img = band(-freqs[k])
    mask = np.ones(n, bool)
    for f in (freqs[k], -freqs[k]):
        i = int(np.argmin(np.abs(freqs - f)))
        mask[max(i - guard, 0):i + guard + 1] = False
    noise = spec[mask].mean() * (2 * guard + 1)
    db = lambda x: 10 * np.log10(max(x, 1e-30))
    return {
        "peak_hz": float(freqs[k]),
        "expected_hz": float(tone_hz),
        "image_rejection_db": db(sig) - db(img),
        "snr_db": db(sig) - db(noise),
        "rms": float(np.sqrt(np.mean(np.abs(iq) ** 2))),
    }


def verdict(r: dict, rate: float) -> bool:
    bin_hz = rate / 65536
    return (abs(r["peak_hz"] - r["expected_hz"]) <= 4 * bin_hz
            and r["image_rejection_db"] > 30 and r["snr_db"] > 20)


def _tone_block(n: int, start: int, rate: float, tone_hz: float, amp: float):
    t = (np.arange(n) + start) / rate
    return (amp * np.exp(2j * np.pi * tone_hz * t)).astype(np.complex64)


def run_rate(mode: str, uri: str, rate: float, seconds: float) -> dict:
    from backend.rf.backends import ad9361, ad9361_cs8
    be = ad9361_cs8 if mode == "cs8" else ad9361
    amp = 0.5 * be.DAC_FULL_SCALE
    block = 65536
    # coherent tone: integer number of cycles in the RX capture
    tone = round(rate / 8 / (rate / block)) * (rate / block)
    h = be.open_tx(uri, 1.57542e9, rate)
    res = {"rate": rate, "mode": mode}
    try:
        be.set_gain(h, 0, ad9361._MUTE_GAIN_DB)
        ad9361._set_rx_lo_powerdown(h, False)
        h._ctrl.debug_attrs["loopback"].value = "1"
        stop = threading.Event()
        pushed = [0]
        t0 = [time.monotonic()]

        def pump():
            pos = 0
            while not stop.is_set():
                b = _tone_block(block, pos, rate, tone, amp)
                be.write(h, [b])
                pos += block
                pushed[0] += block

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        time.sleep(1.0)                     # let the DMA fill and settle
        t0[0] = time.monotonic()
        p0 = pushed[0]
        h.rx_enabled_channels = [0]
        h.rx_buffer_size = block
        end = t0[0] + seconds
        captures = []
        while time.monotonic() < end:
            captures.append(np.asarray(h.rx()))
        dt = time.monotonic() - t0[0]
        res["push_ratio"] = (pushed[0] - p0) / dt / rate
        res.update(analyze(captures[-1], rate, tone))
        res["pass"] = verdict(res, rate) and res["push_ratio"] > 0.98
        stop.set()
        th.join(timeout=5)
    finally:
        try:
            h._ctrl.debug_attrs["loopback"].value = "0"
        except Exception:
            pass
        be.close(h)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="ip:192.168.3.1")
    ap.add_argument("--mode", choices=("cs8", "cs16"), default="cs8")
    ap.add_argument("--rates", default="4e6,8e6")
    ap.add_argument("--seconds", type=float, default=5.0)
    a = ap.parse_args()
    ok = True
    for r in (float(x) for x in a.rates.split(",")):
        try:
            res = run_rate(a.mode, a.uri, r, a.seconds)
        except Exception as ex:
            print(f"{a.mode} {r/1e6:.2f} Msps: ERROR {type(ex).__name__}: {ex}")
            ok = False
            continue
        ok &= res["pass"]
        print(f"{res['mode']} {r/1e6:5.2f} Msps  tone {res['peak_hz']/1e3:9.1f} kHz "
              f"(exp {res['expected_hz']/1e3:.1f})  image rej {res['image_rejection_db']:5.1f} dB  "
              f"SNR {res['snr_db']:5.1f} dB  rms {res['rms']:7.1f}  "
              f"push {res['push_ratio']*100:5.1f}%  {'PASS' if res['pass'] else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
