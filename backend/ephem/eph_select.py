"""Deterministic broadcast-ephemeris record selection.

``ephemeris.parse_rinex`` collapses a daily BRDC file to one record per PRN
(the one whose ``toe`` is nearest the file's midday). That is enough for the
generator, which realigns every ``toc``/``toe`` to the requested start
anyway -- but it hides *which* navigation message was used and gives no
explicit handling of multiple records, stale records, unhealthy satellites
or missing PRNs.

This module is the auditable selector: given every record a file carries for
a PRN and a scenario epoch, it picks one by fixed rules and returns a report
naming the choice and every record it rejected and why. Nothing here changes
the generator; ``app`` / ``generator`` record the report in ``meta.json`` so
a recording says exactly which ephemeris produced it.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import georinex as gr
import numpy as np

from backend import config
from backend.ephem import ephemeris

_WEEK = 604800.0
# GPS broadcast nav is nominally valid for 2 h from toe (curve fit interval);
# a record older than this at the scenario epoch is "stale".
DEFAULT_MAX_AGE_S = 7200.0
# Records up to this far in the *future* of the epoch are still acceptable
# (the message is uploaded slightly ahead of its toe).
DEFAULT_MAX_LEAD_S = 7200.0


class EphemerisSelectionError(Exception):
    """No acceptable record for a requested PRN at the scenario epoch."""


@dataclass
class RecordRef:
    """A single parsed broadcast record plus its provenance."""
    prn: int
    toe_gps_s: float          # continuous GPS seconds (week*604800 + toe)
    toc_gps_s: float
    gps_week: int
    iode: int | None
    iodc: int | None
    health: int
    eph: dict = field(repr=False)

    def age_s(self, epoch_gps_s: float) -> float:
        """Signed: >0 the record is in the past of the epoch, <0 in the future."""
        return epoch_gps_s - self.toe_gps_s


@dataclass
class SelectionReport:
    epoch_gps_s: float
    selected: dict[int, RecordRef]
    rejected: dict[int, list[tuple[str, str]]]   # prn -> [(record_id, reason)]
    missing: list[int]
    unhealthy: list[int]
    stale_used: list[int]

    def identifier(self) -> dict:
        """Compact, JSON-safe summary for meta.json."""
        return {
            "epoch_gps_s": self.epoch_gps_s,
            "selected": {
                p: {"toe_gps_s": r.toe_gps_s, "gps_week": r.gps_week,
                    "iode": r.iode, "iodc": r.iodc, "health": r.health,
                    "age_s": round(r.age_s(self.epoch_gps_s), 3)}
                for p, r in sorted(self.selected.items())
            },
            "missing_prns": self.missing,
            "unhealthy_prns": self.unhealthy,
            "stale_used_prns": self.stale_used,
            "rejected": {p: v for p, v in sorted(self.rejected.items())},
        }


def _to_datetime(ts) -> dt.datetime:
    secs = np.datetime64(ts, "s").astype("int64")
    return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=int(secs))


def parse_rinex_records(path) -> dict[int, list[RecordRef]]:
    """Every GPS navigation record in ``path``, grouped by PRN, unsorted."""
    nav = gr.load(str(path), use="G")
    out: dict[int, list[RecordRef]] = {}
    for sv in nav.sv.values:
        if not str(sv).startswith("G"):
            continue
        prn = int(str(sv)[1:])
        sub = nav.sel(sv=sv).dropna(dim="time", how="all")
        for i in range(int(sub.time.size)):
            rec = sub.isel(time=i)
            e: dict[str, float] = {}
            for key, var in ephemeris._VARMAP.items():
                if var is None:
                    continue
                try:
                    e[key] = float(rec[var].values)
                except (KeyError, ValueError):
                    e[key] = 0.0
            toc_dt = _to_datetime(rec.time.values)
            e["toc"] = ((toc_dt - ephemeris._GPS_EPOCH).total_seconds()) % _WEEK
            week = int(e.get("gps_week", 0))
            ref = RecordRef(
                prn=prn,
                toe_gps_s=week * _WEEK + float(e["toe"]),
                toc_gps_s=week * _WEEK + float(e["toc"]),
                gps_week=week,
                iode=int(e["iode"]) if "iode" in e else None,
                iodc=int(e["iodc"]) if "iodc" in e else None,
                health=int(e.get("health", 0)),
                eph=e,
            )
            out.setdefault(prn, []).append(ref)
    return out


def select(records_by_prn: dict[int, list[RecordRef]],
           epoch_gps_s: float,
           *,
           prns: list[int] | None = None,
           max_age_s: float = DEFAULT_MAX_AGE_S,
           max_lead_s: float = DEFAULT_MAX_LEAD_S,
           require_healthy: bool = True,
           allow_stale: bool = False) -> SelectionReport:
    """Pick one record per PRN for ``epoch_gps_s`` (continuous GPS seconds).

    Rules, in order:
      1. drop records with ``health != 0`` when ``require_healthy``;
      2. drop records more than ``max_lead_s`` in the future of the epoch;
      3. among the rest prefer the newest ``toe`` at or before the epoch;
         if none, the closest future record within ``max_lead_s``;
      4. if the chosen record is older than ``max_age_s`` it is *stale*:
         used only when ``allow_stale``, otherwise the PRN is rejected.
    Deterministic: ties broken by higher ``iodc`` then higher ``toe``.
    """
    want = sorted(records_by_prn) if prns is None else sorted(prns)
    selected: dict[int, RecordRef] = {}
    rejected: dict[int, list[tuple[str, str]]] = {}
    missing, unhealthy, stale_used = [], [], []

    for prn in want:
        recs = records_by_prn.get(prn, [])
        if not recs:
            missing.append(prn)
            continue
        rej = rejected.setdefault(prn, [])
        pool = []
        for r in recs:
            rid = f"toe={r.toe_gps_s:.0f},iode={r.iode}"
            if require_healthy and r.health != 0:
                rej.append((rid, f"unhealthy (health={r.health})"))
                continue
            if r.age_s(epoch_gps_s) < -max_lead_s:
                rej.append((rid, f"{-r.age_s(epoch_gps_s):.0f}s in the future"))
                continue
            pool.append(r)
        if not pool:
            if any("unhealthy" in reason for _, reason in rej):
                unhealthy.append(prn)
            continue
        past = [r for r in pool if r.age_s(epoch_gps_s) >= 0.0]
        if past:
            best = max(past, key=lambda r: (r.toe_gps_s, r.iodc or -1))
        else:
            best = min(pool, key=lambda r: (-r.age_s(epoch_gps_s), -(r.iodc or -1)))
        age = best.age_s(epoch_gps_s)
        if age > max_age_s:
            if not allow_stale:
                rej.append((f"toe={best.toe_gps_s:.0f}", f"stale ({age:.0f}s old)"))
                continue
            stale_used.append(prn)
        selected[prn] = best
        for r in pool:
            if r is not best:
                rej.append((f"toe={r.toe_gps_s:.0f}", "not the closest valid record"))

    rejected = {p: v for p, v in rejected.items() if v}
    return SelectionReport(epoch_gps_s, selected, rejected, missing, unhealthy, stale_used)


def selected_eph_by_prn(report: SelectionReport) -> dict[int, dict]:
    """The parsed-ephemeris dicts for the selected records, for geometry/fit."""
    return {p: r.eph for p, r in report.selected.items()}
