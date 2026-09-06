"""Produce a small committed RINEX 3 mixed nav fixture from a full daily file.

Usage:
    python tools/trim_rinex_mixed.py data/rinex/BRDC_2026244.rnx \
        tests/fixtures/brdc_mixed.rnx --per-system 3

Keeps the RINEX 3 header verbatim, then a handful of record blocks for each of
G R E C J S. Blocks are copied as raw text lines (no re-formatting) so georinex
parses them byte-identically to the source.

georinex silently drops nav records whose optional "spare" columns are blank
(common for BeiDou), so the source is first loaded with georinex and only the
(SV, epoch) pairs it actually decodes are eligible for the fixture. For BeiDou
(C) only MEO/IGSO PRNs 6..58 are kept -- the GEO PRNs C01..C05 / C59..C63 take
a different reference-frame rotation and would not line up with the sys-int
cross-check, which propagates every C record as MEO.
"""
import argparse

import numpy as np
import georinex as gr

_SYS = "GRECJS"
# BeiDou PRNs that use the GEO reference-frame rotation; skipped by the trim.
_BDS_GEO_PRNS = set(range(1, 6)) | set(range(59, 64))
# Non-NaN on a parsed record for every system; NaN when georinex dropped it.
_SENTINEL = "SVclockBias"


def _is_record_start(ln: str) -> bool:
    return len(ln) > 3 and ln[0] in _SYS and ln[1:3].strip().isdigit()


def _epoch_key(ln: str) -> str:
    # "C06 2026 08 31 23 00 00 ..." -> "2026 08 31 23 00 00"
    return " ".join(ln[4:23].split())


def _good_pairs(src: str, systems: str) -> dict:
    """{sys_char: [(prn_int, epoch_key), ...]} georinex decodes cleanly."""
    out: dict = {}
    for s in systems:
        try:
            nav = gr.load(src, use=[s])
        except Exception:
            continue
        for sv in nav.sv.values:
            prn = int(str(sv)[1:])
            sub = nav.sel(sv=sv)
            vals = sub[_SENTINEL].values
            times = sub.time.values
            for t, v in zip(times, np.atleast_1d(vals)):
                if not np.isfinite(v):
                    continue
                ts = str(np.datetime64(t, "s"))  # 2026-08-31T23:00:00
                d, hms = ts.split("T")
                y, mo, da = d.split("-")
                key = f"{y} {mo} {da} {hms.replace(':', ' ')}"
                out.setdefault(s, []).append((prn, key))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--per-system", type=int, default=3)
    a = ap.parse_args()

    with open(a.src) as fh:
        lines = fh.read().splitlines(keepends=True)
    hi = next(i for i, ln in enumerate(lines) if "END OF HEADER" in ln)
    header, body = lines[: hi + 1], lines[hi + 1:]

    blocks: dict = {}
    cur_key = None
    for ln in body:
        if _is_record_start(ln):
            cur_key = (ln[0], int(ln[1:3]), _epoch_key(ln))
            blocks[cur_key] = [ln]
        elif cur_key is not None:
            blocks[cur_key].append(ln)

    good = _good_pairs(a.src, _SYS)
    kept: list[list[str]] = []
    seen = {s: 0 for s in _SYS}
    for s in _SYS:
        used_prn: set[int] = set()
        for prn, key in good.get(s, []):
            if seen[s] >= a.per_system or prn in used_prn:
                continue
            if s == "C" and prn in _BDS_GEO_PRNS:
                continue
            blk = blocks.get((s, prn, key))
            if blk is None:
                continue
            kept.append(blk)
            used_prn.add(prn)
            seen[s] += 1

    with open(a.dst, "w") as fh:
        fh.writelines(header)
        for b in kept:
            fh.writelines(b)
    print("systems:", {s: seen[s] for s in _SYS}, "-> blocks:", len(kept))


if __name__ == "__main__":
    main()
