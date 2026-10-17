"""Expected gs_loader write streams, decoded from fpga/vec/<case>_cmd.hex.

Usage (repo root):  python scripts/fpga_loader_vectors.py [case ...] [--p P]
Default cases: gce12 gapcase latecase maxch (also ca1 e1b e1c b1i gce45).

Writes fpga/vec/<case>_load.hex (P=12) or <case>_load_p<P>.hex: one line per
expected l_we write, 83 hex digits = 332 bits
    { pipe[3:0], slot[3:0], par[175:0] (173 used), st[147:0] }
Order per loaded block: first the invalidation sweep (ch = nch .. P*SLOTS-1,
all-zero par/st), then the channel records in stream order.  The sequencer
stub model is: target starts at start_block; for each BLOCK record in stream
order, while idx > target the target is advanced (the missing block is muted
at the edge); idx == target loads and then advances the target; idx < target
is dropped (dropped_late).
Also fpga/vec/<case>_loadinfo[_pP].hex, 8 lines of 8 hex digits:
    nch fs shift start_lo start_hi nwrites dropped_late nblocks_loaded
Layouts follow the comments in fpga/rtl/gs_defs.vh (authoritative).
"""
from __future__ import annotations

import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.fpga import cmdfmt  # noqa: E402

PAR_W, ST_W = 173, 148


def pack_par(c) -> int:
    cboc = {0: 0, 1: 1, -1: 2}[c.cboc]
    v = int(bool(c.valid)) | (c.boc << 1) | (cboc << 2) | (c.kind << 4)
    v |= c.prn << 6
    v |= (c.carr_inc & 0xFFFFFFFF) << 12
    v |= c.code_inc << 44
    v |= c.sign_bits << 87
    for i in range(3):
        v |= (c.coef[i] & 0x3FFFF) << (119 + 18 * i)
    return v


def pack_st(c) -> int:
    v = c.carr_phase0
    v |= c.code_phase0 << 32
    v |= c.sign_phase0 << 84
    v |= c.sign_idx0 << 136
    v |= (c.code_phase0 >> 45) << 141
    return v


def line(pipe, slot, par, st) -> str:
    v = (pipe << 328) | (slot << 324) | (par << 148) | st
    return f"{v:083x}"


def expected(words, p):
    slots = (64 + p - 1) // p
    data = struct.pack(f"<{len(words)}I", *words)
    items = cmdfmt.unpack_stream(data)
    cfg = None
    target = None
    lines, dropped, nblk = [], 0, 0
    for it in items:
        if isinstance(it, cmdfmt.CfgCommand):
            cfg = it
            target = it.start_block
            continue
        idx, chans = it
        if target is None:
            raise SystemExit("BLOCK before CFG")
        while idx > target:
            target += 1
        if idx < target:
            dropped += 1
            continue
        for ch in range(len(chans), p * slots):
            lines.append(line(ch % p, ch // p, 0, 0))
        for c in chans:
            lines.append(line(c.ch % p, c.ch // p, pack_par(c), pack_st(c)))
        nblk += 1
        target += 1
    return cfg, lines, dropped, nblk


def main(argv):
    p = 12
    cases = []
    i = 0
    while i < len(argv):
        if argv[i] == "--p":
            p = int(argv[i + 1])
            i += 2
        else:
            cases.append(argv[i])
            i += 1
    cases = cases or ["gce12", "gapcase", "latecase", "maxch"]
    vec = os.path.join(ROOT, "fpga", "vec")
    suf = "" if p == 12 else f"_p{p}"
    for case in cases:
        with open(os.path.join(vec, f"{case}_cmd.hex")) as f:
            words = [int(s, 16) for s in f.read().split()]
        cfg, lines, dropped, nblk = expected(words, p)
        with open(os.path.join(vec, f"{case}_load{suf}.hex"), "w") as f:
            f.write("\n".join(lines) + "\n")
        info = [cfg.nch, cfg.fs_hz, cfg.shift, cfg.start_block & 0xFFFFFFFF,
                cfg.start_block >> 32, len(lines), dropped, nblk]
        with open(os.path.join(vec, f"{case}_loadinfo{suf}.hex"), "w") as f:
            f.write("".join(f"{x:08x}\n" for x in info))
        print(f"{case}: nch={cfg.nch} writes={len(lines)} dropped={dropped} blocks={nblk}")


if __name__ == "__main__":
    main(sys.argv[1:])
