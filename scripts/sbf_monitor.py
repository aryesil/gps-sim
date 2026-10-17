"""Live Septentrio SBF monitor: connect to a raw SBF TCP stream, verify CRCs and
print one line per epoch with the tracked satellites per constellation, their
C/N0 and the PVT state. Read-only; it never sends anything to the receiver.

    python scripts/sbf_monitor.py 192.168.1.93 28785 --seconds 120 --csv sbf.csv

Blocks used: MeasEpoch (4027), PVTGeodetic (4007), SatVisibility (4012),
DOP (4001), RAIMStatus (4059). Layouts follow the Septentrio Firmware
Reference Guide for the mosaic family (SBF block IDs and field order).
"""
from __future__ import annotations

import argparse
import math
import socket
import struct
import time
from collections import defaultdict

_DNU = -2e10   # Septentrio "do not use" marker for f8/f4 fields

# SBF signal type -> short name (Firmware Reference Guide, MeasEpoch)
SIGNAL = {
    0: "G L1CA", 1: "G L1P", 2: "G L2P", 3: "G L2C", 4: "G L5", 5: "G L1C",
    6: "J L1CA", 7: "J L2C", 8: "R L1CA", 9: "R L2CA", 10: "R L3",
    11: "C B1C", 12: "C B2a", 13: "I L5", 15: "E E1", 17: "E E6", 18: "E E5a",
    19: "E E5b", 20: "E E5", 22: "S L1", 23: "S L5", 24: "J L5", 26: "C B1I",
    27: "C B2I", 28: "C B3I",
}

PVT_ERROR = {
    0: "none", 1: "not enough measurements", 2: "not enough ephemerides",
    3: "DOP too large", 4: "sum of squared residuals too large",
    5: "no convergence", 6: "not enough measurements after outlier rejection",
    7: "position output prohibited", 8: "not enough differential corrections",
    9: "base coordinates unavailable", 10: "ambiguities not fixed",
}
PVT_MODE = {0: "no PVT", 1: "stand-alone", 2: "differential", 3: "fixed location",
            4: "RTK fixed", 5: "RTK float", 6: "SBAS", 7: "moving-base RTK fixed",
            8: "moving-base RTK float", 10: "PPP", 12: "Rx-pos only"}


def svid_to_name(svid: int) -> str:
    if 1 <= svid <= 37:
        return f"G{svid:02d}"
    if 38 <= svid <= 61:
        return f"R{svid - 37:02d}"
    if 71 <= svid <= 106:
        return f"E{svid - 70:02d}"
    if 120 <= svid <= 140:
        return f"S{svid:03d}"
    if 141 <= svid <= 180:
        return f"C{svid - 140:02d}"
    if 223 <= svid <= 245:
        return f"C{svid - 185:02d}"
    if 181 <= svid <= 187:
        return f"J{svid - 180:02d}"
    if 191 <= svid <= 197:
        return f"I{svid - 190:02d}"
    return f"?{svid}"


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Stream:
    """Yield (block_id, tow_ms, wnc, body) for every CRC-valid SBF block."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()
        self.bad_crc = 0
        self.blocks = 0

    def __iter__(self):
        while True:
            data = self.sock.recv(65536)
            if not data:
                return
            self.buf += data
            while True:
                i = self.buf.find(b"$@")
                if i < 0:
                    del self.buf[:-1]
                    break
                if i:
                    del self.buf[:i]
                if len(self.buf) < 8:
                    break
                crc, ident, length = struct.unpack_from("<HHH", self.buf, 2)
                if length < 8 or length > 8192 or length % 4:
                    del self.buf[:2]
                    continue
                if len(self.buf) < length:
                    break
                blk = bytes(self.buf[:length])
                del self.buf[:length]
                if crc16(blk[4:]) != crc:
                    self.bad_crc += 1
                    continue
                self.blocks += 1
                tow, wnc = struct.unpack_from("<IH", blk, 8)
                yield ident & 0x1FFF, tow, wnc, blk[14:]


def parse_meas_epoch(body: bytes):
    n1, sb1_len, sb2_len = body[0], body[1], body[2]
    off = 6
    out = []
    for _ in range(n1):
        if off + 20 > len(body):
            break
        chan, typ, svid = body[off], body[off + 1], body[off + 2]
        cn0_raw = body[off + 15]
        sig = typ & 0x1F
        # CN0 offset: +10 dB-Hz unless GPS L1P/L2P (signal types 1, 2)
        cn0 = cn0_raw * 0.25 + (0.0 if sig in (1, 2) else 10.0)
        if cn0_raw == 255:
            cn0 = None
        locktime = struct.unpack_from("<H", body, off + 16)[0]
        n2 = body[off + 19]
        out.append({"sv": svid_to_name(svid), "sig": SIGNAL.get(sig, f"sig{sig}"),
                    "cn0": cn0, "lock_s": locktime})
        off += sb1_len + n2 * sb2_len
    return out


def parse_pvt_geodetic(body: bytes):
    mode, error = body[0], body[1]
    lat, lon, h = struct.unpack_from("<ddd", body, 2)
    nsv = body[2 + 24 + 4 * 5 + 8 + 4 + 2]
    ok = lat > _DNU
    return {"mode": PVT_MODE.get(mode & 0x0F, str(mode & 0x0F)), "error": PVT_ERROR.get(error, str(error)),
            "lat": math.degrees(lat) if ok else None, "lon": math.degrees(lon) if ok else None,
            "h": h if ok else None, "nsv": nsv}


def parse_dop(body: bytes):
    nr_sv = body[0]
    pdop, tdop, hdop, vdop = (v / 100.0 for v in struct.unpack_from("<4H", body, 2))
    return {"nsv": nr_sv, "pdop": pdop, "hdop": hdop}


def parse_raim(body: bytes):
    return {"integrity": body[0]}


def fmt_epoch(t, meas, pvt, dop, raim):
    per = defaultdict(list)
    for m in meas:
        if m["cn0"] is not None:
            per[m["sv"][0]].append(m["cn0"])
    sysbits = " ".join(f"{s}:{len(v)}@{sum(v) / len(v):.0f}" for s, v in sorted(per.items())) or "no tracking"
    p = pvt or {}
    pos = (f"{p['lat']:.5f},{p['lon']:.5f},{p['h']:.0f}m" if p.get("lat") is not None else "-")
    return (f"{t:6.0f}s | {sysbits:<34} | {p.get('mode', '?'):<11} nsv={p.get('nsv', '?'):<2} "
            f"err={p.get('error', '?')} pos={pos} hdop={(dop or {}).get('hdop', '-')}"
            f" raim={(raim or {}).get('integrity', '-')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--csv", help="append one row per signal per epoch")
    ap.add_argument("--sv", action="store_true", help="also list every tracked signal each epoch")
    a = ap.parse_args()

    sock = socket.create_connection((a.host, a.port), timeout=10)
    sock.settimeout(15)
    csv = open(a.csv, "a", encoding="utf-8") if a.csv else None
    if csv and csv.tell() == 0:
        csv.write("t_s,tow_ms,sv,signal,cn0,lock_s\n")
    t0 = time.monotonic()
    meas, pvt, dop, raim = [], None, None, None
    last_tow = None
    ids_seen = defaultdict(int)
    stream = Stream(sock)
    try:
        for ident, tow, wnc, body in stream:
            ids_seen[ident] += 1
            if ident == 4027:
                meas = parse_meas_epoch(body)
                if csv:
                    for m in meas:
                        csv.write(f"{time.monotonic() - t0:.1f},{tow},{m['sv']},{m['sig']},{m['cn0']},{m['lock_s']}\n")
                    csv.flush()
                last_tow = tow
                print(fmt_epoch(time.monotonic() - t0, meas, pvt, dop, raim), flush=True)
                if a.sv:
                    for m in sorted(meas, key=lambda x: x["sv"]):
                        print(f"          {m['sv']} {m['sig']:<7} cn0={m['cn0']} lock={m['lock_s']}s")
            elif ident == 4007:
                pvt = parse_pvt_geodetic(body)
            elif ident == 4001:
                dop = parse_dop(body)
            elif ident == 4059:
                raim = parse_raim(body)
            if time.monotonic() - t0 >= a.seconds:
                break
    except socket.timeout:
        print("timeout: no SBF data for 15 s")
    finally:
        sock.close()
    print(f"blocks={stream.blocks} bad_crc={stream.bad_crc} ids={dict(ids_seen)}")


if __name__ == "__main__":
    main()
