"""grid_backtest.py вЂ” per-symbol parameter grid search.

Uses the SAME signal logic as live/backtest (signal.py). Precomputes
liquidity levels once per bar, then simulates grid: zone buffer, SL
buffer, TP buffer, hold, swing-k, trend filter. Picks best config per
symbol by expectancy (>=10 closed trades), reports 1y OOS. Saved to
backtest_grid.json.
"""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from strategy import YAHOO_SYMBOLS, ema, fetch_candles, find_swings
from backtest import build_h1_ema200, build_h4_ema50, build_w_ema50
from strategy_signal import signal_from_features

HERE = Path(__file__).parent
MIN_BARS = 260

ZONES = [1.005, 1.01, 1.015]
SLS = [0.993, 0.995, 0.998]
TPS = [1.003, 1.005, 1.008]
HOLDS = [5, 10, 20]
KS = [1, 2]
FILTERS = ["h4", "d1", "both", "w1", "w1d1", "w1h4"]
DEFAULT = {"zone": 1.01, "sl_buf": 0.995, "tp_buf": 1.005, "hold": 10, "k": 2, "filter_": "h4"}


def precompute_levels(df: pd.DataFrame, k: int, h1_ema200: pd.Series, h4_ema50: pd.Series,
                      w_ema50: pd.Series) -> list:
    levels = []
    for i in range(MIN_BARS, len(df) - 1):
        window = df.iloc[: i + 1]
        last = float(df["close"].iloc[i])
        swings = find_swings(window, k=k, max_points=6)
        sh = [p for _, p in swings["swing_highs"]]
        sl = [p for _, p in swings["swing_lows"]]
        eq = (float(window["high"].max()) + float(window["low"].min())) / 2
        ema50d = float(ema(window["close"], 50).iloc[-1])
        ts = df.index[i]
        ema200_val = h1_ema200.get(ts)
        if ema200_val is None or pd.isna(ema200_val):
            ema200_val = ema50d
        h4_val = h4_ema50.get(ts)
        if h4_val is None or pd.isna(h4_val):
            h4_val = ema50d
        w_val = w_ema50.get(ts)
        w1_trend = ("bullish" if last > w_val else "bearish"
                    if w_val is not None and not pd.isna(w_val) else None)
        levels.append({
            "last": last,
            "date": df.index[i + 1],
            "open_next": float(df["open"].iloc[i + 1]),
            "liquidity_sell": min([lvl for lvl in sh if lvl > last], default=None),
            "liquidity_buy": max([lvl for lvl in sl if lvl < last], default=None),
            "nearest_above": min([lvl for lvl in sh + [eq] if lvl > last], default=None),
            "nearest_below": max([lvl for lvl in sl + [eq] if lvl < last], default=None),
            "ema200": ema200_val,
            "h4_trend": "bullish" if last > h4_val else "bearish",
            "d1_trend": "bullish" if last > ema50d else "bearish",
            "w1_trend": w1_trend,
            "highs": [float(x) for x in df["high"].iloc[i + 1 :]],
            "lows": [float(x) for x in df["low"].iloc[i + 1 :]],
        })
    return levels


def simulate(levels: list, zone: float, sl_buf: float, tp_buf: float, hold: int,
             k: int, filter_: str) -> dict:
    params = {"zone": zone, "sl_buf": sl_buf, "tp_buf": tp_buf, "hold": hold, "k": k, "filter": filter_}
    trades = []
    for lv in levels:
        info = signal_from_features(lv["last"], lv["ema200"], lv["h4_trend"], lv["d1_trend"],
                                    lv["liquidity_buy"], lv["liquidity_sell"],
                                    lv["nearest_below"], lv["nearest_above"], params,
                                    w1_trend=lv.get("w1_trend"))
        signal = info["signal"]
        if signal not in ("buy", "sell"):
            continue
        entry = lv["open_next"]
        sl, tp = info["sl"], info["tp"]
        if not sl or not tp:
            continue
        if signal == "buy" and not (sl < entry < tp):
            continue
        if signal == "sell" and not (tp < entry < sl):
            continue
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk <= 0 or reward <= 0:
            continue
        result, exit_price = "flat", entry
        for j in range(min(hold, len(lv["highs"]))):
            hi, lo = lv["highs"][j], lv["lows"][j]
            if signal == "buy":
                if lo <= sl:
                    result, exit_price = "loss", sl
                    break
                if hi >= tp:
                    result, exit_price = "win", tp
                    break
            else:
                if hi >= sl:
                    result, exit_price = "loss", sl
                    break
                if lo <= tp:
                    result, exit_price = "win", tp
                    break
        r = (exit_price - entry) / risk if signal == "buy" else (entry - exit_price) / risk
        trades.append(r)
    closed = [r for r in trades if r != 0]
    if len(closed) < 10:
        return {"trades": len(closed), "exp": None, "winrate": None, "total": None, "maxdd": None}
    wins = [r for r in closed if r > 0]
    equity, peak, dd = 0.0, 0.0, 0.0
    for r in closed:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return {
        "trades": len(closed),
        "exp": sum(closed) / len(closed),
        "winrate": round(len(wins) / len(closed) * 100, 1),
        "total": round(sum(closed), 2),
        "maxdd": round(dd, 2),
    }


def main() -> int:
    out = {"generated": pd.Timestamp.now(tz="UTC").isoformat(), "symbols": {}}
    for name, yahoo in YAHOO_SYMBOLS.items():
        try:
            df = fetch_candles(yahoo, "1d", "2y")
            if len(df) < MIN_BARS:
                out["symbols"][name] = {"error": f"not enough bars ({len(df)})"}
                continue
            h1_ema200 = build_h1_ema200(yahoo, df.index)
            h4_ema50 = build_h4_ema50(yahoo, df.index)
            w_ema50 = build_w_ema50(yahoo, df.index)
            levels_cache = {k: precompute_levels(df, k, h1_ema200, h4_ema50, w_ema50) for k in KS}
            best = None
            results = []
            for zone in ZONES:
                for sl_buf in SLS:
                    for tp_buf in TPS:
                        for hold in HOLDS:
                            for k in KS:
                                for f in FILTERS:
                                    r = simulate(levels_cache[k], zone, sl_buf, tp_buf, hold, k, f)
                                    if r["exp"] is None:
                                        continue
                                    results.append({"zone": zone, "sl_buf": sl_buf, "tp_buf": tp_buf,
                                                    "hold": hold, "k": k, "filter_": f, **r})
                                    if best is None or r["exp"] > best["exp"]:
                                        best = {"zone": zone, "sl_buf": sl_buf, "tp_buf": tp_buf,
                                                "hold": hold, "k": k, "filter_": f, **r}
            default_r = simulate(levels_cache[2], **DEFAULT)
            results.sort(key=lambda x: x["exp"], reverse=True)
            last_date = levels_cache[2][-1]["date"]
            # DEV+IS: РІСЃС‘ РєСЂРѕРјРµ РїРѕСЃР»РµРґРЅРёС… 180 РґРЅРµР№; OOS: 180-365 РґРЅРµР№ РЅР°Р·Р°Рґ; HOLD_OUT: РїРѕСЃР»РµРґРЅРёРµ 180 РґРЅРµР№
            holdout_start = last_date - pd.Timedelta(days=180)
            oos_start = last_date - pd.Timedelta(days=365)
            train_lv = [lv for lv in levels_cache[2] if lv["date"] < oos_start]
            oos_lv = [lv for lv in levels_cache[2] if oos_start <= lv["date"] < holdout_start]
            holdout_lv = [lv for lv in levels_cache[2] if lv["date"] >= holdout_start]
            default_oos = simulate(oos_lv, **DEFAULT)
            best_oos = (simulate(oos_lv, best["zone"], best["sl_buf"], best["tp_buf"],
                                 best["hold"], best["k"], best["filter_"]) if best else None)
            best_holdout = (simulate(holdout_lv, best["zone"], best["sl_buf"], best["tp_buf"],
                                     best["hold"], best["k"], best["filter_"]) if best else None)
            holdout_ok = bool(best_holdout and best_holdout["exp"] is not None
                              and best_holdout["exp"] >= 0.3 and best_holdout["trades"] >= 5)
            out["symbols"][name] = {
                "default": {"zone": 1.01, "sl_buf": 0.995, "tp_buf": 1.005, "hold": 10,
                            "k": 2, "filter_": "h4", **default_r},
                "best": best,
                "best_oos_6m": best_oos,
                "default_oos_6m": default_oos,
                "best_holdout_6m": best_holdout,
                "holdout_ok": holdout_ok,
                "top5": results[:5],
            }
            d = default_r
            b = best
            print(f"{name:8s} default exp={d['exp'] if d['exp'] is not None else 0:+.3f}R "
                  f"(n={d['trades']}) -> best exp={b['exp']:+.3f}R n={b['trades']} "
                  f"zone={b['zone']} sl={b['sl_buf']} tp={b['tp_buf']} hold={b['hold']} "
                  f"k={b['k']} filter={b['filter_']} winrate={b['winrate']}% dd={b['maxdd']}R")
            def _f(v):
                return f"{v:+.3f}R" if v is not None else "n/a"
            if best_oos and best_oos["exp"] is not None:
                print(f"           OOS last year: default {_f(default_oos['exp'])} (n={default_oos['trades']}) "
                      f"vs best {_f(best_oos['exp'])} (n={best_oos['trades']})")
            elif default_oos["exp"] is not None:
                print(f"           OOS last year: default {_f(default_oos['exp'])} (n={default_oos['trades']}) "
                      f"vs best вЂ” РЅРµРґРѕСЃС‚Р°С‚РѕС‡РЅРѕ СЃРґРµР»РѕРє")
            else:
                print("           OOS last year: default vs best вЂ” РЅРµРґРѕСЃС‚Р°С‚РѕС‡РЅРѕ СЃРґРµР»РѕРє")
        except Exception as exc:
            out["symbols"][name] = {"error": str(exc)}
            print(f"{name:8s} ERROR: {exc}")
    (HERE / "backtest_grid.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Saved:", HERE / "backtest_grid.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())