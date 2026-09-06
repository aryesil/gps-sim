# backend/ephemeris_source.py
"""Pick the satellite-state source for an *analysis* query: broadcast or precise.

This is the single place that decides, per request, whether geometry /
verification uses the broadcast (realigned) ephemeris that actually drives
the generated IQ, or a precise SP3 reference (backend/precise.py).

Hard rule (docs/precise-ephemeris-design.md, Phase 7): ``precise`` never
silently degrades to ``broadcast``. If precise data is unavailable for the
epoch, the request fails -- unless the caller *explicitly* opts in with
``fallback_to_broadcast=True``, in which case the fallback is reported in
the returned warnings and in every state's ``source`` string.

Nothing here touches signal generation. gps-sdr-sim is broadcast-only.
"""
from __future__ import annotations

from typing import Callable, Literal

from backend.gpstime import GPSTime
from backend import precise as _precise

EphemerisMode = Literal["broadcast", "precise"]
VALID_MODES: tuple[str, ...] = ("broadcast", "precise")


class EphemerisModeError(ValueError):
    """Invalid mode, or precise requested with no usable precise product."""


def normalise_mode(mode: str | None) -> EphemerisMode:
    m = (mode or "broadcast").strip().lower()
    if m not in VALID_MODES:
        raise EphemerisModeError(
            f"ephemeris_mode must be one of {VALID_MODES}, got {mode!r}")
    return m  # type: ignore[return-value]


def build_state_fns(
    mode: str | None,
    prns: list[int],
    epoch_gps: GPSTime,
    broadcast_eph_by_prn: dict[int, dict],
    *,
    provider: "_precise.PreciseEphemerisProvider | None" = None,
    fallback_to_broadcast: bool = False,
    on_missing: str = "error",
    order: int | None = None,
    allow_boundary: bool = False,
) -> tuple[dict[int, Callable | dict], list[str]]:
    """Return ``({prn: eph_or_state_fn}, warnings)`` ready for
    ``geometry.constellation`` / ``geometry.observables``.

    * ``broadcast`` -> the parsed broadcast dicts, untouched.
    * ``precise``   -> ``provider.state_fn(prn)`` per PRN. A PRN missing from
      the SP3 product, or an epoch outside coverage, raises
      ``EphemerisModeError`` unless ``fallback_to_broadcast`` is set, in
      which case that PRN falls back and a warning is recorded.
    """
    m = normalise_mode(mode)
    if m == "broadcast":
        return {p: broadcast_eph_by_prn[p] for p in prns if p in broadcast_eph_by_prn}, \
               ["ephemeris: broadcast"]

    if provider is None or not provider.loaded:
        raise EphemerisModeError(
            "ephemeris_mode='precise' but no precise (SP3) product is loaded -- "
            "POST /api/precise/load first")

    # Coverage gate up front: a clear whole-request failure beats a
    # per-PRN scatter of errors when the epoch is simply out of span.
    cov_lo, cov_hi = provider.coverage()
    if not (cov_lo.seconds <= epoch_gps.seconds <= cov_hi.seconds):
        if not fallback_to_broadcast:
            raise EphemerisModeError(
                f"epoch outside precise product coverage "
                f"[{cov_lo.to_datetime().isoformat()}Z .. "
                f"{cov_hi.to_datetime().isoformat()}Z]")
        return {p: broadcast_eph_by_prn[p] for p in prns if p in broadcast_eph_by_prn}, \
               [f"ephemeris: broadcast (FELL BACK -- epoch outside precise coverage "
                f"{cov_lo.to_datetime().isoformat()}Z..{cov_hi.to_datetime().isoformat()}Z)"]

    have = {p for (s, p) in provider.satellites() if s == "G"}
    out: dict[int, Callable | dict] = {}
    warnings: list[str] = [f"ephemeris: precise ({provider.product.source})"]
    fell_back: list[int] = []
    skipped: list[int] = []
    for p in prns:
        if p in have:
            out[p] = provider.state_fn(p, week=epoch_gps.week, order=order,
                                       allow_boundary=allow_boundary)
        elif fallback_to_broadcast and p in broadcast_eph_by_prn:
            out[p] = broadcast_eph_by_prn[p]           # explicit opt-in only
            fell_back.append(p)
        elif on_missing == "skip":
            skipped.append(p)                          # absent from precise analysis, not substituted
        else:
            raise EphemerisModeError(
                f"PRN {p} is not in the precise product; pass "
                f"fallback_to_broadcast=true for a broadcast substitute, or "
                f"restrict the PRN set")
    if fell_back:
        warnings.append(f"broadcast fallback for PRN {fell_back} (not in precise product)")
    if skipped:
        warnings.append(f"{len(skipped)} PRN(s) not in precise product, omitted: {skipped}")
    return out, warnings


def build_precise_state_fns(provider, keys, week):
    """Build ``f(sow) -> (pos_ecef, vel_ecef, clk_bias_s)`` interpolants from a
    precise (SP3) ``provider`` for a collection of constellation ``keys``.

    ``keys`` are ``(sysc, prn)`` tuples (a bare int is treated as ``("G", prn)``).
    Returns ``({key: fn}, skipped)`` where ``skipped`` lists the keys the precise
    product does not cover for ``week`` -- a not-covered key is swallowed and
    surfaced in the list, never raised. Callers decide whether a given skipped
    key is a hard failure (REQUIRED system) or an acceptable degrade.
    """
    try:
        have = {tuple(sp) for sp in provider.satellites()}
    except Exception:
        have = set()
    out: dict = {}
    skipped: list = []
    for key in keys:
        nk = ("G", key) if isinstance(key, int) else tuple(key)
        if nk not in have:
            skipped.append(key)
            continue
        try:
            out[key] = provider.state_fn(key, week=week)
        except (_precise.PreciseProductError, KeyError, ValueError):
            skipped.append(key)
    return out, skipped
