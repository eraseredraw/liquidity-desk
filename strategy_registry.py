# -*- coding: utf-8 -*-
"""strategy_registry.py — единый реестр стратегий.

Каждая стратегия — модуль с функцией check(yahoo_symbol, sym, cfg) -> dict|None.
Возврат: {"dir":"buy"|"sell", "entry":float, "sl":float, "tp":float,
           "rr":float, "strategy":str, "ts":str, ...}
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m5_sniper import check as m5_check, DEFAULTS as M5_DEFAULTS
from ny_open import check as ny_check
from strategy_signal import signal_from_features
from strategy import YAHOO_SYMBOLS, SYMBOL_PARAMS, fetch_candles, in_kill_zone, ema, find_swings

# ── Registry ────────────────────────────────────────────────────────────────

STRATEGIES = {}  # id -> {"name": str, "module": ..., "defaults": dict, "enabled": bool}


def register(id_: str, name: str, check_fn, defaults: dict = None, enabled: bool = True):
    STRATEGIES[id_] = {
        "id": id_,
        "name": name,
        "check": check_fn,
        "defaults": defaults or {},
        "enabled": enabled,
    }


# ── Strategy №1: M5 BOS (Break of Structure) ──────────────────────────────


def _check_m5(yahoo_symbol: str, sym: str, cfg: dict) -> dict | None:
    """Обёртка над m5_sniper.check() в единый формат."""
    params = SYMBOL_PARAMS.get(sym)
    if not params:
        return None
    # Нам нужен тренд — берём из стратегии через fetch_candles
    try:
        df1 = fetch_candles(yahoo_symbol, "1d", "2y")
        if df1 is None or len(df1) < 50:
            return None
        last = float(df1["close"].iloc[-1])
        ema50d = float(ema(df1["close"], 50).iloc[-1])
        d1_trend = "buy" if last > ema50d else "sell"
        h4 = ema(df1["close"], 50).iloc[-1]
        h4_trend = "bullish" if last > h4 else "bearish"
    except Exception:
        return None

    result = m5_check(yahoo_symbol, sym, params, cfg=cfg,
                      d1_trend=d1_trend, h4_trend=h4_trend)
    if not result:
        return None
    return {
        "dir": result["dir"],
        "entry": result["entry"],
        "sl": result["sl"],
        "tp": result["tp"],
        "rr": result["rr"],
        "strategy": result.get("strategy", "m5_sniper"),
        "ts": result.get("ts", datetime.now(timezone.utc).isoformat()),
        "symbol": sym,
        "kz_in": result.get("kz_in", False),
        "kz_name": result.get("kz_name"),
        "is_crypto": result.get("is_crypto", False),
        "h4_ok": result.get("h4_ok"),
        "d1_trend": d1_trend,
        "swing": result.get("swing"),
        "extra": {"swing": result.get("swing")},
    }


register("m5", "M5 Break of Structure", _check_m5, M5_DEFAULTS)


# ── Strategy №2: NY Open 5m Candle (Casper SMC) ───────────────────────────


def _check_ny(yahoo_symbol: str, sym: str, cfg: dict) -> dict | None:
    """Обёртка над ny_open.check() в единый формат."""
    result = ny_check(yahoo_symbol, sym=sym, cfg=cfg)
    if not result:
        return None
    return {
        "dir": result["dir"],
        "entry": result["entry"],
        "sl": result["sl"],
        "tp": result["tp"],
        "rr": result["rr"],
        "strategy": "ny_open",
        "ts": result.get("ts", datetime.now(timezone.utc).isoformat()),
        "symbol": sym,
        "kz_in": result.get("kz_in", False),
        "kz_name": result.get("kz_name"),
        "is_crypto": result.get("is_crypto", False),
        "extra": {
            "range_high": result.get("range_high"),
            "range_low": result.get("range_low"),
            "range_50pct": result.get("range_50pct"),
            "breakout_candle": result.get("breakout_candle_idx"),
            "confirm_candle": result.get("confirm_candle_idx"),
        },
    }


register("ny_open", "NY Open 5m Candle", _check_ny, M5_DEFAULTS)


# ── Strategy №3: Zone Liquidity (Institutional Levels) ────────────────────


def _check_zone(yahoo_symbol: str, sym: str, cfg: dict,
                _analysis: list = None) -> dict | None:
    """Сигнал по зонам ликвидности/поддержки/сопротивления.

    cfg ожидает:
      zone_pct: float — процент зоны (по умолчанию 0.5)
      atr_mult: float — множитель ATR для адаптивной зоны
    """
    from strategy import analyze_all

    try:
        if _analysis is None:
            analysis, _ = analyze_all(sym)
            if not analysis:
                return None
            a = analysis[0]
        else:
            a = _analysis[0] if _analysis else None
            if not a:
                return None
    except Exception:
        return None

    last = a["last_price"]
    zone_pct = cfg.get("zone_pct", 0.5)
    atr_mult = cfg.get("atr_mult")
    if atr_mult and a.get("atr_h1"):
        atr4_pct = a["atr_h1"] * 4 / last * 100
        zone_pct = min(1.5, max(0.1, atr4_pct * atr_mult))

    zones = []
    for key, side in (("liquidity_pool_below", "buy"), ("liquidity_pool_above", "sell"),
                      ("h4_support", "buy"), ("h4_resistance", "sell")):
        lvl = a.get(key)
        if not lvl:
            continue
        dist = abs(last - lvl) / last * 100
        zones.append({"level": lvl, "side": side, "dist_pct": round(dist, 2), "key": key})
    if not zones:
        return None
    zones.sort(key=lambda z: z["dist_pct"])
    nearest = zones[0]

    if nearest["dist_pct"] > zone_pct:
        return None

    side = nearest["side"]
    signal = "buy" if side == "buy" else "sell"
    entry = last
    if signal == "buy":
        sl = nearest["level"] * 0.995
        tp = a.get("nearest_resistance", nearest["level"] * 1.01)
        risk = entry - sl
        if risk <= 0:
            return None
        tp_val = entry + 2 * risk
        rr = 2.0
    else:
        sl = nearest["level"] * 1.005
        tp = a.get("nearest_support", nearest["level"] * 0.99)
        risk = sl - entry
        if risk <= 0:
            return None
        tp_val = entry - 2 * risk
        rr = 2.0

    min_rr = cfg.get("min_rr", 1.0)
    max_rr = cfg.get("max_rr", 6.0)
    if not (min_rr <= rr <= max_rr):
        return None

    kz_in, kz_name = in_kill_zone()
    is_crypto = bool((a.get("params") or {}).get("crypto"))

    return {
        "dir": signal,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp": round(tp_val, 5),
        "rr": round(rr, 2),
        "strategy": "zone_liquidity",
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": sym,
        "kz_in": kz_in,
        "kz_name": kz_name,
        "is_crypto": is_crypto,
        "extra": {
            "zone_key": nearest["key"],
            "zone_level": nearest["level"],
            "zone_dist_pct": nearest["dist_pct"],
            "signal": a.get("signal"),
            "trend_h1": a.get("trend_h1"),
            "confluence": a.get("confluence"),
        },
    }


register("zone", "Zone Liquidity", _check_zone,
         {"zone_pct": 0.5, "min_rr": 1.0, "max_rr": 6.0})


# ── Strategy №4: FXSSI Sentiment (розничный ratio, контрарианский) ────────


def _check_fxssi(yahoo_symbol: str, sym: str, cfg: dict) -> dict | None:
    """Сигнал по настроению рынка FXSSI (Current Ratio).

    Источник: fxssi.py — % покупателей от 10 брокеров (average, взвешенный).
    Контрарианская логика: розница в крайности (buy <= low_buy% или
    >= high_buy%) -> вход против толпы. Структурный фильтр: цена у пула
    ликвидности на стороне входа (как у Zone Liquidity).

    cfg:
      low_buy/high_buy: пороги % покупателей (по умолчанию 25/75)
      sl_atr/tp_atr: стоп/тейк в ATR(H1)
      min_rr/max_rr: рамки по RR
    """
    import time as _time

    from strategy import analyze_all

    try:
        from fxssi import get as fxssi_get
        s = fxssi_get(sym)
        if not s:
            return None
        analysis, _ = analyze_all(sym)
        if not analysis:
            return None
        a = analysis[0]
    except Exception:
        return None

    buy = s["buy"]
    low_buy = cfg.get("low_buy", 25)
    high_buy = cfg.get("high_buy", 75)

    if buy <= low_buy:
        signal = "buy"
        pool = a.get("liquidity_pool_below")
        reason = f"розница шортит ({buy:.1f}% buy) — контрарианский buy"
    elif buy >= high_buy:
        signal = "sell"
        pool = a.get("liquidity_pool_above")
        reason = f"розница в лонгах ({buy:.1f}% buy) — контрарианский sell"
    else:
        return None

    last = a["last_price"]
    if pool:
        dist = abs(last - pool) / last * 100
        pool_ok = dist <= cfg.get("zone_pct", 0.5)
    else:
        pool_ok = False
    if not pool_ok:
        return None

    atr_h1 = a.get("atr_h1")
    if not atr_h1:
        return None
    sl_atr = cfg.get("sl_atr", 1.0)
    tp_atr = cfg.get("tp_atr", 2.5)

    if signal == "buy":
        entry = last
        sl = entry - sl_atr * atr_h1
        tp = entry + tp_atr * atr_h1
    else:
        entry = last
        sl = entry + sl_atr * atr_h1
        tp = entry - tp_atr * atr_h1
    if sl <= 0 or tp <= 0:
        return None

    risk = abs(entry - sl)
    reward = abs(tp - entry)
    rr = round(reward / risk, 2) if risk > 0 else 0.0
    min_rr = cfg.get("min_rr", 1.8)
    max_rr = cfg.get("max_rr", 6.0)
    if not (min_rr <= rr <= max_rr):
        return None

    kz_in, kz_name = in_kill_zone()
    is_crypto = bool((a.get("params") or {}).get("crypto"))

    return {
        "dir": signal,
        "entry": round(entry, 5),
        "sl": round(sl, 5),
        "tp": round(tp, 5),
        "rr": rr,
        "strategy": "fxssi_sentiment",
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": sym,
        "kz_in": kz_in,
        "kz_name": kz_name,
        "is_crypto": is_crypto,
        "extra": {
            "reason": reason,
            "buy_pct": s["buy"],
            "sell_pct": s["sell"],
            "broker_spread": s.get("spread"),
            "formed": s.get("formed"),
            "signal": a.get("signal"),
            "trend_h1": a.get("trend_h1"),
            "confluence": a.get("confluence"),
        },
    }


register("fxssi", "FXSSI Sentiment (contrarian)", _check_fxssi,
         {"low_buy": 25, "high_buy": 75, "sl_atr": 1.0, "tp_atr": 2.5,
          "min_rr": 1.8, "max_rr": 6.0})


# ── API ────────────────────────────────────────────────────────────────────


def list_strategies() -> list:
    """Список всех зарегистрированных стратегий."""
    return [{"id": s["id"], "name": s["name"], "enabled": s["enabled"]}
            for s in STRATEGIES.values()]


def check_strategy(strategy_id: str, yahoo_symbol: str, sym: str,
                   cfg: dict = None) -> dict | None:
    """Запустить конкретную стратегию."""
    s = STRATEGIES.get(strategy_id)
    if not s or not s["enabled"]:
        return None
    merged = {**s["defaults"], **(cfg or {})}
    try:
        if strategy_id == "zone":
            _analysis = (cfg or {}).get("_analysis")
            return s["check"](yahoo_symbol, sym, merged, _analysis=_analysis)
        return s["check"](yahoo_symbol, sym, merged)
    except Exception:
        return None


def check_all_for_symbol(yahoo_symbol: str, sym: str,
                         config: dict = None) -> dict:
    """Запустить все стратегии для одного символа."""
    all_cfg = config or {}
    results = {}
    for sid, s in STRATEGIES.items():
        if not s["enabled"]:
            continue
        strategy_cfg = all_cfg.get(sid, {})
        if not strategy_cfg.get("enabled", True):
            continue
        result = check_strategy(sid, yahoo_symbol, sym, strategy_cfg)
        if result:
            results[sid] = result
    return results