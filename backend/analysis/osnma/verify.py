"""Receiver-side OSNMA verifier (loose time sync: all timing comes from the page stamps)."""
from __future__ import annotations

import bisect
import hashlib
from collections import Counter
from dataclasses import dataclass, field

from . import crypto, merkle, p256
from .crypto import bits_from_bytes, bits_from_int, bytes_from_bits, int_from_bits
from .pages import split_page, subframe_of
from .tables import (ADKD0_MASK, DSM_BLOCK_BITS, KS_BITS, MACLT, NAVDATA_LEN, NB_DK_BLOCKS,
                     NB_DP_BLOCKS, TS_BITS, nt_for)

_SF_S = 30
_WEEK_S = 604800
_PKR_DSM_ID = 12
_DS_BITS = 512
_ITN_BITS = 1024
_NPK_BITS = {1: 264}
_MAX_FAILURES = 50
_MAX_STEPS = 30000


@dataclass
class VerifyReport:
    counts: Counter = field(default_factory=Counter)
    failures: list = field(default_factory=list)
    chain_ok: bool | None = None

    def fail(self, kind: str, prn=None, gst_sf=None, slot=None) -> None:
        if len(self.failures) < _MAX_FAILURES:
            self.failures.append({"kind": kind, "prn": prn, "gst_sf": gst_sf, "slot": slot})

    def summary(self) -> str:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        return f"chain_ok={self.chain_ok} {parts}"


@dataclass
class _Chain:
    cid: int
    alpha: bytes
    hf: int
    ks: int
    ts: int
    mf: int
    maclt: int
    auth: dict = field(default_factory=dict)
    times: list = field(default_factory=list)

    def add(self, t: int, key: bytes) -> None:
        if t not in self.auth:
            bisect.insort(self.times, t)
        self.auth[t] = key

    def step_down(self, key: bytes, t_from: int, t_to: int) -> bytes | None:
        if (t_from - t_to) // _SF_S > _MAX_STEPS:
            return None
        t = t_from
        while t > t_to:
            key = crypto.tesla_step(key, t - _SF_S, self.alpha, self.hf)
            t -= _SF_S
        return key

    def check(self, t: int, key: bytes) -> bool | None:
        if not self.times:
            return None
        if t in self.auth:
            return self.auth[t] == key
        i = bisect.bisect_left(self.times, t)
        if i > 0:
            a = self.times[i - 1]
            d = self.step_down(key, t, a)
            return None if d is None else d == self.auth[a]
        a = self.times[0]
        d = self.step_down(self.auth[a], a, t)
        return None if d is None else d == key

    def dist(self, t: int) -> int:
        return min(abs(t - a) for a in self.times) if self.times else 1 << 60


def pages_from_decoded(msgs: list[dict], prn: int, gst_abs0: int) -> dict[tuple[int, int], list[int]]:
    return {(prn, gst_abs0 + 2 * (m["sym_idx"] // 500)): list(m["page_bits"]) for m in msgs}


class Verifier:
    def __init__(self, pubkey: bytes | None = None, merkle_root: bytes | None = None):
        self.pubkey = pubkey
        self.merkle_root = merkle_root
        self._pks: dict[int, bytes] = {}
        self._chains: list[_Chain] = []
        self._words: dict[tuple[int, int], dict[int, list[int]]] = {}
        self._pages: dict[tuple[int, int], list[int]] = {}
        self._rep = VerifyReport()

    def run(self, pages: dict[tuple[int, int], list[int]]) -> VerifyReport:
        self._rep = VerifyReport()
        self._pages = pages
        self._pks, self._chains, self._words = {}, [], {}
        sfs = self._subframes(pages)
        self._collect_dsms(sfs)
        self._verify_keys(sfs)
        self._verify_tags(sfs)
        c = self._rep.counts
        self._rep.chain_ok = None if not (c["key_ok"] or c["key_bad"]) else c["key_bad"] == 0
        return self._rep

    def _subframes(self, pages) -> dict[tuple[int, int], dict]:
        raw: dict[tuple[int, int], dict[int, list[int]]] = {}
        for (prn, t), b in pages.items():
            if len(b) != 240 or (t - 1) % 2:
                continue
            sf, p = subframe_of(t)
            raw.setdefault((sf, prn), {})[p] = b
        out = {}
        for key, pp in raw.items():
            if len(pp) != 15:
                continue
            osn = [split_page(pp[p])[1] for p in range(15)]
            out[key] = {"hk": [x for o in osn for x in o[:8]],
                        "mack": [x for o in osn for x in o[8:]]}
        return dict(sorted(out.items()))

    def _sf_words(self, prn: int, sf: int) -> dict[int, list[int]]:
        k = (prn, sf)
        w = self._words.get(k)
        if w is None:
            w = {}
            for p in range(15):
                b = self._pages.get((prn, sf + 1 + 2 * p))
                if b is None or len(b) != 240:
                    continue
                word = split_page(b)[0]
                w.setdefault(int_from_bits(word[:6]), word)
            self._words[k] = w
        return w

    def _adkd0_data(self, prn: int, sf: int) -> list[int] | None:
        w = self._sf_words(prn, sf)
        out: list[int] = []
        for wt in sorted(ADKD0_MASK):
            if wt not in w:
                return None
            a, b = ADKD0_MASK[wt]
            out += w[wt][a:b]
        return out

    def _collect_dsms(self, sfs) -> None:
        state: dict[int, dict] = {}
        done: list[tuple[int, int, list[int], int]] = []
        seen: set = set()
        for (sf, prn), d in sfs.items():
            hk = d["hk"]
            hdr, dsm_id, bid = int_from_bits(hk[:8]), int_from_bits(hk[8:12]), int_from_bits(hk[12:16])
            block = hk[16:16 + DSM_BLOCK_BITS]
            st = state.get(dsm_id)
            if st is None or (bid in st["blocks"] and st["blocks"][bid] != block):
                st = state[dsm_id] = {"blocks": {}, "hdr": hdr}
            if bid == 0:
                st["hdr"] = hdr
            st["blocks"][bid] = block
            if 0 not in st["blocks"]:
                continue
            nbc = int_from_bits(st["blocks"][0][:4])
            nb = (NB_DP_BLOCKS if dsm_id == _PKR_DSM_ID else NB_DK_BLOCKS).get(nbc)
            if nb is None:
                state.pop(dsm_id)
                continue
            if all(i in st["blocks"] for i in range(nb)):
                bits = [x for i in range(nb) for x in st["blocks"][i]]
                state.pop(dsm_id)
                key = (dsm_id, tuple(bits), st["hdr"])
                if key not in seen:
                    seen.add(key)
                    done.append((dsm_id, sf, bits, st["hdr"]))
        for dsm_id, sf, bits, hdr in done:
            if dsm_id == _PKR_DSM_ID:
                self._pkr(bits, sf)
        for dsm_id, sf, bits, hdr in done:
            if dsm_id != _PKR_DSM_ID:
                self._kroot(bits, hdr, sf)

    def _pkr(self, bits: list[int], sf: int) -> None:
        cnt = self._rep.counts
        if self.merkle_root is None:
            return
        mid = int_from_bits(bits[4:8])
        itn = [bytes_from_bits(bits[8 + 256 * i:8 + 256 * (i + 1)]) for i in range(4)]
        pos = 8 + _ITN_BITS
        npkt, npkid = int_from_bits(bits[pos:pos + 4]), int_from_bits(bits[pos + 4:pos + 8])
        npk_bits = _NPK_BITS.get(npkt)
        if npk_bits is None:
            cnt["pkr_bad"] += 1
            self._rep.fail("pkr_npkt", gst_sf=sf)
            return
        pos += 8
        npk = bytes_from_bits(bits[pos:pos + npk_bits])
        pad = bits[pos + npk_bits:]
        leaf = merkle.leaf(npkt, npkid, npk)
        dig = bits_from_bytes(hashlib.sha256(self.merkle_root + leaf).digest())
        ok = merkle.root_from_path(leaf, mid, itn) == self.merkle_root and pad[:256] == dig[:len(pad)]
        if ok:
            cnt["pkr_ok"] += 1
            self._pks[npkid] = npk
        else:
            cnt["pkr_bad"] += 1
            self._rep.fail("pkr", gst_sf=sf)

    def _kroot(self, bits: list[int], hdr: int, sf: int) -> None:
        rep = self._rep
        ib = int_from_bits
        pkid, cid = ib(bits[4:8]), ib(bits[8:10])
        hf, mf = ib(bits[12:14]), ib(bits[14:16])
        kc, tc = ib(bits[16:20]), ib(bits[20:24])
        maclt = ib(bits[24:32])
        wn, towh = ib(bits[36:48]), ib(bits[48:56])
        alpha = bytes_from_bits(bits[56:104])
        if kc >= len(KS_BITS) or tc >= len(TS_BITS) or TS_BITS[tc] is None:
            rep.counts["kroot_sig_bad"] += 1
            rep.fail("kroot_params", gst_sf=sf)
            return
        ks, ts = KS_BITS[kc], TS_BITS[tc]
        end = 104 + ks
        mb = bytes_from_bits(bits_from_int(hdr, 8) + bits[8:end])
        sig = bytes_from_bits(bits[end:end + _DS_BITS])
        pad = bits[end + _DS_BITS:]
        pkb = self.pubkey if self.pubkey is not None else self._pks.get(pkid)
        if pkb is None:
            rep.counts["kroot_sig_bad"] += 1
            rep.fail("kroot_no_pubkey", gst_sf=sf)
            return
        try:
            ok = p256.verify(p256.decompress(pkb), mb, sig)
        except ValueError:
            ok = False
        dig = bits_from_bytes(hashlib.sha256(mb + sig).digest())
        if not (ok and pad[:256] == dig[:len(pad)]):
            rep.counts["kroot_sig_bad"] += 1
            rep.fail("kroot_sig", gst_sf=sf)
            return
        rep.counts["kroot_sig_ok"] += 1
        gst_k = wn * _WEEK_S + towh * 3600
        ch = next((c for c in self._chains if c.cid == cid and c.alpha == alpha), None)
        if ch is None:
            ch = _Chain(cid, alpha, hf, ks, ts, mf, maclt)
            self._chains.append(ch)
        ch.add(gst_k - _SF_S, bytes_from_bits(bits[104:end]))

    def _chain_for(self, cid: int, t: int) -> _Chain | None:
        cs = [c for c in self._chains if c.cid == cid]
        return min(cs, key=lambda c: c.dist(t)) if cs else None

    @staticmethod
    def _parse(d: dict, ch: _Chain) -> dict | None:
        nt = nt_for(ch.ks, ch.ts)
        m = d["mack"]
        w = ch.ts + 16
        if nt < 1:
            return None
        tags = []
        for i in range(1, nt):
            info = m[w * i + ch.ts:w * (i + 1)]
            tags.append((int_from_bits(m[w * i:w * i + ch.ts]), int_from_bits(info[:8]),
                         int_from_bits(info[8:12]), int_from_bits(info[12:]), int_from_bits(info)))
        return {"tag0": int_from_bits(m[:ch.ts]), "macseq": int_from_bits(m[ch.ts:ch.ts + 12]),
                "cop0": int_from_bits(m[ch.ts + 12:w]), "tags": tags,
                "key": bytes_from_bits(m[w * nt:w * nt + ch.ks])}

    def _verify_keys(self, sfs) -> None:
        rep = self._rep
        for (sf, prn), d in sfs.items():
            ch = self._chain_for(int_from_bits(d["hk"][2:4]), sf)
            if ch is None:
                continue
            p = self._parse(d, ch)
            if p is None:
                continue
            r = ch.check(sf, p["key"])
            if r is None:
                continue
            if r:
                rep.counts["key_ok"] += 1
                ch.add(sf, p["key"])
            else:
                rep.counts["key_bad"] += 1
                rep.fail("key", prn, sf)

    def _key_at(self, t: int) -> bytes | None:
        for c in self._chains:
            k = c.auth.get(t)
            if k is not None:
                return k
        return None

    def _verify_tags(self, sfs) -> None:
        rep = self._rep
        cnt = rep.counts
        for (sf, prn), d in sfs.items():
            hk = d["hk"]
            nmas = int_from_bits(hk[:2])
            ch = self._chain_for(int_from_bits(hk[2:4]), sf)
            if ch is None:
                continue
            p = self._parse(d, ch)
            if p is None:
                continue
            ka, kb = self._key_at(sf + _SF_S), self._key_at(sf + 11 * _SF_S)
            seqs = MACLT.get(ch.maclt)
            seq = () if seqs is None else seqs[0 if sf % 60 == 0 else 1]
            prev = sf - _SF_S

            def check(key, prn_d, ctr, tag, cop, adkd, data_prn):
                if key is None:
                    cnt["nokey"] += 1
                    return None
                if cop == 0:
                    nd = [0] * NAVDATA_LEN[adkd]
                else:
                    nd = self._adkd0_data(data_prn, prev)
                    if nd is None:
                        cnt["nodata"] += 1
                        return None
                return crypto.tag(key, prn_d, prn, sf, ctr, nmas, nd, ch.ts) == tag

            r = check(ka, None, 1, p["tag0"], p["cop0"], 0, prn)
            if r is not None:
                cnt["tag0_ok" if r else "tag0_bad"] += 1
                if not r:
                    rep.fail("tag0", prn, sf, 0)
            flx = [t[4] for i, t in enumerate(p["tags"], 1) if i < len(seq) and seq[i] == "FLX"]
            if ka is None:
                cnt["nokey"] += 1
            else:
                ok = crypto.macseq(ka, prn, sf, flx) == p["macseq"]
                cnt["macseq_ok" if ok else "macseq_bad"] += 1
                if not ok:
                    rep.fail("macseq", prn, sf)
            for i, (tag, prn_d, adkd, cop, _info) in enumerate(p["tags"], 1):
                if prn_d == 0 or adkd not in (0, 4, 12):
                    continue
                if adkd == 4 and cop > 0:
                    cnt["adkd4_skip"] += 1
                    continue
                r = check(kb if adkd == 12 else ka, prn_d, i + 1, tag, cop, adkd, prn_d)
                if r is None:
                    continue
                name = "dummy" if cop == 0 else f"adkd{adkd}"
                cnt[f"{name}_ok" if r else f"{name}_bad"] += 1
                if not r:
                    rep.fail(name, prn, sf, i)
