"""OSNMA NMA header, DSM-KROOT, DSM-PKR and the HKROOT block schedule (SIS ICD 1.1 section 3.2)."""
from __future__ import annotations

import hashlib
from functools import lru_cache

from . import crypto, merkle, p256, tesla
from .crypto import bits_from_bytes, bits_from_int, bytes_from_bits
from .tables import (DSM_BLOCK_BITS, HKROOT_BITS, KS_BITS, NB_DK_BLOCKS, NB_DP_BLOCKS,
                     TS_BITS, code_of)

_SF_S = 30
_PKR_DSM_ID = 12


def nma_header(nmas: int, cid: int, cpks: int) -> int:
    return (nmas & 3) << 6 | (cid & 3) << 4 | (cpks & 7) << 1


def kroot_message(ks, cfg, cp, hour_gst: int, nmah: int) -> list[int]:
    kroot = tesla.key_for_subframe(cp, hour_gst - _SF_S)
    m = bits_from_int(nmah, 8) + bits_from_int(cp.cid, 2) + bits_from_int(1, 2)
    m += bits_from_int(cfg.hf, 2) + bits_from_int(cfg.mf, 2)
    m += bits_from_int(code_of(KS_BITS, cfg.ks_bits), 4)
    m += bits_from_int(code_of(TS_BITS, cfg.ts_bits), 4)
    m += bits_from_int(cfg.maclt, 8) + bits_from_int(0, 4)
    m += bits_from_int((hour_gst // 604800) % 4096, 12)
    m += bits_from_int((hour_gst % 604800) // 3600, 8)
    m += bits_from_bytes(cp.alpha) + bits_from_bytes(kroot, cfg.ks_bits)
    return m


def _pad(bits: list[int], nblocks: int, digest: bytes) -> list[int]:
    n = nblocks * DSM_BLOCK_BITS - len(bits)
    return bits + (bits_from_bytes(digest) + [0] * n)[:n]


@lru_cache(maxsize=64)
def _dsm_kroot(ks, cfg, cp, hour_gst: int, nmah: int, bad_sig: bool) -> tuple:
    m = kroot_message(ks, cfg, cp, hour_gst, nmah)
    mb = bytes_from_bits(m)
    sig = bytearray(p256.sign(ks.d, mb))
    if bad_sig:
        sig[0] ^= 0x80
    ds = bits_from_bytes(bytes(sig))
    body = m[8:] + ds
    need = 8 + len(body)
    nb = next(c for c in sorted(NB_DK_BLOCKS) if NB_DK_BLOCKS[c] * DSM_BLOCK_BITS >= need)
    bits = bits_from_int(nb, 4) + bits_from_int(ks.pkid, 4) + body
    digest = hashlib.sha256(mb + bytes(sig)).digest()
    return tuple(_pad(bits, NB_DK_BLOCKS[nb], digest))


def dsm_kroot(ks, cfg, cp, hour_gst: int, nmah: int, bad_sig: bool = False) -> list[int]:
    return list(_dsm_kroot(ks, cfg, cp, hour_gst, nmah, bad_sig))


@lru_cache(maxsize=8)
def _dsm_pkr(ks) -> tuple:
    leaf = merkle.leaf(1, ks.pkid, ks.npk)
    itn = merkle.path(ks.merkle_levels, ks.leaf_index)
    nb = 7
    bits = bits_from_int(nb, 4) + bits_from_int(ks.leaf_index, 4)
    for x in itn:
        bits += bits_from_bytes(x)
    bits += bits_from_int(1, 4) + bits_from_int(ks.pkid, 4) + bits_from_bytes(ks.npk)
    digest = hashlib.sha256(ks.merkle_root + leaf).digest()
    return tuple(_pad(bits, NB_DP_BLOCKS[nb], digest))


def dsm_pkr(ks) -> list[int]:
    return list(_dsm_pkr(ks))


@lru_cache(maxsize=64)
def _chain(master: bytes, no: int, period_s: int, ks_bits: int, hf: int):
    return tesla.chain_for(master, no, period_s=period_s, ks_bits=ks_bits, hf=hf)


def chain_at(ks, cfg, gst_sf: int):
    no = tesla.chain_no_at(gst_sf, cfg.chain_period_s)
    return _chain(ks.master_secret, no, cfg.chain_period_s, cfg.ks_bits, cfg.hf)


def hkroot(ks, cfg, prn: int, gst_sf: int, *, nmas: int | None = None, cid_off: int = 0,
           bad_sig: bool = False) -> list[int]:
    k = gst_sf // _SF_S
    cp = chain_at(ks, cfg, gst_sf)
    in_eoc = gst_sf >= cp.gst0 + cfg.chain_period_s - cfg.eoc_window_s
    nmah = nma_header(cfg.nmas if nmas is None else nmas, cp.cid + cid_off, 2 if in_eoc else 1)
    if k % cfg.pkr_every >= cfg.pkr_every - cfg.pkr_len:
        dsm_id, bits = _PKR_DSM_ID, dsm_pkr(ks)
    else:
        is_next = (k // cfg.pkr_every) % 2 if in_eoc else 0
        if is_next:
            use = _chain(ks.master_secret, cp.chain_no + 1, cfg.chain_period_s, cfg.ks_bits, cfg.hf)
            hour = use.gst0
        else:
            use, hour = cp, gst_sf - gst_sf % 3600
        dsm_id = 2 * ((gst_sf // 3600) % 6) + is_next
        bits = dsm_kroot(ks, cfg, use, hour, nmah, bad_sig)
    nb = len(bits) // DSM_BLOCK_BITS
    bid = (k + prn) % nb
    out = (bits_from_int(nmah, 8) + bits_from_int(dsm_id, 4) + bits_from_int(bid, 4)
           + bits[bid * DSM_BLOCK_BITS:(bid + 1) * DSM_BLOCK_BITS])
    assert len(out) == HKROOT_BITS
    return out
