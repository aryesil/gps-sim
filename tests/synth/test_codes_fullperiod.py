"""Kod üreteçlerinin TAM periyot doğrulaması (ICD'den bağımsız özelliklerle).

L5 XB indeks hatası (i+adv >= 10230'da sarma) yalnızca ilk 10 chip'i sınayan
testlerden kaçmıştı. Buradaki sınamalar tablolara değil, kodun matematiksel
yapısına dayanır:
  * LFSR kodları, karakteristik polinomun doğrusal özyinelemesini tüm
    uzunlukta sağlamalıdır (çıkış dizisi polinomu ve kayma yönünü sınar).
  * GPS/QZSS/SBAS/NavIC aynı Gold ailesidir: iki kod arasındaki çevrimsel
    çapraz korelasyon yalnızca {-65, -1, 63} değerlerini alır.
  * IS-GPS-200 Tablo 3-IIa'daki L2C PRN 1 "son durum" değerleri koda hiç
    girmez; N adım sonraki yazmaç durumu bunlarla eşleşmelidir.
"""
import numpy as np

from backend.synth import _lib


def _bits(chips):
    return (np.asarray(chips) < 0).astype(np.uint8)


def _recurrence_holds(seq, exponents):
    """s[n] == XOR_k s[n-k] (k = sıfırdan büyük üsler) tüm n için."""
    ex = [e for e in exponents if e > 0]
    m = max(ex)
    n = len(seq)
    acc = np.zeros(n - m, np.uint8)
    for k in ex:
        acc ^= seq[m - k:n - k]
    return np.array_equal(acc, seq[m:])


def _polymul_gf2(a, b):
    r = set()
    for x in a:
        for y in b:
            r ^= {x + y}
    return sorted(r)


# IS-GPS-200 3.2.1.4: 1 + x^3 + x^4 + x^5 + x^6 + x^9 + x^11 + x^13 + x^16
#                       + x^19 + x^21 + x^24 + x^27
_L2C_POLY = [0, 3, 4, 5, 6, 9, 11, 13, 16, 19, 21, 24, 27]


def test_l2c_cm_cl_satisfy_icd_polynomial_all_prns():
    for prn in list(range(1, 64)) + list(range(159, 211)):
        cm, cl = _lib.code_l2c(prn)
        assert _recurrence_holds(_bits(cm), _L2C_POLY), f"CM PRN{prn}"
        assert _recurrence_holds(_bits(cl), _L2C_POLY), f"CL PRN{prn}"


def _galois_state(init, n_steps):
    r = init
    for _ in range(n_steps):
        r = (r >> 1) ^ (0o445112474 if r & 1 else 0)
    return r


def test_l2c_prn1_end_states_match_icd_table():
    # IS-GPS-200 Tablo 3-IIa PRN 1: CM 742417664 -> 552566002 (10229 adım,
    # son chip'te), CL 624145772 -> 267724236 (767249 adım).
    assert _galois_state(0o742417664, 10229) == 0o552566002
    assert _galois_state(0o624145772, 767249) == 0o267724236


def test_b1i_all_prns_satisfy_g1_xor_g2_polynomial():
    # BDS-SIS-ICD-B1I 4.3: G1 = 1+x+x^7+x^8+x^9+x^10+x^11,
    # G2 = 1+x+x^2+x^3+x^4+x^5+x^8+x^9+x^11. Seçilen G2 çıkışı G2'nin
    # özyinelemesini sağlar, G1 ile XOR çarpım polinomunu.
    poly = _polymul_gf2([0, 1, 7, 8, 9, 10, 11], [0, 1, 2, 3, 4, 5, 8, 9, 11])
    for prn in range(1, 64):
        prim, _ = _lib.code(3, prn, 2046, 20)
        assert _recurrence_holds(_bits(prim)[:2046], poly), f"B1I PRN{prn}"


def test_b2a_data_and_pilot_satisfy_icd_polynomials_before_g1_reset():
    # G1 chip 8190'da sıfırlanır; ondan önceki bölüm iki LFSR'nin XOR'u.
    data_poly = _polymul_gf2([0, 1, 5, 11, 13], [0, 3, 5, 9, 11, 12, 13])
    pilot_poly = _polymul_gf2([0, 3, 6, 7, 13], [0, 1, 5, 7, 8, 12, 13])
    for prn in range(1, 64):
        bd, bp = _lib.code_b2a(prn)
        assert _recurrence_holds(_bits(bd)[:8190], data_poly), f"B2a-D PRN{prn}"
        assert _recurrence_holds(_bits(bp)[:8190], pilot_poly), f"B2a-P PRN{prn}"


def test_l1_gold_family_cross_correlation_is_three_valued():
    codes = ([_lib.code(0, p, 1023)[0] for p in range(1, 33)]
             + [_lib.code(1, p, 1023)[0] for p in range(193, 203)]
             + [_lib.code(2, p, 1023)[0] for p in range(120, 159)]
             + [_lib.code_navic(p) for p in range(1, 15)])
    f = [np.fft.fft(c.astype(float)) for c in codes]
    allowed = {-65, -1, 63}
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            c = np.round(np.fft.ifft(f[i] * np.conj(f[j])).real).astype(int)
            assert set(np.unique(c)) <= allowed, (i, j)
        # öz-korelasyon: tepe 1023, yan tepeler {-65, -1, 63}
        ac = np.round(np.fft.ifft(f[i] * np.conj(f[i])).real).astype(int)
        assert ac[0] == 1023 and set(np.unique(ac[1:])) <= allowed, i
