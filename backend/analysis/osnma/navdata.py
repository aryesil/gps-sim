"""ADKD navdata bit extraction from I/NAV words (OSNMA SIS ICD 1.1 annex C)."""
from __future__ import annotations

from .. import inav_encode
from .pages import split_page
from .tables import ADKD0_MASK, ADKD4_MASK


def _gather(words: dict[int, list[int]], mask: dict[int, tuple[int, int]]):
    out: list[int] = []
    for wt in sorted(mask):
        w = words.get(wt)
        if w is None:
            return None
        a, b = mask[wt]
        out += w[a:b]
    return out


def adkd0_bits(words: dict[int, list[int]]) -> list[int] | None:
    return _gather(words, ADKD0_MASK)


def adkd4_bits(words: dict[int, list[int]]) -> list[int] | None:
    return _gather(words, ADKD4_MASK)


def words_from_pages(pages240: list[list[int]]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for p in pages240:
        if not p:
            continue
        w = split_page(p)[0]
        out.setdefault(int("".join(map(str, w[:6])), 2), w)
    return out


def words_from_eph(eph: dict, gst_week: int, tow: int) -> dict[int, list[int]]:
    return {t: inav_encode.word_bits(t, eph, tow, gst_week) for t in range(1, 6)}
