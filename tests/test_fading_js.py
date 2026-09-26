"""frontend/js/fading.js must reproduce the native fading gain exactly, so
the scrubbed per-SV power bars show what the IQ actually carries."""
import json
import pathlib
import shutil
import subprocess

import pytest

from backend.synth import fading

_JS = pathlib.Path(__file__).parent.parent / "frontend" / "js" / "fading.js"
_KEY = bytes(range(7, 39)).hex()

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


def _node(expr: str):
    src = _JS.read_text() + f"\nconsole.log(JSON.stringify({expr}));\n"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout)


def test_js_chacha20_rfc8439_block_vector():
    words = _node("globalThis._fadingChachaBlock("
                  "[0x03020100,0x07060504,0x0b0a0908,0x0f0e0d0c,"
                  "0x13121110,0x17161514,0x1b1a1918,0x1f1e1d1c],"
                  "1, 0x09000000, 0x4a000000, 0)")
    got = b"".join(int(w).to_bytes(4, "little") for w in words).hex()
    assert got.startswith("10f1e7e4d13b5915500fdd1fa32071c4")


@pytest.mark.parametrize("model", ["lognormal", "keyed"])
def test_js_matches_native(model):
    cfg = fading.FadingConfig.from_dict(
        {"model": model, "sigma_db": 3.0, "coherence_s": 0.7, "seed": 11,
         "key": _KEY})
    cases = [("G", 5, 0.1), ("E", 5, 0.1), ("C", 30, 12.34), ("R", 3, 99.9)]
    svs = [{"sys": s, "prn": p, "fading_model": fading.MODEL_INT[model],
            "fading_sigma_db": 3.0, "fading_coherence_s": 0.7,
            "fading_seed": 11, "fading_key": _KEY} for s, p, _ in cases]
    js = _node("[" + ",".join(
        f"globalThis.fadingGainDb({json.dumps(sv)}, {t})"
        for sv, (_, _, t) in zip(svs, cases)) + "]")
    for (s, p, t), got in zip(cases, js):
        want = fading._gain_db(p, t, cfg, s)
        if model == "lognormal":
            # lognormal has no system domain (G5 == E5), kept for back-compat
            want = fading._gain_db(p, t, cfg, "G")
        assert got == pytest.approx(want, abs=1e-3), (s, p, t)


def test_js_keyed_without_recorded_key_is_flat():
    sv = {"sys": "G", "prn": 5, "fading_model": 2, "fading_sigma_db": 3.0,
          "fading_coherence_s": 1.0}
    assert _node(f"globalThis.fadingGainDb({json.dumps(sv)}, 4.2)") == 0
