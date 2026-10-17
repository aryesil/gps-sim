"""Air-ready sample chunks: scale, saturate and pack in the producer process.

At tens of Msps the work between "generator output" and "bytes on the link"
(int16 -> float, level, saturate, int8/nibble pack) is hundreds of MB of numpy
traffic per second of signal. Done in the process that also runs the TX pump,
it fights the pump for the GIL; the board's DMA then runs dry for a few
milliseconds at a time and the DAC repeats stale samples (a CW at the carrier
for a GNSS receiver) while the host-side queue still looks healthy. Doing it
in the generator's worker process leaves the main process with nothing but
slicing bytes and handing them to libiio.

Only the 8-bit and 4-bit formats of the Tezuka backends are packed this way
(they are the ones that run at high rates); everything else keeps the complex
path through transmit.stream.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

PREVIEW_SAMPLES = 1 << 18      # excerpt kept for the spectrogram / acquisition tap


@dataclass(frozen=True)
class AirFormat:
    name: str                  # "cs8" or "cs4"
    dac_full_scale: float      # largest code the format carries
    level_offset_db: float     # added to the requested level (see ad9361_cs4)
    bytes_per_sample: int      # packed size of one complex sample (cs8 2, cs4 1)


CS8 = AirFormat("cs8", 127.0, 0.0, 2)
CS4 = AirFormat("cs4", 7.0, 10.0, 1)
_FORMATS = {"cs8": CS8, "cs4": CS4}


def format_by_name(name: str) -> AirFormat:
    return _FORMATS[name]


@dataclass
class AirChunk:
    """One packed chunk. ``data`` is uint8, ``n_samples`` complex samples."""
    data: np.ndarray
    n_samples: int
    clipped: int
    gain: float                # digital scale applied to the generator output
    bytes_per_sample: int
    preview: np.ndarray        # complex64 excerpt of the unscaled signal (UI taps)

    def __len__(self) -> int:  # transmit.stream and the UI tee count samples
        return self.n_samples


_STEP = 1 << 20                # components per slice: temporaries stay cache-sized
                               # (whole-chunk temporaries page-fault their way
                               # to several times the arithmetic cost)


def _flat(data) -> np.ndarray:
    """Interleaved I,Q,I,Q... view of raw ints or a complex array (no copy
    unless the input needs converting)."""
    a = np.asarray(data)
    if np.iscomplexobj(a):
        a = np.ascontiguousarray(a, dtype=np.complex64).view(np.float32)
    return a.reshape(-1)


def _pack_slice(x: np.ndarray, fmt: AirFormat) -> np.ndarray:
    """Interleaved float32 I,Q already scaled, rounded and inside +-full scale
    -> packed uint8 bytes (the layout the FPGA expects, see ad9361_cs8/cs4).
    ``x`` holds whole samples; cs4 needs an even sample count."""
    if fmt.name == "cs8":
        return x.astype(np.int8).view(np.uint8)
    i = x[0::2].astype(np.int8).view(np.uint8)
    q = x[1::2].astype(np.int8).view(np.uint8)
    return ((q & 0x0F) << 4) | (i & 0x0F)


_RMS_SAMPLES = 1 << 22        # samples used to measure the level (4 M: ~0.1 s at 30 Msps)


def _lut_codes(fmt: AirFormat, gain: float, src_dtype) -> tuple[np.ndarray, np.ndarray, int]:
    """Code (int8) and saturation flag (uint8) for every possible raw value:
    one table lookup per component replaces the float scale/clip/round chain,
    with the same float32 arithmetic so the codes are identical."""
    info = np.iinfo(src_dtype)
    v = np.arange(info.min, info.max + 1, dtype=np.int64).astype(np.float32)
    x = v * np.float32(gain)
    lim = np.float32(fmt.dac_full_scale)
    flag = (np.abs(x) > lim).astype(np.uint8)
    codes = np.rint(np.clip(x, -lim, lim)).astype(np.int8)
    return codes, flag, int(info.min)


def prepare(data, fmt: AirFormat, *, level_dbfs: float, tx_scale: float = 1.0,
            gain: float | None = None) -> AirChunk:
    """Scale ``data`` (raw interleaved ints or a complex array) to the format,
    saturate, quantise and pack it. ``gain`` is the digital scale to use; None
    measures it from this chunk (the session then reuses it for the rest of
    the stream, exactly like transmit.stream's level measurement)."""
    flat = _flat(data)
    n = flat.size // 2
    flat = flat[:2 * n]
    preview = (flat[:2 * PREVIEW_SAMPLES].astype(np.float32).view(np.complex64).copy()
               if n else np.zeros(0, np.complex64))
    if gain is None:
        total = 0.0
        m = min(n, _RMS_SAMPLES)
        for lo in range(0, 2 * m, _STEP):
            seg = flat[lo:min(lo + _STEP, 2 * m)].astype(np.float64)
            total += float(seg @ seg)               # sum of i^2 + q^2
        rms = float(np.sqrt(total / m)) if m else 0.0
        gain = (fmt.dac_full_scale * 10.0 ** ((level_dbfs + fmt.level_offset_db) / 20.0)
                / rms * tx_scale) if rms > 0.0 else tx_scale
    lim = np.float32(fmt.dac_full_scale)
    g32 = np.float32(gain)
    n_bytes = n * fmt.bytes_per_sample
    if fmt.name == "cs4" and n % 2:
        n_bytes += 1                                  # whole 16-bit words
    out = np.zeros(n_bytes, dtype=np.uint8)
    clipped = 0
    per_sample = fmt.bytes_per_sample
    lut = None
    if flat.dtype in (np.int16, np.int8):
        lut = _lut_codes(fmt, gain, flat.dtype)
    for lo in range(0, 2 * n, _STEP):
        if lut is not None:
            codes_t, flag_t, base = lut
            idx = flat[lo:lo + _STEP].astype(np.intp) - base if base else flat[lo:lo + _STEP].astype(np.intp)
            codes = codes_t[idx]
            fl = flag_t[idx]
            clipped += int(np.count_nonzero(fl[0::2] | fl[1::2]))
            if fmt.name == "cs8":
                packed = codes.view(np.uint8)
            else:
                packed = ((codes[1::2].view(np.uint8) & 0x0F) << 4) | (codes[0::2].view(np.uint8) & 0x0F)
            start = (lo // 2) * per_sample
            out[start:start + packed.size] = packed
            continue
        x = flat[lo:lo + _STEP].astype(np.float32)
        if g32 != 1.0:
            x *= g32
        clipped += int(np.count_nonzero((np.abs(x) > lim).reshape(-1, 2).any(axis=1)))
        np.clip(x, -lim, lim, out=x)
        np.rint(x, out=x)
        packed = _pack_slice(x, fmt)
        start = (lo // 2) * per_sample
        out[start:start + packed.size] = packed
    return AirChunk(data=out, n_samples=n, clipped=clipped, gain=float(gain),
                    bytes_per_sample=fmt.bytes_per_sample, preview=preview)


def air_format_for(kind: str, sample_rate: float) -> AirFormat | None:
    """The packed format a backend kind uses at this rate, or None when it has
    none (16-bit and every other SDR keep the complex path)."""
    from backend.rf.backends import BACKENDS
    backend = BACKENDS.get(kind)
    getter = getattr(backend, "air_format_for", None)
    if getter is None:
        return None
    try:
        return getter(sample_rate)
    except Exception:
        return None
