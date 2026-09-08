# -*- coding: utf-8 -*-
"""fxssi.py — настроение рынка FXSSI (Current Ratio, розничные позиции).

Источник: https://fxssi.com/api/current-ratios (JSON, без ключа).
Данные: % покупателей по каждой паре от 10 брокеров + взвешенный average.
Эндпоинт обновляется каждые ~5 минут — кэшируем (память + файл) на 15 минут.

Семантика: buy=70% → розница скучена в лонгах → контрарианский SELL-байас.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
CACHE_FILE = HERE / "fxssi_cache.json"

PAGE_URL = "https://fxssi.com/tools/current-ratio"
API_URL = "https://fxssi.com/api/current-ratios"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": PAGE_URL,
    "Accept": "application/json",
}

# Наши символы -> тикеры FXSSI
FXSSI_SYMBOLS = {
    "XAUUSD": "XAUUSD",
    "GBPUSD": "GBPUSD",
    "EURUSD": "EURUSD",
    "GBPJPY": "GBPJPY",
    "DAX": "GER40",
    "BTCUSD": "BTCUSD",
}

CACHE_TTL = 900  # 15 минут — данные на сайте обновляются раз в ~5 мин

_memory_cache = {"ts": 0.0, "data": None}


def _load_disk_cache() -> dict | None:
    try:
        if CACHE_FILE.exists():
            blob = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - blob.get("ts", 0) < CACHE_TTL:
                return blob.get("data")
    except Exception:
        pass
    return None


def _save_disk_cache(data: dict) -> None:
    try:
        CACHE_FILE.write_text(
            json.dumps({"ts": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def fetch_sentiment(ttl: int = CACHE_TTL, force: bool = False) -> dict | None:
    """Весь снапшот FXSSI: {pair: {buy, sell, spread, brokers, formed}} + meta.

    Кэш: память -> файл -> запрос. None при недоступности источника.
    """
    global _memory_cache
    now = time.time()
    if not force:
        mem = _memory_cache
        if mem["data"] and now - mem["ts"] < ttl:
            return mem["data"]
        disk = _load_disk_cache()
        if disk:
            _memory_cache = {"ts": now, "data": disk}
            return disk

    last_exc = None
    for attempt in range(2):
        try:
            s = requests.Session()
            s.headers.update({"User-Agent": HEADERS["User-Agent"]})
            page = s.get(PAGE_URL, timeout=20)  # даёт FXSSI_PUB_SESS — API без неё 404
            page.raise_for_status()
            r = s.get(API_URL, headers=HEADERS, timeout=15)
            r.raise_for_status()
            raw = r.json()
            brokers_raw = raw.get("brokers", {})
            pairs_raw = raw.get("pairs", {})
            weights = raw.get("broker_weights", {})

            pairs = {}
            for pair, brokers in pairs_raw.items():
                avg = brokers.get("average")
                if avg is None:
                    continue
                buy = float(avg)
                per_broker = {b: float(v) for b, v in brokers.items()
                              if b != "average" and 0.0 <= float(v) <= 100.0}
                pairs[pair] = {
                    "buy": round(buy, 2),
                    "sell": round(100.0 - buy, 2),
                    "spread": round(max(per_broker.values()) - min(per_broker.values()), 2)
                    if len(per_broker) > 1 else 0.0,
                    "brokers": per_broker,
                }

            data = {
                "pairs": pairs,
                "pair_weights": raw.get("pair_weights", {}),
                "broker_titles": raw.get("broker_titles", {}),
                "server_time": raw.get("server_time"),
                "server_time_text": raw.get("server_time_text"),
                "formed": raw.get("formed"),
                "fetched": datetime.now(timezone.utc).isoformat(),
            }
            _memory_cache["ts"] = now
            _memory_cache["data"] = data
            _save_disk_cache(data)
            return data
        except Exception as exc:
            last_exc = exc
            time.sleep(1 + attempt)
    return None


def get(sym: str, ttl: int = CACHE_TTL) -> dict | None:
    """Сентимент по нашему символу: {buy, sell, spread, brokers, ...} или None."""
    fxssi_pair = FXSSI_SYMBOLS.get(sym, sym)
    snap = fetch_sentiment(ttl=ttl)
    if not snap:
        return None
    entry = snap["pairs"].get(fxssi_pair)
    if not entry:
        return None
    out = dict(entry)
    out["pair"] = fxssi_pair
    out["formed"] = snap.get("formed")
    out["fetched"] = snap.get("fetched")
    return out


def bias(sym: str, low: float = 25.0, high: float = 75.0) -> dict:
    """Контрарианский байас по розничному ratio.

    buy <= low  → толпа в шортах → bias buy.
    buy >= high → толпа в лонгах → bias sell.
    Иначе neutral. Возвращает {} если данных нет.
    """
    s = get(sym)
    if not s:
        return {}
    b = s["buy"]
    if b <= low:
        direction, reason = "buy", f"розница шортит ({b:.1f}% buy) — контрарианский buy"
    elif b >= high:
        direction, reason = "sell", f"розница в лонгах ({b:.1f}% buy) — контрарианский sell"
    else:
        direction, reason = "neutral", f"без крайности ({b:.1f}% buy)"
    return {
        "bias": direction,
        "reason": reason,
        "buy_pct": round(b, 1),
        "sell_pct": round(100.0 - b, 1),
        "spread": s.get("spread"),
        "formed": s.get("formed"),
    }


if __name__ == "__main__":
    snap = fetch_sentiment(force=True)
    if not snap:
        print("FXSSI недоступен")
        raise SystemExit(1)
    print(f"Пар: {len(snap['pairs'])}, server_time: {snap['server_time_text'] if 'server_time_text' in snap else snap['server_time']}")
    for sym in FXSSI_SYMBOLS:
        s = get(sym)
        b = bias(sym)
        if s:
            print(f"{sym:8s} buy {s['buy']:5.1f}%  sell {s['sell']:5.1f}%  "
                  f"spread {s['spread']:4.1f}  bias={b.get('bias', '-'):8s} {b.get('reason', '')}")
        else:
            print(f"{sym:8s} нет данных")
