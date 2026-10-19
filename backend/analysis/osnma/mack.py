"""OSNMA MACK assembly and parsing (SIS ICD 1.1 section 3.3)."""
from __future__ import annotations

from dataclasses import dataclass

from . import crypto
from .tables import MACK_BITS, MACLT, NAVDATA_LEN, nt_for


@dataclass(frozen=True)
class TagSlot:
    prn_d: int
    adkd: int
    cop: int
    navdata: tuple[int, ...]


def slot_sequence(maclt: int, gst_sf: int) -> tuple[str, ...]:
    return MACLT[maclt][0 if gst_sf % 60 == 0 else 1]


def tag_info(prn_d: int, adkd: int, cop: int) -> int:
    return (prn_d & 0xFF) << 8 | (adkd & 0xF) << 4 | (cop & 0xF)


def _navdata(adkd: int, cop: int, navdata) -> list[int]:
    return [0] * NAVDATA_LEN[adkd] if cop == 0 else list(navdata)


def build_mack(*, prn_a: int, gst_sf: int, nmas: int, ks_bits: int, ts_bits: int,
               key_tag: bytes, key_slow: bytes, key_tx: bytes, tag0_navdata,
               tag0_cop: int, slots: list[TagSlot],
               flx_idx: tuple[int, ...] = ()) -> list[int]:
    nt = nt_for(ks_bits, ts_bits)
    if len(slots) > nt - 1:
        raise ValueError(f"{len(slots)} slots exceed nt-1={nt - 1}")
    t0 = crypto.tag(key_tag, None, prn_a, gst_sf, 1, nmas,
                    _navdata(0, tag0_cop, tag0_navdata), ts_bits)
    flx = [tag_info(s.prn_d, s.adkd, s.cop)
           for i, s in enumerate(slots, 1) if i in flx_idx]
    seq = crypto.macseq(key_tag, prn_a, gst_sf, flx)
    bits = (crypto.bits_from_int(t0, ts_bits) + crypto.bits_from_int(seq, 12)
            + crypto.bits_from_int(tag0_cop, 4))
    for i, s in enumerate(slots):
        k = key_slow if s.adkd == 12 else key_tag
        t = crypto.tag(k, s.prn_d, prn_a, gst_sf, i + 2, nmas,
                       _navdata(s.adkd, s.cop, s.navdata), ts_bits)
        bits += (crypto.bits_from_int(t, ts_bits)
                 + crypto.bits_from_int(tag_info(s.prn_d, s.adkd, s.cop), 16))
    bits += [0] * ((nt - 1 - len(slots)) * (ts_bits + 16))
    bits += crypto.bits_from_bytes(key_tx, ks_bits)
    return bits + [0] * (MACK_BITS - len(bits))


def parse_mack(bits480: list[int], ks_bits: int, ts_bits: int) -> dict:
    nt = nt_for(ks_bits, ts_bits)
    ib = crypto.int_from_bits
    tags = []
    pos = ts_bits + 16
    for _ in range(nt - 1):
        tag = ib(bits480[pos:pos + ts_bits])
        info = bits480[pos + ts_bits:pos + ts_bits + 16]
        tags.append((tag, ib(info[:8]), ib(info[8:12]), ib(info[12:])))
        pos += ts_bits + 16
    return {"tag0": ib(bits480[:ts_bits]),
            "macseq": ib(bits480[ts_bits:ts_bits + 12]),
            "cop0": ib(bits480[ts_bits + 12:ts_bits + 16]),
            "tags": tags,
            "key": crypto.bytes_from_bits(bits480[pos:pos + ks_bits])}
