"""Stateless per-(PRN, subframe) OSNMA generator: HKROOT and MACK bits from a keyset and ephemeris."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np

from backend import geometry
from backend.analysis import inav_encode
from . import crypto, dsm, keys, mack, navdata, tesla
from .tables import HF, KS_BITS, MACLT, TS_BITS, nt_for

_WEEK_S = 604800
_SF_S = 30
FAULT_KINDS = ("bad_tag", "tamper_eph", "stale_key", "replay", "wrong_cid", "nmas",
               "bad_kroot_sig", "no_osnma")


@dataclass(frozen=True)
class Fault:
    kind: str
    prn: int | None = None
    from_gst: int | None = None
    to_gst: int | None = None
    n: int = 1
    slot: int = 1

    def active(self, prn: int, gst: int) -> bool:
        return ((self.prn is None or self.prn == prn)
                and (self.from_gst is None or gst >= self.from_gst)
                and (self.to_gst is None or gst < self.to_gst))


@dataclass(frozen=True)
class OsnmaConfig:
    nmas: int = 2
    ks_bits: int = 128
    ts_bits: int = 40
    maclt: int = 33
    hf: int = 0
    mf: int = 0
    chain_period_s: int = 604800
    eoc_window_s: int = 86400
    pkr_every: int = 20
    pkr_len: int = 4
    min_target_el_deg: float = 10.0
    no_osnma_prns: tuple[int, ...] = ()
    faults: tuple[Fault, ...] = ()
    keyset_path: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "OsnmaConfig":
        d = dict(d or {})
        unknown = set(d) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown OSNMA config keys: {sorted(unknown)}")
        if "no_osnma_prns" in d:
            d["no_osnma_prns"] = tuple(int(x) for x in d["no_osnma_prns"])
        if "faults" in d:
            fl = []
            for f in d["faults"]:
                try:
                    fl.append(f if isinstance(f, Fault) else Fault(**f))
                except TypeError as e:
                    raise ValueError(f"bad fault {f!r}: {e}") from None
            d["faults"] = tuple(fl)
        cfg = cls(**d)
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        if self.mf != 0:
            raise ValueError("only MF=0 (HMAC-SHA-256) is supported")
        if self.hf not in (HF.SHA256, HF.SHA3_256):
            raise ValueError(f"unsupported HF {self.hf}")
        if self.maclt not in MACLT:
            raise ValueError(f"unsupported MACLT {self.maclt}")
        if self.ks_bits not in KS_BITS or self.ts_bits not in TS_BITS:
            raise ValueError("KS/TS not in ICD tables")
        if len(MACLT[self.maclt][0]) > nt_for(self.ks_bits, self.ts_bits):
            raise ValueError("MACLT needs more tags than the MACK holds")
        for f in self.faults:
            if f.kind not in FAULT_KINDS:
                raise ValueError(f"unknown fault kind {f.kind!r}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["no_osnma_prns"] = list(self.no_osnma_prns)
        d["faults"] = [asdict(f) for f in self.faults]
        return d


@dataclass
class OsnmaContext:
    config: OsnmaConfig
    eph_now: dict[int, dict]
    eph_next: dict[int, dict] | None = None
    switch_gst: int | None = None
    data_start_gst: float = float("-inf")
    anchor_ecef: tuple[float, float, float] = (0.0, 0.0, 0.0)


_KEYSETS: dict[tuple, keys.Keyset] = {}


def _keyset(path: str) -> keys.Keyset:
    p = Path(path) if path else keys.DEFAULT_PATH
    try:
        ck = (str(p), p.stat().st_mtime_ns)
    except FileNotFoundError:
        ck = None
    ks = _KEYSETS.get(ck) if ck else None
    if ks is None:
        ks = keys.load_or_create(p)
        _KEYSETS.clear()
        _KEYSETS[(str(p), p.stat().st_mtime_ns)] = ks
    return ks


class OsnmaGenerator:
    def __init__(self, ctx: OsnmaContext):
        self.ctx = ctx
        self.cfg = ctx.config
        self.ks = _keyset(self.cfg.keyset_path)
        self._sf: dict[tuple, tuple[list[int], list[int]]] = {}
        self._el: dict[tuple, float] = {}
        self._adkd0_cache: dict[tuple, list[int] | None] = {}
        self._adkd4_cache: dict[tuple, list[int] | None] = {}

    def _after_switch(self, gst_abs: int) -> bool:
        c = self.ctx
        return c.switch_gst is not None and c.eph_next is not None and gst_abs >= c.switch_gst

    def eph_for(self, prn: int, gst_abs: int) -> dict | None:
        c = self.ctx
        return (c.eph_next if self._after_switch(gst_abs) else c.eph_now).get(prn)

    def _prns(self, gst_abs: int) -> list[int]:
        c = self.ctx
        return sorted(c.eph_next if self._after_switch(gst_abs) else c.eph_now)

    def _fault(self, kind: str, prn: int, gst: int) -> Fault | None:
        for f in self.cfg.faults:
            if f.kind == kind and f.active(prn, gst):
                return f
        return None

    def tamper_word(self, prn: int, gst_abs: int, wtype: int, word: list[int]) -> list[int]:
        if wtype == 1 and self._fault("tamper_eph", prn, gst_abs):
            word = list(word)
            word[20] ^= 1
        return word

    def _key(self, gst: int) -> bytes:
        c = self.cfg
        return tesla.key_tx(self.ks.master_secret, gst, period_s=c.chain_period_s,
                            ks_bits=c.ks_bits, hf=c.hf)[0]

    def _cop(self, gst_sf: int) -> int:
        c = self.ctx
        start = c.switch_gst if self._after_switch(gst_sf) else c.data_start_gst
        if start == float("-inf"):
            return 15
        return int(min(max((gst_sf - start) // _SF_S, 0), 15))

    def _adkd0(self, prn: int, gst_sf: int) -> list[int] | None:
        prev = gst_sf - _SF_S
        ck = (prn, prev)
        if ck not in self._adkd0_cache:
            eph = self.eph_for(prn, prev)
            bits = None
            if eph is not None:
                w = navdata.words_from_eph(eph, prev // _WEEK_S, prev % _WEEK_S + 1)
                bits = navdata.adkd0_bits(w)
            if len(self._adkd0_cache) > 64:
                self._adkd0_cache.clear()
            self._adkd0_cache[ck] = bits
        return self._adkd0_cache[ck]

    def _adkd4(self, prn: int, gst_sf: int) -> list[int] | None:
        prev = gst_sf - _SF_S
        ck = (prn, prev)
        if ck not in self._adkd4_cache:
            eph = self.eph_for(prn, prev)
            bits = None
            if eph is not None:
                wk, tow = prev // _WEEK_S, prev % _WEEK_S
                w = {t: inav_encode.word_bits(t, eph, tow + 1 + 2 * p, wk)
                     for p, t in ((2, 6), (4, 10)) if inav_encode.osnma_wtype(tow + 1 + 2 * p) == t}
                bits = navdata.adkd4_bits(w)
            if len(self._adkd4_cache) > 64:
                self._adkd4_cache.clear()
            self._adkd4_cache[ck] = bits
        return self._adkd4_cache[ck]

    def _elevation(self, prn: int, gst_sf: int) -> float:
        ck = (gst_sf, prn)
        if ck not in self._el:
            a = np.asarray(self.ctx.anchor_ecef, float)
            eph = self.eph_for(prn, gst_sf)
            if eph is None:
                el = -90.0
            elif not a.any():
                el = 90.0
            else:
                el = geometry.observables(eph, a, float(gst_sf % _WEEK_S))["el_deg"]
            if len(self._el) > 256:
                self._el.clear()
            self._el[ck] = el
        return self._el[ck]

    def _targets(self, prn_a: int, gst_sf: int) -> list[int]:
        cand = [p for p in self._prns(gst_sf) if p != prn_a
                and self._elevation(p, gst_sf) >= self.cfg.min_target_el_deg]
        if not cand:
            return []
        r = (gst_sf // _SF_S + prn_a) % len(cand)
        return cand[r:] + cand[:r]

    def _adkd4_slot(self, prn_a: int, gst_sf: int, cop: int) -> mack.TagSlot:
        nav = self._adkd4(prn_a, gst_sf) if cop else None
        if nav is None:
            return mack.TagSlot(prn_a, 4, 0, ())
        return mack.TagSlot(prn_a, 4, cop, tuple(nav))

    def _slots(self, prn_a: int, gst_sf: int, cop: int):
        seq = mack.slot_sequence(self.cfg.maclt, gst_sf)
        targets = self._targets(prn_a, gst_sf)
        others = [p for p in self._prns(gst_sf) if p != prn_a]
        dummy_d = others[0] if others else prn_a
        slots, flx, e = [], [], 0
        for i, code in enumerate(seq[1:], 1):
            if code == "FLX":
                adkd, kind = 0, "E"
                flx.append(i)
            else:
                adkd, kind = int(code[:2]), code[2]
            if adkd == 4:
                slots.append(self._adkd4_slot(prn_a, gst_sf, cop))
                continue
            if kind == "S":
                prn_d, c = prn_a, cop
            elif e < len(targets):
                prn_d, c = targets[e], cop
                e += 1
            else:
                prn_d, c = dummy_d, 0
            nav = self._adkd0(prn_d, gst_sf) if c else None
            if nav is None:
                c, nav = 0, ()
            slots.append(mack.TagSlot(prn_d, adkd, c, tuple(nav)))
        return slots, tuple(flx)

    def _mack(self, prn: int, gst_sf: int, nmas: int) -> list[int]:
        cfg = self.cfg
        cop = self._cop(gst_sf)
        nav0 = self._adkd0(prn, gst_sf) if cop else None
        if nav0 is None:
            cop, nav0 = 0, ()
        slots, flx = self._slots(prn, gst_sf, cop)
        return mack.build_mack(
            prn_a=prn, gst_sf=gst_sf, nmas=nmas, ks_bits=cfg.ks_bits, ts_bits=cfg.ts_bits,
            key_tag=self._key(gst_sf + _SF_S), key_slow=self._key(gst_sf + 11 * _SF_S),
            key_tx=self._key(gst_sf), tag0_navdata=nav0, tag0_cop=cop, slots=slots,
            flx_idx=flx)

    def _build(self, prn: int, gst_sf: int, faults: bool = True):
        cfg = self.cfg
        if prn in cfg.no_osnma_prns or (faults and self._fault("no_osnma", prn, gst_sf)):
            return [0] * 120, [0] * 480
        f = (lambda k: self._fault(k, prn, gst_sf)) if faults else (lambda k: None)
        rp = f("replay")
        if rp:
            return self._build(prn, gst_sf - _SF_S * rp.n, faults=False)
        nf = f("nmas")
        nmas = nf.n if nf else cfg.nmas
        hk = dsm.hkroot(self.ks, cfg, prn, gst_sf, nmas=nmas,
                        cid_off=1 if f("wrong_cid") else 0, bad_sig=bool(f("bad_kroot_sig")))
        mk = self._mack(prn, gst_sf, nmas)
        step = cfg.ts_bits + 16
        sk = f("stale_key")
        if sk:
            kpos = nt_for(cfg.ks_bits, cfg.ts_bits) * step
            mk[kpos:kpos + cfg.ks_bits] = crypto.bits_from_bytes(
                self._key(gst_sf - _SF_S * sk.n), cfg.ks_bits)
        bt = f("bad_tag")
        if bt and 0 <= (bt.slot - 1) * step < 480:
            mk[(bt.slot - 1) * step] ^= 1
        return hk, mk

    def subframe(self, prn: int, gst_sf: int) -> tuple[list[int], list[int]]:
        ck = (prn, gst_sf)
        r = self._sf.get(ck)
        if r is None:
            if len(self._sf) > 128:
                self._sf.clear()
            r = self._sf[ck] = self._build(prn, gst_sf)
        return r

    def page_bits(self, prn: int, gst_abs: int) -> list[int]:
        off = (gst_abs - 1) % _SF_S
        p = off // 2
        hk, mk = self.subframe(prn, gst_abs - 1 - off)
        return hk[8 * p:8 * p + 8] + mk[32 * p:32 * p + 32]
