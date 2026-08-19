# -*- coding: utf-8 -*-
"""m5_sniper.py — снайперские M5 Break-of-Structure входы.

Правила (только уверенные сигналы, 1-2/день):
  1. Свежий M5 BOS: lower low (buy) / higher high (sell), последняя закрытая
     свеча пробивает уровень между свингами, анти-догон (вход в 0.15% от пробоя).
  2. TP = следующий swing-уровень в направлении, дистанция >= min_tp_dist,
     RR в [min_rr, max_rr] (SL за последним свингом).
  3. Направление = D1-тренд (EMA50).
  4. Форекс: только в Kill Zone. Крипта 24/7 — KZ не применима.
  5. H4-согласие — бонус-метка, не фильтр.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from strategy import fetch_candles, find_swings, in_kill_zone, ema

LOOKBACK = 40
DEFAULTS = {"min_rr": 1.5, "max_rr": 6.0, "min_tp_dist": 0.0005, "dedup_h": 12}


def _trends(yahoo_symbol: str, df1: pd.DataFrame) -> tuple:
    last = float(df1["close"].iloc[-1])
    ema50d = float(ema(df1["close"], 50).iloc[-1])
    d1 = "buy" if last > ema50d else "sell"
    h4 = ema(df1["close"], 50).iloc[-1]
    return d1, None


def check(yahoo_symbol: str, sym: str, params: dict = None, cfg: dict = None,
          d1_trend: str = None, h4_trend: str = None, at_ts=None) -> dict:
    """Проверка снайперского M5 BOS прямо сейчас. Возвращает dict или None."""
    try:
        cfg = {**DEFAULTS, **(cfg or {})}
        df5 = fetch_candles(yahoo_symbol, "5m", "1d")
        if len(df5) < LOOKBACK + 5:
            return None
        df5 = df5.tail(LOOKBACK)
        highs, lows, closes = df5["high"].values, df5["low"].values, df5["close"].values

        if d1_trend is None:
            df1 = fetch_candles(yahoo_symbol, "1d", "2y")
            d1_trend, h4_trend = _trends(yahoo_symbol, df1)
        if d1_trend not in ("buy", "sell"):
            return None

        is_crypto = bool((params or {}).get("crypto"))
        direction = d1_trend

        if direction == "buy":
            lows_sw = find_swings(df5, k=2, max_points=8)["swing_lows"]
            highs_sw = find_swings(df5, k=2, max_points=8)["swing_highs"]
            if len(lows_sw) < 2 or not highs_sw:
                return None
            last_sl, prev_sl = lows_sw[-1][1], lows_sw[-2][1]
            i_prev, i_last = df5.index.get_loc(lows_sw[-2][0]), df5.index.get_loc(lows_sw[-1][0])
            shb = float(max(highs[min(i_prev, i_last):max(i_prev, i_last) + 1]))
            if not (closes[-1] > shb):
                return None
            entry = float(closes[-1])
            sl = last_sl * 0.9995
            if entry <= sl or entry > shb * 1.0015:
                return None
            tp = min([h for h in highs_sw if h[1] > entry], default=None)
            if tp is None or (tp[1] - entry) / entry < cfg["min_tp_dist"]:
                return None
            rr = (tp[1] - entry) / (entry - sl)
            if not (cfg["min_rr"] <= rr <= cfg["max_rr"]):
                return None
            res = {"dir": "buy", "entry": round(entry, 5), "sl": round(sl, 5),
                   "tp": round(tp[1], 5), "rr": round(rr, 2), "swing": round(shb, 5)}
        else:
            highs_sw = find_swings(df5, k=2, max_points=8)["swing_highs"]
            lows_sw = find_swings(df5, k=2, max_points=8)["swing_lows"]
            if len(highs_sw) < 2 or not lows_sw:
                return None
            last_sh, prev_sh = highs_sw[-1][1], highs_sw[-2][1]
            i_prev, i_last = df5.index.get_loc(highs_sw[-2][0]), df5.index.get_loc(highs_sw[-1][0])
            slb = float(min(lows[min(i_prev, i_last):max(i_prev, i_last) + 1]))
            if not (closes[-1] < slb):
                return None
            entry = float(closes[-1])
            sl = last_sh * 1.0005
            if entry >= sl or entry < slb * 0.9985:
                return None
            tp = max([l for l in lows_sw if l[1] < entry], default=None)
            if tp is None or (entry - tp[1]) / entry < cfg["min_tp_dist"]:
                return None
            rr = (sl - entry) / (entry - tp[1])
            if not (cfg["min_rr"] <= rr <= cfg["max_rr"]):
                return None
            res = {"dir": "sell", "entry": round(entry, 5), "sl": round(sl, 5),
                   "tp": round(tp[1], 5), "rr": round(rr, 2), "swing": round(slb, 5)}

        now = pd.Timestamp.now(tz="UTC")
        if at_ts is not None:
            now = at_ts
        kz_in, kz_name = (False, None) if is_crypto else in_kill_zone(now)
        res["kz_in"] = kz_in
        res["kz_name"] = kz_name if kz_in else None
        res["is_crypto"] = is_crypto
        res["h4_ok"] = (h4_trend == direction) if h4_trend else None
        res["d1_trend"] = d1_trend
        res["ts"] = now.isoformat()
        res["symbol"] = sym
        return res
    except Exception:
        return None