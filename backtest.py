"""backtest.py вЂ” daily-bar backtest of the liquidity strategy.

Uses the SAME signal logic as live (signal.py / signal_from_features).
Entry on next day open, exit at SL below buy-liquidity pool or TP at
nearest resistance. No lookahead вЂ” every day sees only bars up to itself.
H4 trend comes from real 4h candles (not a daily proxy). Trades where
SL/TP are invalid against the actual entry price are skipped (fixes the
old "win with negative R" garbage). Results saved to backtest_stats.json.
"""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from strategy import SYMBOL_PARAMS, YAHOO_SYMBOLS, ema, fetch_candles, find_swings
from strategy_signal import signal_from_features

HERE = Path(__file__).parent


def build_h4_ema50(yahoo: str, ref_index: pd.Index) -> pd.Series:
    """H4 EMA50, РІС‹СЂРѕРІРЅРµРЅРЅС‹Р№ ffill РЅР° РґРЅРµРІРЅРѕР№ РёРЅРґРµРєСЃ Р±СЌРєС‚РµСЃС‚Р°."""
    h4 = fetch_candles(yahoo, "4h", "2y")
    h4d = h4.resample("1D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    return ema(h4d["close"], 50).reindex(ref_index, method="ffill")


def build_w_ema50(yahoo: str, ref_index: pd.Index) -> pd.Series:
    """РќРµРґРµР»СЊРЅС‹Р№ EMA50 (СЃРІРµС‡Рё W-FRI), ffill РЅР° РґРЅРµРІРЅРѕР№ РёРЅРґРµРєСЃ вЂ” Р±РµР· Р·Р°РіР»СЏРґС‹РІР°РЅРёСЏ:
    РґР»СЏ РґРЅСЏ D Р±РµСЂС‘С‚СЃСЏ EMA РїРѕСЃР»РµРґРЅРµР№ Р·Р°РІРµСЂС€С‘РЅРЅРѕР№ РЅРµРґРµР»Рё."""
    d1 = fetch_candles(yahoo, "1d", "2y")
    w = d1.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    return ema(w["close"], 50).reindex(ref_index, method="ffill")


def build_h1_ema200(yahoo: str, ref_index: pd.Index) -> pd.Series:
    """H1 EMA200 вЂ” С‚РѕС‚ Р¶Рµ С‚СЂРµРЅРґРѕРІС‹Р№ С„РёР»СЊС‚СЂ, С‡С‚Рѕ РІ Р»Р°Р№РІРµ, РІС‹СЂРѕРІРЅРµРЅ РЅР° РґРЅРµРІРЅРѕР№ РёРЅРґРµРєСЃ."""
    h1 = fetch_candles(yahoo, "1h", "2y")
    h1d = h1.resample("1D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    return ema(h1d["close"], 200).reindex(ref_index, method="ffill")


def signal_for_bar(df: pd.DataFrame, h4_ema50: pd.Series, h1_ema200: pd.Series,
                   w_ema50: pd.Series, i: int, params: dict) -> dict:
    window = df.iloc[: i + 1]
    last = float(window["close"].iloc[-1])
    swings = find_swings(window, k=params["k"], max_points=6)
    sh = [p for _, p in swings["swing_highs"]]
    sl = [p for _, p in swings["swing_lows"]]
    liquidity_sell = min([lvl for lvl in sh if lvl > last], default=None)
    liquidity_buy = max([lvl for lvl in sl if lvl < last], default=None)
    eq = (float(window["high"].max()) + float(window["low"].min())) / 2
    ema50d = float(ema(window["close"], 50).iloc[-1])
    nearest_above = min([lvl for lvl in sh + [eq] if lvl > last], default=None)
    nearest_below = max([lvl for lvl in sl + [eq] if lvl < last], default=None)
    ts = df.index[i]
    ema200_val = h1_ema200.get(ts)
    if ema200_val is None or pd.isna(ema200_val):
        ema200_val = ema50d
    h4_val = h4_ema50.get(ts)
    if h4_val is None or pd.isna(h4_val):
        h4_val = ema50d
    w_val = w_ema50.get(ts)
    w1_trend = "bullish" if last > w_val else "bearish" if w_val is not None and not pd.isna(w_val) else None
    h4_trend = "bullish" if last > h4_val else "bearish"
    d1_trend = "bullish" if last > ema50d else "bearish"
    return signal_from_features(last, ema200_val, h4_trend, d1_trend, liquidity_buy,
                                liquidity_sell, nearest_below, nearest_above, params,
                                w1_trend=w1_trend)


def backtest_symbol(name: str, yahoo: str, min_bars: int = 260) -> dict:
    params = SYMBOL_PARAMS.get(name)
    if params is None:
        return {"symbol": name, "trades": [], "disabled": True}
    df = fetch_candles(yahoo, "1d", "2y")
    if len(df) < min_bars:
        return {"symbol": name, "trades": [], "error": f"not enough bars ({len(df)})"}
    h4_ema50 = build_h4_ema50(yahoo, df.index)
    h1_ema200 = build_h1_ema200(yahoo, df.index)
    w_ema50 = build_w_ema50(yahoo, df.index)
    trades = []
    for i in range(min_bars, len(df) - 1):
        info = signal_for_bar(df, h4_ema50, h1_ema200, w_ema50, i, params)
        if info["signal"] not in ("buy", "sell"):
            continue
        side = info["signal"]
        entry = float(df["open"].iloc[i + 1])
        sl, tp = info["sl"], info["tp"]
        if not sl or not tp:
            continue
        if side == "buy" and not (sl < entry < tp):
            continue  # invalid geometry vs actual entry вЂ” old "win with negative R" garbage
        if side == "sell" and not (tp < entry < sl):
            continue
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk <= 0 or reward <= 0:
            continue
        result = "flat"
        exit_price = entry
        for j in range(i + 1, min(i + 1 + params["hold"], len(df))):
            high, low = float(df["high"].iloc[j]), float(df["low"].iloc[j])
            if side == "buy" and low <= sl:
                result, exit_price = "loss", sl
                break
            if side == "sell" and high >= sl:
                result, exit_price = "loss", sl
                break
            if side == "buy" and high >= tp:
                result, exit_price = "win", tp
                break
            if side == "sell" and low <= tp:
                result, exit_price = "win", tp
                break
        trades.append({
            "date": str(df.index[i].date()),
            "side": side,
            "entry": round(entry, 5),
            "sl": round(sl, 5),
            "tp": round(tp, 5),
            "result": result,
            "rr": round(reward / risk, 2),
            "r": round((exit_price - entry) / risk, 2) if side == "buy" else round((entry - exit_price) / risk, 2),
        })
    return {"symbol": name, "trades": trades}


def summarize(result: dict) -> dict:
    trades = result.get("trades", [])
    closed = [t for t in trades if t["result"] in ("win", "loss")]
    wins = [t for t in closed if t["result"] == "win"]
    losses = [t for t in closed if t["result"] == "loss"]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in closed:
        equity += t["r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    winrate = len(wins) / len(closed) if closed else 0.0
    expectancy = sum(t["r"] for t in closed) / len(closed) if closed else 0.0
    return {
        "symbol": result.get("symbol"),
        "trades_total": len(trades),
        "trades_closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(trades) - len(closed),
        "winrate": round(winrate * 100, 1),
        "avg_r_win": round(sum(t["r"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_r_loss": round(sum(t["r"] for t in losses) / len(losses), 2) if losses else 0.0,
        "expectancy_r": round(expectancy, 3),
        "total_r": round(sum(t["r"] for t in closed), 2),
        "max_drawdown_r": round(max_dd, 2),
        "last_trades": [
            {"date": t["date"], "side": t["side"], "result": t["result"], "r": t["r"]}
            for t in trades[-5:]
        ],
    }


def main() -> int:
    results, errors = [], []
    for name, yahoo in YAHOO_SYMBOLS.items():
        try:
            res = backtest_symbol(name, yahoo)
            if "error" in res:
                errors.append(f"{name}: {res['error']}")
                continue
            if res.get("disabled"):
                results.append({"symbol": name, "disabled": True})
                continue
            results.append(summarize(res))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    out = {"generated": pd.Timestamp.now(tz="UTC").isoformat(), "symbols": results, "errors": errors}
    (HERE / "backtest_stats.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for s in results:
        if s.get("disabled"):
            print(f"{s['symbol']:8s} disabled (СЂРµС€РµС‚РєР° РїРѕРєР°Р·Р°Р»Р° РїРµСЂРµРѕР±СѓС‡РµРЅРёРµ)")
            continue
        print(f"{s['symbol']:8s} trades={s['trades_closed']:4d} winrate={s['winrate']:5.1f}% "
              f"exp={s['expectancy_r']:+.3f}R total={s['total_r']:+.1f}R maxDD={s['max_drawdown_r']:.1f}R")
    if errors:
        print("ERRORS:", "; ".join(errors))
    print("Saved:", HERE / "backtest_stats.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
