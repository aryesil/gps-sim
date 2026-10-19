"""Weekly TESLA key chains with a checkpoint cache."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from backend.analysis.osnma import crypto

_SF_S = 30
_STRIDE = 256
_CHECKPOINTS: dict[tuple, dict[int, bytes]] = {}


@dataclass(frozen=True)
class ChainParams:
    chain_no: int
    gst0: int
    n_keys: int
    seed: bytes
    alpha: bytes
    ks_bits: int
    hf: int

    @property
    def cid(self) -> int:
        return self.chain_no % 4


def _derive(master_secret: bytes, label: bytes, chain_no: int) -> bytes:
    return hmac.new(master_secret, label + chain_no.to_bytes(4, "big"), hashlib.sha256).digest()


def chain_for(master_secret: bytes, chain_no: int, *, period_s: int = 604800,
              ks_bits: int = 128, hf: int = 0) -> ChainParams:
    return ChainParams(
        chain_no=chain_no, gst0=chain_no * period_s, n_keys=period_s // _SF_S,
        seed=_derive(master_secret, b"osnma-chain", chain_no)[:ks_bits // 8],
        alpha=_derive(master_secret, b"osnma-alpha", chain_no)[:6],
        ks_bits=ks_bits, hf=hf)


def chain_no_at(gst_sf: int, period_s: int = 604800) -> int:
    return gst_sf // period_s


def _checkpoints(cp: ChainParams) -> dict[int, bytes]:
    ck = (cp.seed, cp.alpha, cp.gst0, cp.n_keys, cp.hf)
    cps = _CHECKPOINTS.get(ck)
    if cps is None:
        cps = {cp.n_keys: cp.seed}
        key = cp.seed
        for j in range(cp.n_keys, 0, -1):
            key = crypto.tesla_step(key, cp.gst0 + _SF_S * (j - 2), cp.alpha, cp.hf)
            if (j - 1) % _STRIDE == 0:
                cps[j - 1] = key
        _CHECKPOINTS[ck] = cps
    return cps


def key_for_subframe(cp: ChainParams, gst_sf: int) -> bytes:
    if (gst_sf - cp.gst0) % _SF_S:
        raise ValueError("gst_sf is not on a subframe boundary")
    j = (gst_sf - cp.gst0) // _SF_S + 1
    if not 0 <= j <= cp.n_keys:
        raise ValueError(f"gst_sf {gst_sf} outside chain {cp.chain_no}")
    cps = _checkpoints(cp)
    c = min(cp.n_keys, (j + _STRIDE - 1) // _STRIDE * _STRIDE)
    key = cps[c]
    for i in range(c, j, -1):
        key = crypto.tesla_step(key, cp.gst0 + _SF_S * (i - 2), cp.alpha, cp.hf)
    return key


def key_tx(master_secret: bytes, gst_sf: int, *, period_s: int = 604800,
           ks_bits: int = 128, hf: int = 0) -> tuple[bytes, ChainParams]:
    cp = chain_for(master_secret, chain_no_at(gst_sf, period_s), period_s=period_s,
                   ks_bits=ks_bits, hf=hf)
    return key_for_subframe(cp, gst_sf), cp
