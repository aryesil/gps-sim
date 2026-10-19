"""Live "Start Now" öncü süresi (LIVE_START_LEAD_S) için ölçüm ve kalibrasyon.

Sorun: Start Now, ilk segmentin GPS zamanını ``şimdi + lead`` olarak damgalar.
İlk örnek anten çıkışına ``ready + tx_buffer`` sonra ulaşır (ready = damgadan
ilk segmentin hazır olmasına kadar geçen süre: süreç havuzu spawn'ı, RINEX
çözümü, ilk segment üretimi). ``ready + tx_buffer > lead`` ise havadaki GPS
zamanı gerçek zamandan geri kalır (``lag > 0``), ``<`` ise ileri gider.

Sabit 2.5 s tek bir cihaz/örnekleme hızında ölçülmüştü. Burada:
  * her oturum ilk segment gecikmesini ölçüp raporlar (``lag_s``), ve
  * ``LIVE_START_LEAD_AUTO`` açıksa (varsayılan KAPALI) önceki oturumların
    ölçümlerinden bir sonraki öncü süre türetilir.

Çalışan bir oturumun zaman damgasını sonradan oynatmak segmentler arası
sürekliliği (kod/taşıyıcı fazı) bozacağından oturum içi düzeltme YAPILMAZ;
kalibrasyon yalnızca bir sonraki Start'ın öncü süresini etkiler. Tüm dosya
G/Ç'si en iyi çabayla yapılır: bozuk/yazılamayan dosya varsayılan öncü
süreye düşer, Start'ı asla engellemez.

Varsayılan Start Now artık sabit öncü süre kullanmaz (LIVE_START_SCHEDULED,
backend/session/play_schedule.py): zaman damgası yavaş başlangıç işleri
bittikten sonra seçilir ve TX başlatma komutu o ana kadar bekletilir; rapordaki
``lag_s`` ölçülen artık gecikmedir (hedef ~0). Buradaki sabit öncü süre yolu
LIVE_START_SCHEDULED=0 ile hâlâ kullanılabilir. ``gen_estimate_s`` /
``record_gen`` zamanlanmış yolun ilk-segment üretim süresi geçmişidir: yalnız
başlangıç gecikmesini etkiler (tahmin kısa kalırsa yeniden planlanır), havadaki
zamanın doğruluğunu etkilemez.
"""
from __future__ import annotations

import json
import statistics

from backend import config

_KEEP = 5            # anahtar başına saklanan son ölçüm sayısı
_MIN_LEAD_S = 0.5    # tek bir aykırı ölçümün öncü süreyi uçuramaması için sınırlar
_MAX_LEAD_S = 15.0
_FILE = "live_lead.json"


def make_key(kind: str, sample_rate: float, systems, band: str) -> str:
    """Üretim gecikmesini belirleyen parametreler: cihaz türü, örnekleme hızı,
    sistem kümesi, bant."""
    return f"{kind}:{int(round(sample_rate))}:{'+'.join(sorted(systems))}:{band}"


def tx_buffer_estimate_s(sample_rate: float, block_samples: int = 65536,
                         blocks: int = 2) -> float:
    """İlk segmentin ilk bloğunun DAC'a ulaşmasına kadar kalan donanım yolu
    tahmini: pompanın bir blok çekmesi + çekirdek tamponunun ilk dilimi."""
    return blocks * block_samples / max(float(sample_rate), 1.0)


def suggest_lead_s(samples, tx_buffer_s: float, *, margin_s: float = 0.3,
                   default_s: float | None = None) -> float:
    """Önceki ``ready`` ölçümlerinden öncü süre: medyan (tek tük aykırıya
    dayanıklı) + tx tamponu + pay, [_MIN_LEAD_S, _MAX_LEAD_S] aralığına
    kırpılır. Ölçüm yoksa ``default_s`` (varsayılan LIVE_START_LEAD_S)."""
    if default_s is None:
        default_s = config.LIVE_START_LEAD_S
    vals = [float(v) for v in (samples or []) if v == v and v >= 0.0]
    if not vals:
        return float(default_s)
    lead = statistics.median(vals) + float(tx_buffer_s) + float(margin_s)
    return float(min(max(lead, _MIN_LEAD_S), _MAX_LEAD_S))


def lag_s(ready_s: float, tx_buffer_s: float, lead_s: float) -> float:
    """> 0: havadaki GPS zamanı gerçek zamandan bu kadar geride."""
    return float(ready_s) + float(tx_buffer_s) - float(lead_s)


def _path():
    return config.DATA_DIR / _FILE


def _load() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def samples_for(key: str) -> list[float]:
    v = _load().get(key)
    return [float(x) for x in v] if isinstance(v, list) else []


def record(key: str, ready_s: float) -> None:
    """Ölçümü sakla (en iyi çaba). Geçersiz değerler yok sayılır."""
    if not (ready_s == ready_s) or ready_s < 0.0:
        return
    try:
        d = _load()
        cur = [x for x in d.get(key, []) if isinstance(x, (int, float))]
        d[key] = (cur + [round(float(ready_s), 3)])[-_KEEP:]
        tmp = _path().with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(_path())
    except Exception:
        pass


def lead_for(key: str, tx_buffer_s: float) -> float:
    """Start Now'ın kullanacağı öncü süre. LIVE_START_LEAD_AUTO kapalıysa
    (varsayılan) her zaman sabit ``LIVE_START_LEAD_S``."""
    if not config.LIVE_START_LEAD_AUTO:
        return float(config.LIVE_START_LEAD_S)
    return suggest_lead_s(samples_for(key), tx_buffer_s,
                          margin_s=config.LIVE_START_LEAD_MARGIN_S)


_GEN_PREFIX = "gen:"


def gen_estimate_s(key: str, default_s: float | None = None) -> float:
    """First-segment generation time to plan a scheduled Start Now with: the
    slowest of the last measurements for ``key`` (warm pool, so they are
    stable), else ``LIVE_SCHED_GEN_EST_S``."""
    if default_s is None:
        default_s = config.LIVE_SCHED_GEN_EST_S
    vals = [v for v in samples_for(_GEN_PREFIX + key) if v == v and v >= 0.0]
    if not vals:
        return float(default_s)
    return float(min(max(vals), _MAX_LEAD_S))


def record_gen(key: str, gen_s: float) -> None:
    """Store a measured first-segment generation time (best effort)."""
    record(_GEN_PREFIX + key, gen_s)


_RTT_PREFIX = "rtt:"


def play_latency_estimate_s(key: str, default_s: float) -> float:
    """FPGA start-command latency learned from earlier runs: half the median
    round trip of the armed enable (write the newline -> remote devmem ->
    ssh exit), bounded; ``default_s`` while there is no history."""
    vals = sorted(v for v in samples_for(_RTT_PREFIX + key) if v == v and v > 0.0)
    if not vals:
        return float(default_s)
    med = vals[len(vals) // 2]
    return float(min(max(med / 2.0, config.GNSS_PLAY_LATENCY_MIN_S),
                     config.GNSS_PLAY_LATENCY_MAX_S))


def record_enable_rtt(key: str, rtt_s: float) -> None:
    """Store a measured armed-enable round trip (best effort)."""
    record(_RTT_PREFIX + key, rtt_s)
