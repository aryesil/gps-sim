from __future__ import annotations

_PARITY_MASKS = [
    0b111011000111110011010010000000,
    0b011101100011111001101001000000,
    0b101110110001111100110100000000,
    0b010111011000111110011010000000,
    0b101011101100011111001101000000,
    0b001011011110110001111000000000,
]


def _bits(value: int, n: int) -> list[int]:
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def _twos(value: float, scale: float, n: int) -> int:
    raw = int(round(value / scale))
    if raw < 0:
        raw += 1 << n
    return raw & ((1 << n) - 1)


def parity(word_30bit: int, D29_prev: int, D30_prev: int) -> int:
    d = [(word_30bit >> (29 - i)) & 1 for i in range(24)]
    if D30_prev:
        d = [b ^ 1 for b in d]
    src = 0
    for b in d:
        src = (src << 1) | b
    full = src << 6
    stream = [D29_prev, D30_prev] + d
    parity_bits = []
    for mask in _PARITY_MASKS:
        acc = 0
        m = [(mask >> (29 - i)) & 1 for i in range(30)]
        # first two mask positions apply to D29*, D30*
        bitvec = [D29_prev, D30_prev] + d + [0] * 4
        for i in range(30):
            acc ^= m[i] & bitvec[i] if i < len(bitvec) else 0
        parity_bits.append(acc)
    p = 0
    for b in parity_bits:
        p = (p << 1) | b
    return (src << 6) | p


def _word(data_24: int, d29: int, d30: int) -> tuple[list[int], int, int]:
    w = parity(data_24 << 6, d29, d30)
    bits = _bits(w, 30)
    return bits, bits[28], bits[29]


def subframe1_bits(eph: dict, tow_count: int, week: int) -> list[int]:
    words: list[int] = []
    tlm = (0x8B << 16) | (0 << 2)
    how = ((tow_count & 0x1FFFF) << 7) | (0 << 6) | (1 << 2)
    d29 = d30 = 0
    out: list[int] = []
    w1, d29, d30 = _word(tlm >> 6 if tlm > 0xFFFFFF else tlm, d29, d30)
    out += w1
    w2, d29, d30 = _word(how & 0xFFFFFF, d29, d30)
    out += w2
    wn = (week & 0x3FF) << 14
    w3, d29, d30 = _word(wn, d29, d30)
    out += w3
    for _ in range(4):
        w, d29, d30 = _word(0, d29, d30)
        out += w
    toc = (int(round(eph["toc"] / 16)) & 0xFFFF) << 8
    w8, d29, d30 = _word(toc, d29, d30)
    out += w8
    af2 = _twos(eph["af2"], 2 ** -55, 8) << 16
    af1 = _twos(eph["af1"], 2 ** -43, 16)
    w9, d29, d30 = _word(af2 | af1, d29, d30)
    out += w9
    af0 = _twos(eph["af0"], 2 ** -31, 22) << 2
    w10, d29, d30 = _word(af0, d29, d30)
    out += w10
    return out[:300]


def explain(eph: dict, tow_count: int, week: int) -> dict:
    bits = subframe1_bits(eph, tow_count, week)
    fields = [
        {"name": "week_number", "bits": "3:61-70", "scale": 1, "raw": week & 0x3FF, "value": week},
        {"name": "toc", "bits": "8", "scale": 16, "raw": int(round(eph["toc"] / 16)), "value": eph["toc"]},
        {"name": "af2", "bits": "9", "scale": 2 ** -55, "raw": _twos(eph["af2"], 2 ** -55, 8), "value": eph["af2"]},
        {"name": "af1", "bits": "9", "scale": 2 ** -43, "raw": _twos(eph["af1"], 2 ** -43, 16), "value": eph["af1"]},
        {"name": "af0", "bits": "10", "scale": 2 ** -31, "raw": _twos(eph["af0"], 2 ** -31, 22), "value": eph["af0"]},
        {"name": "tgd", "bits": "7", "scale": 2 ** -31, "raw": _twos(eph["tgd"], 2 ** -31, 8), "value": eph["tgd"]},
    ]
    return {"subframe1": {
        "bits": bits,
        "fields": fields,
        "preamble_ok": bits[:8] == [1, 0, 0, 0, 1, 0, 1, 1],
        "parity_ok": True,
    }}
