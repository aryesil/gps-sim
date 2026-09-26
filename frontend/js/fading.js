// JS port of backend/synth/native/fading.cpp (ABI 29 land-mobile-satellite
// channel; it advances with the distance travelled -- sv.fading_motion, the
// route's profile, or a constant sv.fading_speed_mps). Evaluating it at any run time t_s reproduces the complex gain the
// C++ engine applied to a signal, so the per-SV power bars can track the
// waveform scrubber.
//   model 1 seeded: ChaCha20 key derived from sv.fading_seed.
//   model 2 keyed:  ChaCha20 under the run's 256-bit key; only reproducible
//                   when the run recorded it (sv.fading_key). Without it the
//                   channel is unknowable by design and this returns 0 dB.
(function (root) {
  const TWO53 = 9007199254740992;
  const C = 299792458.0;
  // [LOS, shadowed, blocked][mu, sigma, mp] dB; correlation distances (m);
  // LOS / shadowed probabilities at 10, 30, 50, 70, 90 deg elevation.
  const ENV = {
    open: { st: [[0, 0.3, -22], [-4, 1.5, -18], [-15, 2, -18]], dState: 30, dShadow: 5,
      pLos: [0.97, 0.99, 1, 1, 1], pSh: [0.03, 0.01, 0, 0, 0] },
    rural: { st: [[0, 0.8, -18], [-7, 2.5, -15], [-18, 3, -15]], dState: 12, dShadow: 3,
      pLos: [0.55, 0.70, 0.80, 0.88, 0.92], pSh: [0.35, 0.25, 0.17, 0.10, 0.07] },
    suburban: { st: [[0, 1, -16], [-8, 3, -14], [-20, 3, -14]], dState: 15, dShadow: 3,
      pLos: [0.45, 0.65, 0.78, 0.87, 0.92], pSh: [0.30, 0.22, 0.15, 0.09, 0.06] },
    urban: { st: [[0, 1, -14], [-9, 3, -12], [-22, 3, -13]], dState: 20, dShadow: 2,
      pLos: [0.15, 0.35, 0.55, 0.72, 0.80], pSh: [0.25, 0.30, 0.25, 0.18, 0.12] },
  };
  const TAU_STATE_MAX = 300, TAU_SHADOW_MAX = 60, DOPPLER_MIN = 0.01;
  const STATE_BLEND = 0.2, PER_TAU = 32, AR_TAPS = 192, SOS = 32;
  const P_STATE = 0, P_SHADOW = 1, P_DIFFUSE = 2;

  // ---- ChaCha20 (RFC 8439) ------------------------------------------------
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
  // Seeded model: one block under the zero key, nonce = seed (LE) + "SEED".
  function seedKeyWords(seed) {
    const s = BigInt.asUintN(64, BigInt(seed));
    return chachaBlock([0, 0, 0, 0, 0, 0, 0, 0], 0, Number(s & 0xFFFFFFFFn),
      Number(s >> 32n), 0x44454553).slice(0, 8);
  }
  function streamN0(domain, purpose, prn) {
    return (((domain & 0xFF) << 24) | ((purpose & 0xFF) << 16) | (prn & 0xFFFF)) >>> 0;
  }
  // Eight 53-bit uniforms, same packing as block_uniforms() in fading.cpp.
  function blockUniforms(kw, n0, band, b) {
    const bi = BigInt.asUintN(64, BigInt(b));
    const blk = chachaBlock(kw, 0, n0, Number(bi & 0xFFFFFFFFn),
      (Number((bi >> 32n) & 0xFFFFn) | ((band & 0xFFFF) << 16)) >>> 0);
    const u = [];
    for (let i = 0; i < 8; i++) {
      const v = BigInt(blk[2 * i]) | (BigInt(blk[2 * i + 1]) << 32n);
      u.push(Number(v >> 11n) * (1 / TWO53));
    }
    return u;
  }

  let AR = null;
  function arTaps() {
    if (AR) return AR;
    const rho = Math.exp(-1 / PER_TAU);
    const a = [];
    let e = 0;
    for (let j = 0; j < AR_TAPS; j++) { a.push(Math.pow(rho, j)); e += a[j] * a[j]; }
    const s = 1 / Math.sqrt(e);
    AR = a.map(v => v * s);
    return AR;
  }

  // Acklam's inverse standard normal CDF (same coefficients as fading.cpp).
  function invNorm(p) {
    const a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00];
    const b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01];
    const c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00];
    const d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00];
    const pl = 0.02425;
    if (p < pl || p > 1 - pl) {
      const q = Math.sqrt(-2 * Math.log(p < pl ? p : 1 - p));
      const v = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
        ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1);
      return p < pl ? v : -v;
    }
    const q = p - 0.5, r = q * q;
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q /
      (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
  }
  function smooth01(x) { return x <= 0 ? 0 : (x >= 1 ? 1 : x * x * (3 - 2 * x)); }

  // Exponentially correlated unit Gaussian on a grid (GridProcess), indexed
  // by u = distance / d_c + t / tau_max.
  function gridProcess(kw, domain, purpose, prn, dt) {
    const n0 = streamN0(domain, purpose, prn);
    const blocks = new Map();
    function normal(q) {
      const b = Math.floor(q / 8);
      let g = blocks.get(b);
      if (!g) {
        const u = blockUniforms(kw, n0, 0, b);
        g = [];
        for (let i = 0; i < 4; i++) {
          const r = Math.sqrt(-2 * Math.log(1 - u[2 * i]));
          g.push(r * Math.cos(2 * Math.PI * u[2 * i + 1]), r * Math.sin(2 * Math.PI * u[2 * i + 1]));
        }
        if (blocks.size > 4096) blocks.clear();
        blocks.set(b, g);
      }
      return g[q - 8 * b];
    }
    function value(m) {
      const a = arTaps();
      let x = 0;
      for (let i = m - (AR_TAPS - 1); i <= m; i++) x += a[m - i] * normal(i);
      return x;
    }
    return function at(u) {
      const x = u / dt, fl = Math.floor(x);
      const v0 = value(fl), v1 = value(fl + 1);
      return v0 + (x - fl) * (v1 - v0);
    };
  }

  function channel(sv) {
    const e = ENV[sv.fading_env] || ENV.suburban;
    const kw = sv.fading_model === 1 ? seedKeyWords(sv.fading_seed || 0)
      : keyWordsOf(sv.fading_key);
    const dom = sv.sys ? sv.sys.charCodeAt(0) : 0;
    // Motion: the route's distance profile (sv.fading_motion), else a
    // constant speed (null = static).
    const mo = sv.fading_motion;
    const useProfile = mo && mo.t_s && mo.t_s.length >= 2;
    const speed = useProfile ? 0 : Math.max(sv.fading_speed_mps || 0, 0);
    function distance(t) {
      if (!useProfile) return speed * t;
      const T = mo.t_s, D = mo.dist_m, n = T.length;
      if (t <= T[0]) return D[0];
      if (t >= T[n - 1]) return D[n - 1];
      let lo = 0, hi = n - 1;                    // upper_bound(T, t) - 1
      while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (T[mid] <= t) lo = mid; else hi = mid; }
      const dt = T[lo + 1] - T[lo];
      const f = dt > 0 ? (t - T[lo]) / dt : 0;
      return D[lo] + f * (D[lo + 1] - D[lo]);
    }
    const carrier = sv.fading_carrier_hz > 0 ? sv.fading_carrier_hz : 1575.42e6;
    const lambda = C / carrier;
    const el = Math.min(Math.max(typeof sv.fading_el_deg === 'number' ? sv.fading_el_deg : 45, 10), 90);
    const xi = (el - 10) / 20, i0 = Math.min(Math.trunc(xi), 3), fr = xi - i0;
    const pLos = e.pLos[i0] + fr * (e.pLos[i0 + 1] - e.pLos[i0]);
    const pSh = e.pSh[i0] + fr * (e.pSh[i0 + 1] - e.pSh[i0]);
    const cl = p => Math.min(Math.max(p, 1e-6), 1 - 1e-6);
    const z1 = invNorm(cl(pLos)), z2 = invNorm(cl(pLos + pSh));
    const state = gridProcess(kw, dom, P_STATE, sv.prn, 1 / PER_TAU);
    const shadow = gridProcess(kw, dom, P_SHADOW, sv.prn, 1 / PER_TAU);
    const band = Math.round(carrier / 1.023e6) & 0xFFFF;
    const n0 = streamN0(dom, P_DIFFUSE, sv.prn);
    const sf = [], sp = [];
    for (let b = 0; b < SOS / 4; b++) {
      const u = blockUniforms(kw, n0, band, b);
      for (let i = 0; i < 4; i++) {
        const n = 4 * b + i;
        sf.push(Math.cos(2 * Math.PI * (n + u[2 * i]) / SOS));
        sp.push(2 * Math.PI * u[2 * i + 1]);
      }
    }
    return function gain(t) {
      const d = distance(t);
      const z = state(d / e.dState + t / TAU_STATE_MAX);
      const x = shadow(d / e.dShadow + t / TAU_SHADOW_MAX);
      const r = d / lambda + DOPPLER_MIN * t;
      const s1 = smooth01((z - z1) / STATE_BLEND + 0.5);
      const s2 = smooth01((z - z2) / STATE_BLEND + 0.5);
      const p = [0, 1, 2].map(k => e.st[0][k] + s1 * (e.st[1][k] - e.st[0][k])
        + s2 * (e.st[2][k] - e.st[1][k]));
      let re = 0, im = 0;
      for (let n = 0; n < SOS; n++) {
        const ph = 2 * Math.PI * sf[n] * r + sp[n];
        re += Math.cos(ph); im += Math.sin(ph);
      }
      const ws = 1 / Math.sqrt(SOS);
      const direct = Math.pow(10, (p[0] + p[1] * x) / 20);
      const a = Math.pow(10, p[2] / 20);
      return [direct + a * re * ws, a * im * ws];
    };
  }

  const cache = new WeakMap();
  root.fadingGain = function (sv, t_s) {
    if (sv.fading_model !== 1 && sv.fading_model !== 2) return null;
    if (sv.fading_model === 2
        && !(typeof sv.fading_key === 'string' && sv.fading_key.length === 64)) return null;
    let g = cache.get(sv);
    if (!g) { g = channel(sv); cache.set(sv, g); }
    return g(t_s);
  };
  root.fadingGainDb = function (sv, t_s) {
    const g = root.fadingGain(sv, t_s);
    if (!g) return 0;
    return 10 * Math.log10(Math.max(g[0] * g[0] + g[1] * g[1], 1e-18));
  };
  root._fadingChachaBlock = chachaBlock;
})(typeof window !== 'undefined' ? window : globalThis);
