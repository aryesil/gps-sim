"""Host-side channel selection for the FPGA engine's capacity limit.

The engine serves ``nch`` channels at sample rate ``fs`` only if
``nch <= nch_max(fs)``: P = 12 pipes, one channel per pipe per clock, and
``T = max(4, ceil(nch / P))`` clocks per output sample, so ``fs <= f_eng / T``.

Elevation source: ``BandPlan.sv_el_deg`` (index-parallel to ``band.svs``),
filled by ``engine.plan`` from the same geometric elevation that drives the
mask and the static amplitude taper.  It is informational only; synthesis
output is unchanged.

Policy (``select_svs``): keep everything when it fits.  Otherwise drop the
lowest-elevation satellite first (ties: system code, then PRN), never taking
a constellation below ``min_per_system`` satellites while the limit allows.
A Galileo SV (E1B + E1C, same sys/prn) is one unit of two channels.  If the
protected floor alone exceeds the limit, the floor is relaxed and the
lowest-elevation SV overall is dropped (reason says so).
"""
from __future__ import annotations

import dataclasses
import math

F_ENG = 250e6
PIPES = 12
MAX_CH = 64
MIN_T = 4


@dataclasses.dataclass(frozen=True)
class Dropped:
    system: int          # engine system code (0 GPS, 3 BeiDou, 5 Galileo)
    prn: int
    elevation: float     # degrees (nan if unknown)
    reason: str
    channels: int = 1


@dataclasses.dataclass(frozen=True)
class ChannelPlan:
    kept: list
    dropped: list
    nch: int
    nch_max: int
    T: int
    fs_max: float        # max sample rate for ``nch`` channels


def nch_max(fs: float, f_eng: float = F_ENG, pipes: int = PIPES,
            max_ch: int = MAX_CH) -> int:
    """Largest channel count the engine can serve at sample rate ``fs``."""
    return int(min(max_ch, pipes * math.floor(f_eng / fs)))


def clocks_per_sample(nch: int, pipes: int = PIPES) -> int:
    return max(MIN_T, math.ceil(nch / pipes))


def fs_max(nch: int, f_eng: float = F_ENG, pipes: int = PIPES) -> float:
    """Highest sample rate at which ``nch`` channels are served."""
    return f_eng / clocks_per_sample(nch, pipes)


def select_svs(band, fs: float, f_eng: float = F_ENG,
               min_per_system: int = 3):
    """Return ``(kept_svspecs, dropped)`` fitting ``nch_max(fs, f_eng)``.

    ``kept`` keeps the original ``band.svs`` order.  See module docstring.
    """
    svs = list(band.svs)
    limit = nch_max(fs, f_eng)
    if len(svs) <= limit:
        return svs, []
    els = list(getattr(band, "sv_el_deg", None) or [])
    if len(els) != len(svs):
        raise ValueError("band has no per-SV elevation (sv_el_deg) but "
                         "channels must be trimmed")
    groups: dict = {}                      # (sys, prn) -> [indices]
    for i, sv in enumerate(svs):
        groups.setdefault((int(sv.sys), int(sv.prn)), []).append(i)
    el = {k: min(els[i] for i in v) for k, v in groups.items()}
    per_sys: dict = {}
    for (s, _p) in groups:
        per_sys[s] = per_sys.get(s, 0) + 1
    order = sorted(groups, key=lambda k: (el[k], k[0], k[1]))
    total = len(svs)
    drop: dict = {}

    def take(k, why):
        nonlocal total
        drop[k] = why
        total -= len(groups[k])
        per_sys[k[0]] -= 1

    for k in order:                        # pass 1: respect the floor
        if total <= limit:
            break
        if per_sys[k[0]] > min_per_system:
            take(k, f"lowest elevation, over capacity "
                    f"({limit} ch at fs={fs / 1e6:g} MHz)")
    for k in order:                        # pass 2: floor relaxed
        if total <= limit:
            break
        if k not in drop:
            take(k, f"capacity {limit} ch below min_per_system floor; "
                    "lowest elevation overall")
    dropped = [Dropped(k[0], k[1], el[k], drop[k], len(groups[k]))
               for k in order if k in drop]
    gone = {i for k in drop for i in groups[k]}
    return [sv for i, sv in enumerate(svs) if i not in gone], dropped


def plan_channels(band, fs: float | None = None, f_eng: float = F_ENG,
                  min_per_system: int = 3) -> ChannelPlan:
    """Selection result plus timing numbers (``fs`` defaults to ``band.fs``)."""
    fs = float(band.fs if fs is None else fs)
    kept, dropped = select_svs(band, fs, f_eng, min_per_system)
    n = len(kept)
    return ChannelPlan(kept, dropped, n, nch_max(fs, f_eng),
                       clocks_per_sample(n), fs_max(n, f_eng))
