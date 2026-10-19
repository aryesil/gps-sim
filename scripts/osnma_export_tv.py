"""Export generated Galileo E1-B pages as an OSNMAlib fixed-format CSV (TOW,WN,SVID,NAVBitsHex).

    python scripts/osnma_export_tv.py --plan-json req.json --out run.csv [--keyset k.json]

The request JSON holds ScenarioRequest fields (start as ISO-8601). The E1 nav symbol streams are
captured from engine.plan(...).nav_streams and decoded back to 240-bit pages; the output is
checked with the in-repo verifier when the keyset is available.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.analysis import inav_decode  # noqa: E402
from backend.analysis.osnma import verify  # noqa: E402
from backend.analysis.osnma.crypto import bytes_from_bits  # noqa: E402

_WEEK_S = 604800


def write_fixed_csv(pages: dict[tuple[int, int], list[int]], path) -> int:
    rows = sorted((g, prn, bytes_from_bits(b).hex().upper()) for (prn, g), b in pages.items())
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for g, prn, hx in sorted(rows, key=lambda r: (r[0], r[1])):
            f.write(f"{g % _WEEK_S},{g // _WEEK_S},{prn},{hx}\n")
    return len(rows)


def pages_from_streams(streams: dict[int, np.ndarray], gst_abs0: int) -> dict[tuple[int, int], list[int]]:
    out: dict[tuple[int, int], list[int]] = {}
    for prn, arr in streams.items():
        sym = ((1 - np.asarray(arr)) // 2).astype(int).tolist()
        out.update(verify.pages_from_decoded(inav_decode.decode_messages(sym), prn, gst_abs0))
    return out


def capture_pages(req) -> dict[tuple[int, int], list[int]]:
    from backend.synth import engine
    pl = engine.plan(req)
    ns = getattr(pl, "nav_streams", None)
    t0 = getattr(pl, "nav_t0_gst", None)
    if ns is None or t0 is None:
        raise SystemExit("engine.plan() exposes no nav_streams/nav_t0_gst capture")
    streams = {int(k[1]): v[3] for k, v in ns.items() if k[0] == "E" and str(k[2]).startswith("L1")}
    return pages_from_streams(streams, int(t0))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan-json", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--keyset", help="keyset.json for the self-check (default: keys.DEFAULT_PATH)")
    a = ap.parse_args()
    from backend.analysis.osnma import keys
    from backend.scenario import ScenarioRequest
    d = json.loads(pathlib.Path(a.plan_json).read_text(encoding="utf-8"))
    d["start"] = dt.datetime.fromisoformat(d["start"])
    pg = capture_pages(ScenarioRequest(**d))
    print(f"wrote {write_fixed_csv(pg, a.out)} pages to {a.out}")
    try:
        ks = keys.load(a.keyset or keys.DEFAULT_PATH)
    except FileNotFoundError:
        return
    print(verify.Verifier(pubkey=ks.npk, merkle_root=ks.merkle_root).run(pg).summary())


if __name__ == "__main__":
    main()
