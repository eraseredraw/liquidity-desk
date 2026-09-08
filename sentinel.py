# -*- coding: utf-8 -*-
"""sentinel.py — background market sentinel with Agent Loop.

Переписан на Agent Loop:
  - Стратегии №1 (M5 BOS), №2 (NY Open), №3 (Zone) — через strategy_registry
  - Agent Loop — маршрутизация → план → шаг → оценка → стоп → синтез
  - alert_type = "signal" — сигнал от стратегии с указанием id
  - alert_type = "zone" — обратно-совместимый зонный алерт
  - alert_type = "signal_change" — смена сигнала (neutral->buy/sell)

Usage:
  python sentinel.py            # run forever
  python sentinel.py --once     # single check
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
from strategy import YAHOO_SYMBOLS, SYMBOL_PARAMS, analyze_all, in_kill_zone
from agent_loop import run_loop
from strategy_registry import list_strategies

HERE = Path(__file__).parent
CONFIG = HERE / "sentinel_config.json"
STATE = HERE / "sentinel_state.json"
LOG = HERE / "sentinel.log"
DEFAULT_CONFIG = {
    "interval_min": 10,
    "zone_pct": 0.5,
    "zone_pct_atr_mult": 0.5,
    "symbols": ["XAUUSD", "GBPUSD", "EURUSD", "GBPJPY", "DAX"],
    "ny_open": {"enabled": True},
    "m5": {"enabled": True, "min_rr": 1.5, "max_rr": 6.0, "min_tp_dist": 0.0005, "dedup_h": 12},
    "zone": {"enabled": True, "zone_pct": 0.5, "min_rr": 1.0, "max_rr": 6.0},
}
ALERT_SOUNDS = {
    "zone": "msgbox",
    "signal": "beep",
    "signal_change": "beep",
}


def load_config() -> dict:
    if CONFIG.exists():
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def beep(alert_type: str) -> None:
    atype = ALERT_SOUNDS.get(alert_type, "msgbox")
    if winsound is None:
        return
    if atype == "beep":
        winsound.Beep(800, 300)
    elif atype == "msgbox":
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)


def _telegram(alert_type: str, **kw) -> None:
    try:
        from telegram_alert import send_alert
        send_alert(alert_type, **kw)
    except Exception:
        pass


def log(msg: str) -> None:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def check(cfg: dict) -> dict:
    """Основной цикл: Agent Loop по каждому символу."""
    alerts_dir = HERE / cfg.get("alerts_dir", "alerts")
    alerts_dir.mkdir(exist_ok=True)
    state = load_state()
    result = {"checked": dt.datetime.now(dt.timezone.utc).isoformat(),
              "symbols": {}, "alerts": []}

    symbols = cfg.get("symbols", DEFAULT_CONFIG["symbols"])

    for sym in symbols:
        yahoo = YAHOO_SYMBOLS.get(sym, sym)
        prev = state.get(sym, {})
        s = {"last_check": dt.datetime.now(dt.timezone.utc).isoformat()}

        # Один вызов analyze_all на символ для zone-стратегии и legacy-блоков
        analysis_cache = None
        try:
            analysis_cache, _ = analyze_all(sym)
        except Exception:
            analysis_cache = None

        # Пробрасываем кэш в Agent Loop через cfg
        loop_cfg = {**cfg, "_analysis": analysis_cache}
        loop_result = run_loop(sym, yahoo, loop_cfg, max_steps=6, trace=True)
        s["loop"] = {
            "strategies_run": loop_result["strategies_run"],
            "num_signals": len(loop_result["signals"]),
            "stop_reason": loop_result["stop_reason"],
        }
        result["symbols"][sym] = s

        now = dt.datetime.now(dt.timezone.utc)
        now_iso = now.isoformat()

        # ── Обработка сигналов от стратегий ──
        for sid, sig in loop_result["signals"].items():
            key = f"{sid}_{sig['dir']}"
            last_sig = prev.get(key)
            dedup_h = cfg.get(sid, {}).get("dedup_h", 12) if sid != "zone" else 0
            dedup = dt.timedelta(hours=dedup_h)
            if not last_sig or dt.datetime.fromisoformat(last_sig) < now - dedup:
                kz_tag = (f"[KZ: {sig['kz_name']}]" if sig.get("kz_in")
                          else ("[24/7 крипта]" if sig.get("is_crypto") else ""))
                dir_tag = "ЛОНГ" if sig["dir"] == "buy" else "ШОР"
                sid_name = {"ny_open": "NYOPEN", "m5": "M5", "zone": "ZONE"}.get(sid, sid.upper())
                extra = sig.get("extra", {})
                extra_parts = []
                if extra.get("range_low"):
                    extra_parts.append(f"range {extra['range_low']}-{extra['range_high']}")
                if extra.get("swing"):
                    extra_parts.append(f"swing {extra['swing']}")
                if extra.get("zone_key"):
                    extra_parts.append(f"{extra['zone_key']} @ {extra['zone_level']}")
                extra_str = f" ({', '.join(extra_parts)})" if extra_parts else ""

                msg = (f"SIGNAL {sym} [{sid_name}]{kz_tag}: {dir_tag} @ {sig['entry']} "
                       f"SL {sig['sl']} TP {sig['tp']} RR {sig['rr']}{extra_str}")
                beep("signal")
                _telegram("signal", sym=sym, direction=sig["dir"], entry=sig["entry"],
                          sl=sig["sl"], tp=sig["tp"], rr=sig["rr"],
                          strategy=sid, kz_tag=kz_tag.strip())
                log(f"[ALERT] {msg}")
                result["alerts"].append(msg)
                s[key] = now_iso
                s["alert_type"] = f"signal_{sid}"
                (alerts_dir / f"alert_{sym}_{sid}_{now.strftime('%Y%m%d_%H%M%S')}.txt").write_text(msg, encoding="utf-8")
            else:
                log(f"[INFO] {sym} [{sid}]: dedup (последний {last_sig})")

        state[sym] = s

    save_state(state)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Sentinel with Agent Loop")
    parser.add_argument("--once", action="store_true", help="single check")
    args = parser.parse_args()

    cfg = load_config()
    strats = [s["id"] for s in list_strategies()]
    log(f"Sentinel started. interval={cfg['interval_min']}min, "
        f"symbols={cfg['symbols']}, strategies={strats}")
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
    main()