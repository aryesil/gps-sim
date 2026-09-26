// JS port of backend/synth/native/fading.cpp. Evaluating it at any run time
// t_s reproduces the exact per-block gain the C++ mixer folded into the IQ,
// so the per-SV power bars can track the waveform scrubber.
//   model 1 lognormal: splitmix64 of (seed, prn, knot), smoothstep between
//                      knots on a fixed coherence_s grid.
//   model 2 keyed:     ChaCha20 (RFC 8439) under a 256-bit key; only
//                      reproducible when the run recorded its key
//                      (sv.fading_key). Without it the curve is unknowable
//                      by design and this returns 0 dB.
(function (root) {
  const SMOOTH_VAR_COMP = 1.1602387022306428;  // sqrt(35/26)
  const TWO53 = 9007199254740992;

  function smooth(g0, g1, f) {
    const w = f * f * (3 - 2 * f);
    return g0 * (1 - w) + g1 * w;
  }

  // ---- lognormal ----------------------------------------------------------
  const M64 = (1n << 64n) - 1n;
  function mix64(x) {
    x = (x + 0x9E3779B97F4A7C15n) & M64;
    x = ((x ^ (x >> 30n)) * 0xBF58476D1CE4E5B9n) & M64;
    x = ((x ^ (x >> 27n)) * 0x94D049BB133111EBn) & M64;
    return (x ^ (x >> 31n)) & M64;
  }
  function u01n(h) { return Number(h >> 11n) * (1 / TWO53); }
  function logGauss(seed, prn, knot) {
    const kn = (BigInt(knot) * 0x100000001B3n) & M64;
    const base = mix64((BigInt.asUintN(64, BigInt(seed))
      ^ (BigInt(prn) << 40n) ^ kn) & M64);
    const u1 = u01n(mix64(base)) + 1e-12;
    const u2 = u01n(mix64(base ^ 0xABCDEFn));
    return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
  }
  function lognormalDb(sv, t_s) {
    const x = t_s / sv.fading_coherence_s, k0 = Math.floor(x);
    return sv.fading_sigma_db * SMOOTH_VAR_COMP * smooth(
      logGauss(sv.fading_seed, sv.prn, k0),
      logGauss(sv.fading_seed, sv.prn, k0 + 1), x - k0);
  }

  // ---- keyed (ChaCha20) ---------------------------------------------------
  function rotl(v, c) { return ((v << c) | (v >>> (32 - c))) >>> 0; }
  function chachaBlock(keyWords, counter, n0, n1, n2) {
    const s = [0x61707865, 0x3320646e, 0x79622d32, 0x6b206574,
      ...keyWords, counter >>> 0, n0 >>> 0, n1 >>> 0, n2 >>> 0];
    const x = s.slice();
    const qr = (a, b, c, d) => {
      x[a] = (x[a] + x[b]) >>> 0; x[d] = rotl(x[d] ^ x[a], 16);
      x[c] = (x[c] + x[d]) >>> 0; x[b] = rotl(x[b] ^ x[c], 12);
      x[a] = (x[a] + x[b]) >>> 0; x[d] = rotl(x[d] ^ x[a], 8);
      x[c] = (x[c] + x[d]) >>> 0; x[b] = rotl(x[b] ^ x[c], 7);
    };
    for (let r = 0; r < 10; r++) {
      qr(0, 4, 8, 12); qr(1, 5, 9, 13); qr(2, 6, 10, 14); qr(3, 7, 11, 15);
      qr(0, 5, 10, 15); qr(1, 6, 11, 12); qr(2, 7, 8, 13); qr(3, 4, 9, 14);
    }
    return x.map((v, i) => (v + s[i]) >>> 0);
  }
  function keyWordsOf(hex) {
    const w = [];
    for (let i = 0; i < 8; i++) {
      let v = 0;
      for (let j = 3; j >= 0; j--) v = v * 256 + parseInt(hex.substr(8 * i + 2 * j, 2), 16);
      w.push(v >>> 0);
    }
    return w;
  }
  // Eight 53-bit uniforms, same packing as keyed_draw() in fading.cpp.
  function draw(kw, domain, purpose, prn, knot) {
    const n0 = (((domain & 0xFF) << 24) | ((purpose & 0xFF) << 16) | (prn & 0xFFFF)) >>> 0;
    const k = BigInt.asUintN(64, BigInt(knot));
    const blk = chachaBlock(kw, 0, n0, Number(k & 0xFFFFFFFFn), Number(k >> 32n));
    const u = [];
    for (let i = 0; i < 8; i++) {
      const v = BigInt(blk[2 * i]) | (BigInt(blk[2 * i + 1]) << 32n);
      u.push(Number(v >> 11n) * (1 / TWO53));
    }
    return u;
  }
  const JITTER = 0.35;
  function knot(kw, dom, prn, k) {
    const u = draw(kw, dom, 0, prn, k);
    const g = Math.sqrt(-2 * Math.log(u[1] + 1e-12)) * Math.cos(2 * Math.PI * u[2]);
    return { pos: k + (2 * u[0] - 1) * JITTER, g };
  }
  function keyedDb(sv, t_s) {
    const kw = keyWordsOf(sv.fading_key);
    const dom = sv.sys ? sv.sys.charCodeAt(0) : 0;
    const p = draw(kw, dom, 1, sv.prn, 0);
    const spacing = sv.fading_coherence_s * (0.75 + 0.5 * p[1]);
    const x = t_s / spacing + p[0];
    let k = Math.floor(x);
    let a = knot(kw, dom, sv.prn, k);
    if (x < a.pos) { k -= 1; a = knot(kw, dom, sv.prn, k); }
    let b = knot(kw, dom, sv.prn, k + 1);
    if (x >= b.pos) { k += 1; a = b; b = knot(kw, dom, sv.prn, k + 1); }
    return sv.fading_sigma_db * SMOOTH_VAR_COMP * smooth(a.g, b.g, (x - a.pos) / (b.pos - a.pos));
  }

  root.fadingGainDb = function (sv, t_s) {
    const sigma = sv.fading_sigma_db, coh = sv.fading_coherence_s;
    if (!sv.fading_model || !(sigma > 0) || !(coh > 0)) return 0;
    if (sv.fading_model === 2) {
      return (typeof sv.fading_key === 'string' && sv.fading_key.length === 64)
        ? keyedDb(sv, t_s) : 0;
    }
    return lognormalDb(sv, t_s);
  };
  root._fadingChachaBlock = chachaBlock;
})(typeof window !== 'undefined' ? window : globalThis);
