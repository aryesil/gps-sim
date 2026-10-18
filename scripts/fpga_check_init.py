#!/usr/bin/env python3
"""Post-synthesis BRAM init check.

usage: fpga_check_init.py <bram_dump.txt> <mem_dir>

bram_dump.txt is written by fpga/synth/check_init.tcl (every RAMB18E1/RAMB36E1 of
the synthesized gs_top: INIT_xx/INITP_xx, port widths, pin nets).  The contents of
every read port that belongs to a table ROM are reconstructed from the INIT/INITP
properties and compared bit-exactly with the source images in <mem_dir>.

Mapping (7-series BRAM, per read port of width W = READ_WIDTH_A / READ_WIDTH_B):
  * d = data bits per word (W=1,2,4 -> W; 9 -> 8; 18 -> 16; 36 -> 32; 72 -> 64),
    p = W - d parity bits.  Word a, data pin j<d  = INIT array bit a*d + j
    (INIT_xx bit k = array bit 256*xx + k); parity pin j>=d = INITP array bit
    a*p + (j-d) (INITP_xx likewise).  RAMB36: 32K data + 4K parity bits, RAMB18:
    16K + 2K.  The same physical array is seen by both ports, each with its width.
  * Address pins ADDRARDADDR/ADDRBWRADDR[k] -> word-address bit k - log2(W)
    (pin 15 of a RAMB36 is the cascade select, ignored).  A pin tied to <const0> /
    <const1> fixes that bit; words not reachable that way are not compared.
    The word address a is the source image index (RTL: tab[idx] with idx bit i on
    address pin i; a permutation would show up as a content mismatch).
  * Data pins are assigned to source-image bit columns by the net they drive
    (q_lo_reg[11] -> bit 11 of the ROM word, c0[3] -> bit 3 and s0[3] -> bit 19 of
    the carrier word...) as a hint.  Vivado drops constant columns and repacks the
    others (the carrier cell has 31 data pins, the base table's 16 bits became
    11 pins holding base[15:5]; names are shifted/opaque, e.g. 'B[15]'), so a pin
    whose hint column does not match is assigned to the source column its
    contents equal exactly.  A pin matching no column is a mismatch.  Every
    non-constant source column must be covered by a pin for every reachable
    word, otherwise it is reported missing (a constant column removed by
    synthesis is reported as folded).
"""
import math
import re
import sys
from collections import Counter, defaultdict


def read_mem(path):
    vals = []
    for ln in open(path):
        ln = ln.split("//")[0].strip()
        if not ln or ln.startswith("@"):
            continue
        vals.append(int(ln, 16))
    return vals


def parse_dump(path):
    cells, lutram = [], []
    cur = None
    cone = dict(roots=[], cells={}, ins=defaultdict(dict), unsupported=[])
    for ln in open(path):
        ln = ln.rstrip("\n")
        if ln.startswith("CONE_"):
            t = ln.split()
            if t[0] == "CONE_ROOT":
                cone["roots"].append(t[1])
            elif t[0] == "CONE_CELL":
                cone["cells"][t[1]] = (t[2], t[3])
            elif t[0] == "CONE_IN":
                cone["ins"][t[1]][t[2]] = tuple(t[3:])
            elif t[0] == "CONE_UNSUPPORTED":
                cone["unsupported"].append(t[1:])
        elif ln.startswith("CELL "):
            _, name, ref, ra, rb, mode = ln.split()
            cur = dict(name=name, ref=ref, ra=int(ra), rb=int(rb), mode=mode,
                       init={}, initp={}, pins={})
        elif ln == "END":
            cells.append(cur)
            cur = None
        elif ln.startswith("LUTRAM "):
            lutram.append(ln.split(" ", 3)[1:])
        elif cur is not None:
            parts = ln.split(" ", 2)
            if parts[0].startswith("INITP_"):
                cur["initp"][int(parts[0][6:], 16)] = int(parts[1].split("h")[1], 16)
            elif parts[0].startswith("INIT_"):
                cur["init"][int(parts[0][5:], 16)] = int(parts[1].split("h")[1], 16)
            elif parts[0] == "PIN":
                cur["pins"][parts[1]] = parts[2] if len(parts) > 2 else "-"
    return cells, lutram, cone


def bitarray(props, n):
    v = 0
    for i, x in props.items():
        v |= x << (256 * i)
    return bin(v)[2:].zfill(256 * n)[::-1]      # index = array bit number


WSHIFT = {1: 0, 2: 1, 4: 2, 9: 3, 18: 4, 36: 5, 72: 6}
WDATA = {1: 1, 2: 2, 4: 4, 9: 8, 18: 16, 36: 32, 72: 64}


def port_columns(cell, port, depth):
    """Read port -> dict(cols: pin->mask over words (bit a = value), nets, reach mask)
    or None if the port drives nothing."""
    W = cell["ra"] if port == "A" else cell["rb"]
    if W == 0:
        return None
    pins = cell["pins"]
    dn, pn = ("DOADO", "DOPADOP") if port == "A" else ("DOBDO", "DOPBDOP")
    d = WDATA[W]
    pw = W - d
    dpins = {}
    for j in range(W):
        net = pins.get("%s[%d]" % ((dn if j < d else pn), j if j < d else j - d), "-")
        if net != "-":
            dpins[j] = net
    if not dpins:
        return None
    is36 = cell["ref"] == "RAMB36E1"
    ap = "ADDRARDADDR" if port == "A" else "ADDRBWRADDR"
    top = 14 if is36 else 13
    fixed = {}
    for k in range(WSHIFT[W], top + 1):
        net = pins.get("%s[%d]" % (ap, k), "-")
        if net.endswith("<const0>") or net == "-":
            fixed[k - WSHIFT[W]] = 0
        elif net.endswith("<const1>"):
            fixed[k - WSHIFT[W]] = 1
    data = bitarray(cell["init"], 128 if is36 else 64)
    par = bitarray(cell["initp"], 16 if is36 else 8)
    nwords = 1 << (top + 1 - WSHIFT[W])
    reach = 0
    cols = {j: 0 for j in dpins}
    for a in range(min(depth, nwords)):
        if any(((a >> b) & 1) != v for b, v in fixed.items()):
            continue
        reach |= 1 << a
        for j in dpins:
            bit = data[a * d + j] if j < d else par[a * pw + j - d]
            if bit == "1":
                cols[j] |= 1 << a
    return dict(W=W, cols=cols, nets=dpins, reach=reach)


def popcount(x):
    return bin(x).count("1")


def check_instance(label, src, width, bitfn, ports, report):
    """Compare one table instance (list of (cell, port, portinfo)) with src."""
    depth = len(src)
    full = (1 << depth) - 1
    srccol = [sum(((src[a] >> b) & 1) << a for a in range(depth)) for b in range(width)]
    claimed = {}
    bad = 0
    notes = []
    unresolved = False
    for cname, pname, pi in ports:
        for j, net in pi["nets"].items():
            hint = bitfn(net)
            if hint is not None and not 0 <= hint < width:
                hint = None
            diffs = [(popcount((pi["cols"][j] ^ srccol[b]) & pi["reach"]), b) for b in range(width)]
            if hint is not None and diffs[hint][0] == 0:
                sb = hint                       # the net name says which bit, content agrees
            else:
                # name gives no (valid) bit, or Vivado repacked the columns (it drops
                # constant columns, so net/pin names can be shifted): use the exact
                # content match; prefer a column not yet claimed, then a non-constant one
                ex = [b for n, b in diffs if n == 0]
                ex.sort(key=lambda b: (b in claimed, srccol[b] in (0, full)))
                sb = ex[0] if ex else None
                if sb is not None:
                    notes.append("%s/%s pin %d (net %s) assigned by content to bit %d (name hint %s)" %
                                 (cname, pname, j, net, sb, hint))
            if sb is None:
                n, b = min(diffs)
                d = (pi["cols"][j] ^ srccol[b]) & pi["reach"]
                notes.append("%s/%s pin %d (net %s): contents match no source column; closest bit %d differs in %d words" %
                             (cname, pname, j, net, b, n))
                bad |= d
                claimed[b] = claimed.get(b, 0) | pi["reach"]
                unresolved = True
            else:
                claimed[sb] = claimed.get(sb, 0) | pi["reach"]
    missing, folded = [], []
    for b in range(width):
        cov = claimed.get(b, 0)
        if cov & full == full:
            continue
        if srccol[b] in (0, full) and cov == 0:
            folded.append(b)            # constant column removed by synthesis
        else:
            missing.append(b)
            bad |= full & ~cov
    nbad = popcount(bad & full)
    good = nbad == 0 and not missing and not unresolved
    report.append((good, "%-15s %-4s words=%-5d mismatching_words=%-5d missing_columns=%s folded_const_columns=%s" % (
        label, "OK" if good else "FAIL", depth, nbad, missing or "none", folded or "none")))
    for n in notes:
        report.append((True, "    note: " + n))
    return good


def eval_cone(cone, leaf, nbits=16):
    """Evaluate the dumped LUT/CARRY4 cone: returns the D value of each root flop
    (list indexed by bit) for the leaf values given by leaf(cell, pin) -> 0/1."""
    memo = {}

    def drv(cell, pin):
        t = cone["ins"][cell][pin]
        if t[0] == "-":
            raise ValueError("unconnected input %s/%s" % (cell, pin))
        dcell, dpin, dref = t[0], t[1], (t[2] if len(t) > 2 else "")
        if dref == "GND":
            return 0
        if dref == "VCC":
            return 1
        if dref.startswith("LUT") or dref == "CARRY4":
            return out(dcell, dpin)
        return leaf(dcell, dpin)

    def out(cell, pin):
        key = (cell, pin)
        if key in memo:
            return memo[key]
        ref, init = cone["cells"][cell]
        if ref.startswith("LUT"):
            n = int(ref[3:])
            idx = sum(drv(cell, "I%d" % k) << k for k in range(n))
            v = (int(init.split("h")[1], 16) >> idx) & 1
        elif ref == "CARRY4":
            c = drv(cell, "CI") | drv(cell, "CYINIT")
            for i in range(4):
                si, di = drv(cell, "S[%d]" % i), drv(cell, "DI[%d]" % i)
                memo[(cell, "O[%d]" % i)] = si ^ c
                c = c if si else di
                memo[(cell, "CO[%d]" % i)] = c
            v = memo[key]
        else:
            raise ValueError("unsupported cone cell " + ref)
        memo[key] = v
        return v
    res = [None] * nbits
    for r in cone["roots"]:
        b = int(re.search(r"\[(\d+)\]$", r).group(1))
        res[b] = drv(r, "D")
    return res


def sincos_bit(net):
    m = re.search(r"/(c0|s0)\[(\d+)\]$", net)
    return int(m.group(2)) + (16 if m.group(1) == "s0" else 0) if m else None


def regex_bit(rx):
    return lambda net: (lambda m: int(m.group(1)) if m else None)(re.search(rx, net))


def main():
    dump, memdir = sys.argv[1], sys.argv[2].rstrip("/\\")
    cells, lutram, cone = parse_dump(dump)
    rom = read_mem(memdir + "/gs_rom.mem")
    rom_hi = read_mem(memdir + "/gs_rom_hi.mem")
    base = read_mem(memdir + "/gs_base.mem")
    sc = read_mem(memdir + "/gs_sincos2.mem")
    rom_lo = rom[:16384]
    rep = []

    def chk(cond, text):
        rep.append((cond, ("ok   " if cond else "FAIL ") + text))
    rep.append((True, "== source images (%s) ==" % memdir))
    chk(len(rom) == 18848, "gs_rom.mem has 18848 words (%d)" % len(rom))
    chk(len(rom_hi) == 2464, "gs_rom_hi.mem has 2464 words (%d)" % len(rom_hi))
    chk(rom_hi == rom[16384:], "gs_rom_hi.mem == gs_rom.mem[16384:]")
    chk(len(base) == 256, "gs_base.mem has 256 words (%d)" % len(base))
    chk(len(sc) == 1024, "gs_sincos2.mem has 1024 words (%d)" % len(sc))
    cq = [int(math.floor(32767 * math.cos(2 * math.pi * r / 4096) + 0.5)) for r in range(1024)]
    sq = [0] + [cq[1024 - r] for r in range(1, 1024)]
    chk(sc == [(s << 16) | c for s, c in zip(sq, cq)], "gs_sincos2.mem == {sin,cos} Q1.15 quarter-wave formula")
    chk(sum(1 for w in rom_lo if w) > 16000 and sum(1 for w in base if w) > 100 and sum(1 for w in sc if w) > 1000,
        "source images are non-trivial (not zero)")

    groups = defaultdict(list)
    other = []
    for c in cells:
        n = c["name"]
        if re.match(r"^u_scan/q_lo_reg_\d+$", n):
            groups["rom_lo"].append(c)
        elif re.match(r"^u_scan/q_hi_reg_\d+$", n):
            groups["rom_hi"].append(c)
        elif n.startswith("u_scan/base_q_reg"):
            groups["base"].append(c)
        elif n.endswith("/u_sc/dab_reg"):
            groups["sincos"].append(c)
        else:
            other.append(c)

    rep.append((True, ""))
    rep.append((True, "== synthesized BRAM cells =="))
    n36 = lambda g: sum(1 for c in groups[g] if c["ref"] == "RAMB36E1")
    chk(len(groups["rom_lo"]) == 16 and n36("rom_lo") == 16,
        "rom_lo: %d cells (%d RAMB36), expect 16 RAMB36 (16K x 2 each)" % (len(groups["rom_lo"]), n36("rom_lo")))
    # 2464 x 32: Vivado 2023.2 packs this as 4 x (4K x 9) RAMB36 (no output mux);
    # 3 x (1K x 36) is the other valid packing.  Contents are checked below.
    chk(len(groups["rom_hi"]) in (3, 4) and n36("rom_hi") == len(groups["rom_hi"]),
        "rom_hi: %d cells (%d RAMB36), expect 3 (1K x 36) or 4 (4K x 9) RAMB36" % (len(groups["rom_hi"]), n36("rom_hi")))
    chk(len(groups["base"]) == 1, "base  : %d cells (%s), expect 1" % (len(groups["base"]), ",".join(c["ref"] for c in groups["base"])))
    sc_ports = []
    for c in groups["sincos"]:
        for p in "AB":
            pi = port_columns(c, p, 1024)
            if pi is None:
                continue
            ids = Counter(int(m[-1]) for m in [re.findall(r"g_pipe\[(\d+)\]", net) for net in pi["nets"].values()] if m)
            pipe = ids.most_common(1)[0][0] if ids else None
            sc_ports.append((c["name"], p, pi, pipe))
    chk(len(sc_ports) == 12, "carrier: %d read ports on %d BRAM cells (%d RAMB36; a RAMB36 can serve 2 pipes through ports A/B), expect 12 copies" %
        (len(sc_ports), len(groups["sincos"]), n36("sincos")))
    pipe_ids = sorted(x[3] for x in sc_ports if x[3] is not None)
    chk(pipe_ids == list(range(12)), "carrier copies belong to pipes 0..11 exactly once (found %s)" % pipe_ids)
    for c in other:
        nz = any(c["init"].values()) or any(c["initp"].values())
        rep.append((True, "info  other BRAM %s %s width %d/%d INIT %s" %
                    (c["name"], c["ref"], c["ra"], c["rb"], "non-zero" if nz else "all zero")))

    rep.append((True, ""))
    rep.append((True, "== LUT-based memories =="))
    tabre = re.compile(r"rom|base|sincos|/tab|u_sc")
    offenders = [x[0] for x in lutram if tabre.search(x[0])]
    chk(not offenders, "no LUT RAM/ROM primitive belongs to a ROM/base/carrier table (%d LUT RAM primitives in total%s)" %
        (len(lutram), "; offender " + offenders[0] if offenders else ", all in the per-pipe bank RAMs"))

    rep.append((True, ""))
    rep.append((True, "== BRAM INIT contents vs source images =="))

    def ports_of(cl, depth):
        out = []
        for c in cl:
            for p in "AB":
                pi = port_columns(c, p, depth)
                if pi:
                    out.append((c["name"], p, pi))
        return out
    check_instance("code ROM lo", rom_lo, 32, regex_bit(r"q_lo_reg\[(\d+)\]$"), ports_of(groups["rom_lo"], 16384), rep)
    check_instance("code ROM hi", rom_hi, 32, regex_bit(r"q_hi_reg\[(\d+)\]$"), ports_of(groups["rom_hi"], 2464), rep)
    check_instance("base table", base, 16, regex_bit(r"base_q_reg(?:__\d+)?\[(\d+)\]$"), ports_of(groups["base"], 256), rep)
    for pid in range(12):
        pl = [(cn, p, pi) for cn, p, pi, i in sc_ports if i == pid]
        if not pl:
            rep.append((False, "%-15s FAIL no BRAM read port found" % ("carrier pipe %d" % pid)))
            continue
        check_instance("carrier pipe %d" % pid, sc, 32, sincos_bit, pl, rep)

    # The base BRAM output feeds the adder rom_addr <= base_q + widx; Vivado turned it
    # into a carry chain on the repacked BRAM pins, so verify the whole path from the
    # netlist cone: rom_addr(base[a], widx) == base[a] + widx for every a, widx.
    bc = groups["base"][0] if groups["base"] else None
    if bc is None or not cone["roots"]:
        rep.append((False, "base adder cone     FAIL no base BRAM / rom_addr cone in the dump"))
    else:
        pi = port_columns(bc, "A", 256)
        bad_a = set()
        err = None
        try:
            for a in range(256):
                def leaf(cell, pin, a=a):
                    if cell == bc["name"] and pin.startswith("DOADO["):
                        j = int(pin[6:-1])
                        return (pi["cols"][j] >> a) & 1 if j in pi["cols"] else 0
                    m = re.search(r"s2_widx_reg\[(\d+)\]$", cell)
                    if m and pin == "Q":
                        return (w >> int(m.group(1))) & 1
                    raise ValueError("unknown cone leaf %s/%s" % (cell, pin))
                for w in range(128):
                    r = eval_cone(cone, leaf)
                    if sum(v << i for i, v in enumerate(r)) != (base[a] + w) & 0xFFFF:
                        bad_a.add(a)
                        break
        except (ValueError, KeyError) as e:
            err = e
        good = err is None and not bad_a and not cone["unsupported"]
        rep.append((good, "%-15s %-4s words=256   mismatching_words=%d  (rom_addr = base + widx, all 256 x 128 combinations, %d cone cells)%s" % (
            "base+widx cone", "OK" if good else "FAIL", len(bad_a), len(cone["cells"]),
            "  error: %s" % err if err else "")))

    ok = all(f for f, _ in rep)
    print("\n".join(m for _, m in rep))
    print()
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
