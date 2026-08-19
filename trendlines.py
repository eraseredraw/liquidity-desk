"""trendlines.py — formalized trend lines: fractal pivots, line fitting,
body-breakout with quality filters. Verifiable, no lookahead.

line = least-squares fit through >=3 pivots with ATR-normalized tolerance.
Breakout = close beyond line, body >= 0.35 ATR, close in extreme 40% of
candle, depth <= 1.2 ATR, line respected before (no prior closes beyond).
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from strategy import atr, fetch_candles, find_swings

MIN_PIVOTS = 3
TOL_ATR = 0.2
BODY_ATR = 0.35
DEPTH_ATR = 1.2
EXTREME_FRAC = 0.4
MAX_AGE = 60


def fit_line(points: list) -> tuple:
    xs = [i for i, _ in points]
    ys = [p for _, p in points]
    n = len(points)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    xm, ym = sum(xs) / n, sum(ys) / n
    num = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    den = sum((x - xm) ** 2 for x in xs)
    slope = num / den if den else 0.0
    intercept = ym - slope * xm
    return slope, intercept


def line_value(slope: float, intercept: float, x: int) -> float:
    return slope * x + intercept


def collect_lines(df: pd.DataFrame, pivots: list, atr_val: float, direction: str) -> list:
    lines = []
    p = pivots[-MAX_AGE:]
    if len(p) < MIN_PIVOTS:
        return lines
    idx = list(df.index)
    for i in range(len(p) - MIN_PIVOTS + 1):
        for j in range(i + MIN_PIVOTS - 1, len(p)):
            cand = p[i : j + 1]
            slope, intercept = fit_line(cand)
            if direction == "support" and slope > 0.03 * atr_val:
                continue
            if direction == "resistance" and slope < -0.03 * atr_val:
                continue
            if all(abs(pv - line_value(slope, intercept, pi)) <= TOL_ATR * atr_val for pi, pv in cand):
                lines.append({"slope": slope, "intercept": intercept,
                              "start_x": p[i][0], "last_x": p[j][0], "pivots": len(cand),
                              "direction": direction})
    if not lines:
        return lines
    lines.sort(key=lambda l: (l["last_x"], l["pivots"]), reverse=True)
    return lines[:2]


def check_breakout(df: pd.DataFrame, lines: list, atr_val: float, last_x: int, last_close: float) -> dict:
    closes = df["close"].tolist()
    for line in lines:
        if last_x < line["start_x"]:
            continue
        lv = line_value(line["slope"], line["intercept"], last_x)
        if line["direction"] == "resistance" and last_close > lv:
            body = last_close - float(df["open"].iloc[-1])
            low = float(df["low"].iloc[-1])
            rng = float(df["high"].iloc[-1]) - low
            depth = last_close - lv
            if body >= BODY_ATR * atr_val and depth <= DEPTH_ATR * atr_val and rng > 0:
                pos_in_range = (last_close - low) / rng
                if pos_in_range >= 1 - EXTREME_FRAC:
                    prior = [c for c in closes[: last_x + 1] if c <= lv + TOL_ATR * atr_val]
                    if len(prior) >= 3:
                        return {"break": "buy", "line_value": lv}
        elif line["direction"] == "support" and last_close < lv:
            body = float(df["open"].iloc[-1]) - last_close
            high = float(df["high"].iloc[-1])
            low = float(df["low"].iloc[-1])
            rng = high - low
            depth = lv - last_close
            if body >= BODY_ATR * atr_val and depth <= DEPTH_ATR * atr_val and rng > 0:
                pos_in_range = (high - last_close) / rng
                if pos_in_range >= 1 - EXTREME_FRAC:
                    prior = [c for c in closes[: last_x + 1] if c >= lv - TOL_ATR * atr_val]
                    if len(prior) >= 3:
                        return {"break": "sell", "line_value": lv}
    return {"break": None, "line_value": None}


def detect(df: pd.DataFrame) -> dict:
    """Return active trend lines + last-breakout signal for the last bar."""
    atr_val = float(atr(df).iloc[-1])
    if atr_val <= 0:
        return {"support": None, "resistance": None, "break": None, "break_line": None}
    swings = find_swings(df, k=2, max_points=40)
    support_piv = [(idx_pos(df, ts), float(p)) for ts, p in swings["swing_lows"]]
    resist_piv = [(idx_pos(df, ts), float(p)) for ts, p in swings["swing_highs"]]
    supports = collect_lines(df, support_piv, atr_val, "support")
    resistances = collect_lines(df, resist_piv, atr_val, "resistance")
    last_x = len(df) - 1
    last_close = float(df["close"].iloc[-1])
    result = {"support": None, "resistance": None, "break": None, "break_line": None}
    if supports:
        s = supports[0]
        result["support"] = round(line_value(s["slope"], s["intercept"], last_x), 3)
    if resistances:
        r = resistances[0]
        result["resistance"] = round(line_value(r["slope"], r["intercept"], last_x), 3)
    for line in supports + resistances:
        bk = check_breakout(df, [line], atr_val, last_x, last_close)
        if bk["break"]:
            result["break"] = bk["break"]
            result["break_line"] = round(bk["line_value"], 3)
            break
    return result


def idx_pos(df: pd.DataFrame, ts) -> int:
    try:
        return df.index.get_loc(ts)
    except KeyError:
        return 0


def check_history(yahoo: str, interval: str = "4h", range_: str = "6mo", hold: int = 5,
                  target_atr: float = 0.5) -> dict:
    """Cheap validation: after a body-breakout, does price move >=target ATR
    in the breakout direction within `hold` bars?"""
    df = fetch_candles(yahoo, interval, range_)
    atr_s = atr(df)
    closes = df["close"].tolist()
    n = len(df)
    hits, total = 0, 0
    for i in range(60, n - hold):
        window = df.iloc[: i + 1]
        a = float(atr_s.iloc[i])
        if a <= 0:
            continue
        res = detect(window)
        if not res["break"]:
            continue
        total += 1
        direction = 1 if res["break"] == "buy" else -1
        start = closes[i]
        moved = False
        for j in range(i + 1, i + 1 + hold):
            if direction == 1 and closes[j] >= start + target_atr * a:
                moved = True
                break
            if direction == -1 and closes[j] <= start - target_atr * a:
                moved = True
                break
        if moved:
            hits += 1
    return {
        "breakouts": total,
        "hits": hits,
        "hit_rate_pct": round(hits / total * 100, 1) if total else 0.0,
        "note": f"движение >= {target_atr} ATR в сторону пробоя за {hold} баров",
    }


if __name__ == "__main__":
    import json

    out = {}
    for name, yahoo in [("XAUUSD", "GC=F"), ("GBPUSD", "GBPUSD=X"),
                        ("EURUSD", "EURUSD=X"), ("GBPJPY", "GBPJPY=X")]:
        try:
            out[name] = check_history(yahoo)
        except Exception as exc:
            out[name] = {"error": str(exc)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    Path(__file__).with_name("trendline_check.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")