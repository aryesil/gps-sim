"""Parse Septentrio SBF logs for Galileo OSNMA: GALRawINAV (4023) pages and GALAuthStatus (4245).

    python scripts/osnma_sbf.py capture.sbf [--keyset keyset.json]
    python scripts/osnma_sbf.py --capture HOST PORT SECONDS --out capture.sbf

Layouts follow the mosaic-X5 Firmware v4.15.0 Reference Guide (SBF Reference, 4.2).
"""
from __future__ import annotations

import argparse
import pathlib
import socket
import struct
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.analysis.osnma import keys, verify  # noqa: E402

_GAL_SVID0 = 71
_GPS_WEEK_OFFSET = 1024
_WEEK_S = 604800
_SRC_E1BC = 15
_STATUS = {0: "disabled", 1: "initializing", 2: "waiting for trusted time",
           3: "init failed: inconsistent time", 4: "init failed: KROOT signature invalid",
           5: "init failed: invalid param", 6: "authenticating"}
_TIME_SRC = {0: "NTP", 1: "L-Band", 2: "command", 7: "unknown"}


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def blocks(data: bytes):
    """Yield (block_id, tow_ms, wnc, payload) for every CRC-valid SBF block."""
    i = 0
    while True:
        i = data.find(b"$@", i)
        if i < 0 or i + 14 > len(data):
            return
        crc, ident, length = struct.unpack_from("<HHH", data, i + 2)
        if length < 14 or length % 4 or i + length > len(data) or crc16(data[i + 4:i + length]) != crc:
            i += 2
            continue
        tow, wnc = struct.unpack_from("<IH", data, i + 8)
        yield ident & 0x1FFF, tow, wnc, data[i + 14:i + length]
        i += length


def _navbits(nav: bytes) -> list[int]:
    """234 page bits; the first received bit is the MSB of the little-endian u4 NAVBits[0]."""
    words = struct.unpack("<8I", nav)
    bits = [(w >> (31 - k)) & 1 for w in words for k in range(32)]
    if bits[0] == 0 and bits[114] == 1:
        return bits[:234]
    raw = [(nav[k >> 3] >> (7 - (k & 7))) & 1 for k in range(234)]
    return raw if raw[0] == 0 and raw[114] == 1 else bits[:234]


def _prns(mask: int) -> list[int]:
    return [i + 1 for i in range(64) if mask >> i & 1]


def parse_sbf(path) -> tuple[dict[tuple[int, int], list[int]], list[dict]]:
    pages: dict[tuple[int, int], list[int]] = {}
    auths: list[dict] = []
    for ident, tow, wnc, p in blocks(pathlib.Path(path).read_bytes()):
        if ident == 4023 and len(p) >= 38:
            svid, crc_ok, src = p[0], p[1], p[3]
            prn = svid - _GAL_SVID0 + 1
            if not crc_ok or not 1 <= prn <= 36 or tow == 0xFFFFFFFF or (src & 0x1F) != _SRC_E1BC:
                continue
            n = _navbits(p[6:38])
            gst = (wnc - _GPS_WEEK_OFFSET) * _WEEK_S + tow // 1000 - 2
            pages[(prn, gst)] = n[:114] + [0] * 6 + n[114:234]
        elif ident == 4245 and len(p) >= 38:
            st, td, gam, gau, pam, pau = struct.unpack_from("<HfQQQQ", p, 0)
            auths.append({"tow_ms": tow, "wnc": wnc, "status": _STATUS.get(st & 7, str(st & 7)),
                          "progress": st >> 3 & 0xFF, "time_source": _TIME_SRC.get(st >> 11 & 7, "?"),
                          "merkle_renewal": bool(st >> 14 & 1), "time_delta_s": td,
                          "gal_active": _prns(gam), "gal_authentic": _prns(gau),
                          "gps_active": _prns(pam), "gps_authentic": _prns(pau)})
    return pages, auths


def capture(host: str, port: int, seconds: float, out) -> int:
    s = socket.create_connection((host, port), timeout=10)
    s.settimeout(15)
    n, t0 = 0, time.monotonic()
    with open(out, "wb") as f:
        while time.monotonic() - t0 < seconds:
            d = s.recv(65536)
            if not d:
                break
            f.write(d)
            n += len(d)
    s.close()
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sbf", nargs="?")
    ap.add_argument("--keyset", help="keyset.json for the in-repo verifier (default: keys.DEFAULT_PATH)")
    ap.add_argument("--capture", nargs=3, metavar=("HOST", "PORT", "SECONDS"))
    ap.add_argument("--out", default="capture.sbf")
    a = ap.parse_args()
    path = a.sbf
    if a.capture:
        h, p, sec = a.capture
        print(f"captured {capture(h, int(p), float(sec), a.out)} bytes to {a.out}")
        path = path or a.out
    if not path:
        ap.error("sbf file required")
    pg, auths = parse_sbf(path)
    print(f"GALRawINAV pages: {len(pg)} from {len({k[0] for k in pg})} SVs; "
          f"GALAuthStatus records: {len(auths)}")
    if pg:
        try:
            ks = keys.load(a.keyset or keys.DEFAULT_PATH)
            v = verify.Verifier(pubkey=ks.npk, merkle_root=ks.merkle_root)
        except FileNotFoundError:
            v = verify.Verifier()
        print("in-repo verifier:", v.run(pg).summary())
    prev = None
    for r in auths:
        key = (r["status"], r["progress"], tuple(r["gal_active"]), tuple(r["gal_authentic"]))
        if key != prev:
            print(f"tow={r['tow_ms'] / 1000:.0f}s {r['status']} {r['progress']}% "
                  f"time={r['time_source']} dt={r['time_delta_s']:.2f}s "
                  f"active=E{r['gal_active']} authentic=E{r['gal_authentic']}")
            prev = key


if __name__ == "__main__":
    main()
