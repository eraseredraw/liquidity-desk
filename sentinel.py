"""sentinel.py — background market sentinel. Always on, watches entry zones,
alerts on zone touch / signal change, survives errors, logs everything.

Checks every N minutes: if price enters a liquidity zone (pool/resistance
+/- zone_pct%) or signal changes (neutral -> buy/sell/wait), fires a
Windows sound alert, writes to log and saves report.

Usage:
  python sentinel.py            # run forever
  python sentinel.py --once     # single check (for Task Scheduler)
"""

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

try:
    import winsound
except ImportError:
    winsound = None

sys.path.insert(0, str(Path(__file__).parent))
from strategy import analyze_all, YAHOO_SYMBOLS, in_kill_zone

HERE = Path(__file__).parent
CONFIG = HERE / "sentinel_config.json"
STATE = HERE / "sentinel_state.json"
LOG = HERE / "sentinel.log"
DEFAULT_CONFIG = {
    "interval_min": 10,
    "zone_pct": 0.5,
    "zone_pct_atr_mult": 0.5,
    "symbols": ["XAUUSD", "GBPUSD", "EURUSD", "GBPJPY"],
    "alerts_dir": "alerts",
}
ALERT_SOUNDS = {
    "zone": 3,
    "signal": 2,
    "info": 1,
}


def log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC | {msg}"
    print(line, flush=True)
    try:
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.unlink()
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def beep(kind: str) -> None:
    if winsound is None:
        return
    try:
        for _ in range(ALERT_SOUNDS.get(kind, 1)):
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION if kind in ("zone", "signal") else winsound.MB_ICONASTERISK)
            time.sleep(0.15)
    except Exception:
        pass


def _telegram(kind: str, **kw) -> None:
    """Отправить алерт в Telegram, если настроен (опционально, никогда не падает)."""
    try:
        from telegram_alert import alert_zone, alert_signal_change
        if kind == "zone":
            alert_zone(kw["sym"], kw["price"], kw["zone_name"], kw["signal"], kw.get("score") or 0)
        elif kind == "signal":
            alert_signal_change(kw["sym"], kw["old_signal"], kw["new_signal"], kw["price"])
    except Exception:
        pass


def load_config() -> dict:
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    CONFIG.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(DEFAULT_CONFIG)


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def zones_for(a: dict, zone_pct: float) -> list:
    last = a["last_price"]
    zones = []
    for key, side in (("liquidity_pool_below", "buy"), ("liquidity_pool_above", "sell"),
                      ("h4_support", "buy"), ("h4_resistance", "sell")):
        lvl = a.get(key)
        if not lvl:
            continue
        dist = abs(last - lvl) / last * 100
        zones.append({"level": lvl, "side": side, "dist_pct": round(dist, 2), "key": key})
    zones.sort(key=lambda z: z["dist_pct"])
    return zones


def zone_threshold(a: dict, cfg: dict) -> float:
    """Адаптивная зона: 0.5 * (4*ATR_h1 как % от цены), кламп 0.1..1.5%.
    Если zone_pct_atr_mult не задан — статичный zone_pct (обратная совместимость)."""
    mult = cfg.get("zone_pct_atr_mult")
    atr = a.get("atr_h1")
    last = a.get("last_price")
    if mult and atr and last:
        atr4_pct = atr * 4 / last * 100
        return min(1.5, max(0.1, atr4_pct * mult))
    return cfg.get("zone_pct", 0.5)


def check(cfg: dict) -> dict:
    analysis, errors = analyze_all("ALL")
    alerts_dir = HERE / cfg["alerts_dir"]
    alerts_dir.mkdir(exist_ok=True)
    state = load_state()
    result = {"checked": dt.datetime.now(dt.timezone.utc).isoformat(), "symbols": {}, "alerts": []}
    for a in analysis:
        sym = a["symbol"]
        last = a["last_price"]
        zone_pct = zone_threshold(a, cfg)
        zones = zones_for(a, zone_pct)
        prev = state.get(sym, {})
        s = {
            "last_price": last,
            "signal": a["signal"],
            "trend": a["trend_h1"],
            "confluence": a["confluence"],
            "nearest_support": a["nearest_support"],
            "nearest_resistance": a["nearest_resistance"],
            "pool_below": a["liquidity_pool_below"],
            "pool_above": a["liquidity_pool_above"],
            "zones": zones[:4],
        }
        result["symbols"][sym] = s

        in_zone = any(z["dist_pct"] <= zone_pct for z in zones)
        prev_signal = prev.get("signal")
        prev_alert = prev.get("last_alert_ts")

        if in_zone:
            nearest = zones[0]
            side_text = "зона лонга (отбой/пробой вниз — решение по структуре)" if nearest["side"] == "buy" else "зона шорта или пробоя вверх — решение по структуре"
            kz_in, kz_name = in_kill_zone()
            is_crypto = bool((a.get("params") or {}).get("crypto"))
            kz_tag = f"[KZ: {kz_name}]" if kz_in else ("[24/7 крипта]" if is_crypto else "[вне KZ]")
            if not prev_alert or (dt.datetime.fromisoformat(prev_alert) < dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=cfg["interval_min"] * 2)):
                msg = (f"ЗОНА {sym} {kz_tag}: цена {last} вошла в зону {nearest['key']} = {nearest['level']} "
                       f"(дист {nearest['dist_pct']}%). Оценка: {side_text}. "
                       f"Структура: поддержка {a['nearest_support']}, сопротивление {a['nearest_resistance']}, "
                       f"тренд {a['trend_h1']}, confluence={a['confluence']}")
                beep("zone")
                _telegram("zone", sym=sym, price=last, zone_name=nearest["key"], signal=a["signal"], score=None)
                log(f"[ALERT] {msg}")
                result["alerts"].append(msg)
                s["last_alert_ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
                s["alert_type"] = "zone"
                (alerts_dir / f"alert_{sym}_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt").write_text(msg, encoding="utf-8")
        elif a["signal"] != "neutral" and prev_signal != a["signal"]:
            kz_in, kz_name = in_kill_zone()
            is_crypto = bool((a.get("params") or {}).get("crypto"))
            kz_tag = f"[KZ: {kz_name}]" if kz_in else ("[24/7 крипта]" if is_crypto else "[вне KZ]")
            msg = (f"СИГНАЛ {sym} {kz_tag}: {a['signal'].upper()} (было {prev_signal}). Цена {last}, "
                   f"пул {a['liquidity_pool_below']}/{a['liquidity_pool_above']}, тренд {a['trend_h1']}")
            beep("signal")
            _telegram("signal", sym=sym, price=last, old_signal=prev_signal, new_signal=a["signal"])
            log(f"[ALERT] {msg}")
            result["alerts"].append(msg)
            s["last_alert_ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
            s["alert_type"] = "signal"
            (alerts_dir / f"alert_{sym}_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt").write_text(msg, encoding="utf-8")

        if prev.get("signal") != a["signal"]:
            log(f"[INFO] {sym}: сигнал {prev.get('signal')} -> {a['signal']}")
        state[sym] = s
    save_state(state)
    if errors:
        log(f"[WARN] errors: {errors}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Background market sentinel")
    parser.add_argument("--once", action="store_true", help="single check, then exit")
    args = parser.parse_args()

    cfg = load_config()
    log(f"Sentinel started. interval={cfg['interval_min']}min, zone_pct={cfg['zone_pct']}% "
        f"(atr_mult={cfg.get('zone_pct_atr_mult')}), symbols={cfg['symbols']}")
    if args.once:
        check(cfg)
        return 0
    while True:
        try:
            check(cfg)
        except Exception as exc:
            log(f"[ERROR] {exc}")
        time.sleep(cfg["interval_min"] * 60)


if __name__ == "__main__":
    sys.exit(main())