"""strategy.py вЂ” shared market analysis logic (levels, liquidity, signals).

Fetches candles from Yahoo Finance, computes institutional levels and
liquidity pools, emits the same analysis dict used by trade_signal.py.
"""

import pandas as pd
import requests
import time

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

YAHOO_SYMBOLS = {
    "XAUUSD": "GC=F",
    "GBPUSD": "GBPUSD=X",
    "EURUSD": "EURUSD=X",
    "GBPJPY": "GBPJPY=X",
    "DAX": "^GDAXI",
    "BTCUSD": "BTC-USD",
}

SYMBOL_PARAMS = {
    "GBPJPY": {"zone": 1.005, "sl_buf": 0.995, "tp_buf": 1.008, "hold": 20, "k": 1, "filter": "w1"},
    "BTCUSD": {"zone": 1.01, "sl_buf": 0.995, "tp_buf": 1.005, "hold": 5, "k": 2, "filter": "d1", "crypto": True},
    "XAUUSD": None,
    "GBPUSD": None,
    "EURUSD": None,
    "DAX":    None,
}

MTF_GROUPS = [
    ("intraday", [("5m", "1mo"), ("1h", "2mo")]),
    ("m15", [("15m", "2mo")]),
    ("h1", [("1h", "2mo")]),
    ("h4", [("4h", "2mo")]),
    ("d1", [("1d", "6mo")]),
    ("global", [("1wk", "2y")]),
]

KILL_ZONES_UTC = {
    "london_setup": (2, 5),    # 02:00-05:00 — сбор ликвидности
    "london":       (7, 10),   # 07:00-10:00 — основные движения
    "ny_overlap":   (12, 15),  # 12:00-15:00 — Нью-Йорк оверлап
    "ny":           (15, 17),  # 15:00-17:00 — NY продолжение
}


def in_kill_zone(now_utc=None) -> tuple:
    """(True/False, название зоны или None). Вне KZ — шум, ~70% движений фейк."""
    from datetime import datetime, timezone
    h = (now_utc or datetime.now(timezone.utc)).hour
    for name, (start, end) in KILL_ZONES_UTC.items():
        if start <= h < end:
            return True, name
    return False, None


def m5_break_of_structure(yahoo_symbol: str, direction: str, zone_price: float = None,
                          lookback: int = 40) -> dict:
    """M5 Break of Structure в зоне входа.

    direction: "buy" | "sell"
    zone_price: уровень зоны ликвидности (проверка, что BOS в зоне).

    Для BUY (цена у поддержки):
      1. M5-свинги (k=2) за последние lookback свечей
      2. lower low (стоп-хант ниже последнего swing low)
      3. последняя закрытая свеча пробивает вверх swing high между свингами = BOS
      4. entry = close, SL = под swing low * 0.9995 (≈12-15 пунктов GBPJPY)

    Для SELL — зеркально. BOS считается только СВЕЖИМ (последняя закрытая
    свеча выше/ниже уровня) — устаревший пробой не подтверждает вход.

    Возвращает {"confirmed", "entry_price", "sl_price", "swing_level",
    "r_ratio", "detail"}. На ошибках — {"confirmed": False}.
    """
    try:
        df = fetch_candles(yahoo_symbol, "5m", "1d")
        if len(df) < lookback:
            return {"confirmed": False, "detail": f"мало M5 свечей: {len(df)}"}
        df = df.tail(lookback)
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        n = len(closes)

        if direction == "buy":
            swings = find_swings(df, k=2, max_points=8)["swing_lows"]
            if len(swings) < 2:
                return {"confirmed": False, "detail": "нет 2 M5 swing lows"}
            last_sl, prev_sl = swings[-1][1], swings[-2][1]
            lower_low = last_sl < prev_sl
            i_prev = df.index.get_loc(swings[-2][0])
            i_last = df.index.get_loc(swings[-1][0])
            swing_high_between = float(max(highs[min(i_prev, i_last):max(i_prev, i_last) + 1]))
            bos = closes[-1] > swing_high_between
            if bos:
                entry = float(closes[-1])
                sl = last_sl * 0.9995
                if entry <= sl:
                    return {"confirmed": False, "detail": "entry ниже SL"}
                if entry > swing_high_between * 1.0015:
                    return {"confirmed": False, "detail": f"вход далеко от пробоя ({swing_high_between}) — догон"}
                if zone_price is not None and entry > zone_price * 1.003:
                    return {"confirmed": False, "detail": f"BOS далеко от зоны ({zone_price})"}
                r_ratio = round((swing_high_between - sl) / (entry - sl), 2)
                return {
                    "confirmed": True,
                    "entry_price": round(entry, 5),
                    "sl_price": round(sl, 5),
                    "swing_level": round(swing_high_between, 5),
                    "r_ratio": r_ratio,
                    "detail": "lower low → пробой вверх" if lower_low else "пробой вверх без lower low",
                }
            return {"confirmed": False, "detail": "нет пробоя вверх"}

        elif direction == "sell":
            swings = find_swings(df, k=2, max_points=8)["swing_highs"]
            if len(swings) < 2:
                return {"confirmed": False, "detail": "нет 2 M5 swing highs"}
            last_sh, prev_sh = swings[-1][1], swings[-2][1]
            higher_high = last_sh > prev_sh
            i_prev = df.index.get_loc(swings[-2][0])
            i_last = df.index.get_loc(swings[-1][0])
            swing_low_between = float(min(lows[min(i_prev, i_last):max(i_prev, i_last) + 1]))
            bos = closes[-1] < swing_low_between
            if bos:
                entry = float(closes[-1])
                sl = last_sh * 1.0005
                if entry >= sl:
                    return {"confirmed": False, "detail": "entry выше SL"}
                if entry < swing_low_between * 0.9985:
                    return {"confirmed": False, "detail": f"вход далеко от пробоя ({swing_low_between}) — догон"}
                if zone_price is not None and entry < zone_price * 0.997:
                    return {"confirmed": False, "detail": f"BOS далеко от зоны ({zone_price})"}
                r_ratio = round((sl - swing_low_between) / (sl - entry), 2)
                return {
                    "confirmed": True,
                    "entry_price": round(entry, 5),
                    "sl_price": round(sl, 5),
                    "swing_level": round(swing_low_between, 5),
                    "r_ratio": r_ratio,
                    "detail": "higher high → пробой вниз" if higher_high else "пробой вниз без higher high",
                }
            return {"confirmed": False, "detail": "нет пробоя вниз"}

        return {"confirmed": False, "detail": "направление не buy/sell"}
    except Exception as exc:
        return {"confirmed": False, "detail": str(exc)}


def asian_range(yahoo_symbol: str) -> dict:
    """Азиатский диапазон (00:00-07:00 UTC). Пробой края → байас на разворот.

    Азия ещё идёт (< 07:00 UTC) → берём вчерашнюю сессию.
    Лондон/NY ~70% времени пробивают край и разворачиваются:
    пробой High → sell-байас, пробой Low → buy-байас.
    """
    try:
        df = fetch_candles(yahoo_symbol, "1h", "5d")
        if len(df) < 10:
            return {"bias": "neutral"}
        df = df[~df.index.duplicated(keep="last")].sort_index()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if now.hour >= 7:
            asian_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            asian_start = (now - pd.Timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        asian_end = asian_start + pd.Timedelta(hours=7)
        asian = df[(df.index >= asian_start) & (df.index < asian_end)]
        if len(asian) < 3:
            return {"bias": "neutral"}

        asian_high = float(asian["high"].max())
        asian_low = float(asian["low"].min())
        last_price = float(df["close"].iloc[-1])
        range_pct = (asian_high - asian_low) / asian_low * 100

        bias, broken_level = "neutral", None
        if last_price > asian_high * 1.0003:
            bias, broken_level = "sell", asian_high
        elif last_price < asian_low * 0.9997:
            bias, broken_level = "buy", asian_low

        return {
            "high": asian_high,
            "low": asian_low,
            "range_pct": round(range_pct, 3),
            "bias": bias,
            "broken_level": broken_level,
            "current_price": last_price,
        }
    except Exception:
        return {"bias": "neutral"}


def fetch_candles(yahoo_symbol: str, interval: str, range_: str) -> pd.DataFrame:
    """Свечи через цепочку провайдеров (TwelveData -> Finnhub -> Yahoo)."""
    from providers import fetch as providers_fetch
    return providers_fetch(yahoo_symbol, interval, range_)


def fetch_candles_yahoo(yahoo_symbol: str, interval: str, range_: str) -> pd.DataFrame:
    last_exc = None
    for attempt in range(2):
        for host in ("query1", "query2"):
            url = (
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
                f"?interval={interval}&range={range_}&includePrePost=false"
            )
            try:
                resp = requests.get(url, headers=HEADERS, timeout=12)
                resp.raise_for_status()
                data = resp.json()
                result = (data.get("chart") or {}).get("result") or []
                if not result:
                    raise RuntimeError(f"Yahoo: empty result for {interval}/{range_}")
                result = result[0]
                timestamps = result["timestamp"]
                quote = result["indicators"]["quote"][0]
                df = pd.DataFrame(
                    {
                        "ts": pd.to_datetime(timestamps, unit="s", utc=True),
                        "open": quote["open"],
                        "high": quote["high"],
                        "low": quote["low"],
                        "close": quote["close"],
                        "volume": quote["volume"],
                    }
                )
                df = df.dropna().set_index("ts")
                df = df[~df.index.duplicated(keep="last")]
                return df
            except Exception as exc:
                last_exc = exc
                continue
        time.sleep(1 + attempt)
    raise RuntimeError(f"Yahoo {yahoo_symbol} {interval}/{range_} failed: {last_exc}")


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def find_swings(df: pd.DataFrame, k: int = 2, max_points: int = 6) -> dict:
    highs, lows = df["high"], df["low"]
    swing_highs, swing_lows = [], []
    n = len(df)
    for i in range(k, n - k):
        if highs.iloc[i] == max(highs.iloc[i - k : i + k + 1]):
            swing_highs.append((df.index[i], float(highs.iloc[i])))
        if lows.iloc[i] == min(lows.iloc[i - k : i + k + 1]):
            swing_lows.append((df.index[i], float(lows.iloc[i])))
    swing_highs.sort(key=lambda x: x[0])
    swing_lows.sort(key=lambda x: x[0])
    return {
        "swing_highs": swing_highs[-max_points:],
        "swing_lows": swing_lows[-max_points:],
    }


def atr(series: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        series["high"] - series["low"],
        (series["high"] - series["close"].shift(1)).abs(),
        (series["low"] - series["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI Wilder: gain/loss EWM alpha=1/period."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - 100 / (1 + rs)


def fib_levels(high: float, low: float) -> dict:
    diff = high - low
    if diff <= 0:
        return {}
    return {
        "0.382": round(high - diff * 0.382, 3),
        "0.5": round(high - diff * 0.5, 3),
        "0.618": round(high - diff * 0.618, 3),
    }


def merge_levels(prices: list, tol: float) -> list:
    merged = []
    for p in sorted(prices):
        if merged and abs(merged[-1] - p) <= tol:
            merged[-1] = (merged[-1] + p) / 2
        else:
            merged.append(p)
    return merged


def mtf_levels(yahoo_symbol: str, last_price: float, atr_ref: float, cache: dict = None) -> dict:
    """Grouped swing levels by timeframe family. cache: {interval: df|None}."""
    cache = cache if cache is not None else {}
    tol = max(0.3 * atr_ref, last_price * 0.0005)
    out = {}
    for group, specs in MTF_GROUPS:
        pts = []
        for interval, rng in specs:
            if interval not in cache:
                try:
                    cache[interval] = fetch_candles(yahoo_symbol, interval, rng)
                except Exception:
                    cache[interval] = None
            df = cache[interval]
            if df is None or len(df) < 30:
                continue
            swings = find_swings(df, k=2, max_points=10)
            pts += [p for _, p in swings["swing_highs"] + swings["swing_lows"]]
        if not pts:
            out[group] = []
            continue
        merged = merge_levels(pts, tol)
        near = [lvl for lvl in merged if abs(lvl - last_price) <= last_price * 0.02]
        near.sort(key=lambda p: abs(p - last_price))
        out[group] = [round(p, 3) for p in near[:6]]
    return out


def analyze_symbol(name: str, yahoo_symbol: str) -> dict:
    params = SYMBOL_PARAMS.get(name)
    d1 = fetch_candles(yahoo_symbol, "1d", "6mo")
    h1 = fetch_candles(yahoo_symbol, "1h", "2mo")
    h4 = h1.resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    k = params["k"] if params else 2

    last = float(d1["close"].iloc[-1])
    d1_high = float(d1["high"].max())
    d1_low = float(d1["low"].min())
    equilibrium = (d1_high + d1_low) / 2

    swings = find_swings(d1, k=k, max_points=6)
    sh = [p for _, p in swings["swing_highs"]]
    sl = [p for _, p in swings["swing_lows"]]

    liquidity_sell = min([lvl for lvl in sh if lvl > last], default=None)
    liquidity_buy = max([lvl for lvl in sl if lvl < last], default=None)

    h1_ema200 = float(ema(h1["close"], 200).iloc[-1])
    h4_ema50 = float(ema(h4["close"], 50).iloc[-1])

    h4_swings = find_swings(h4, k=2, max_points=4)
    h4_resistance = max([p for _, p in h4_swings["swing_highs"]], default=None)
    h4_support = min([p for _, p in h4_swings["swing_lows"]], default=None)

    if liquidity_sell is None and h4_resistance and h4_resistance > last:
        liquidity_sell = h4_resistance
    if liquidity_buy is None and h4_support and h4_support < last:
        liquidity_buy = h4_support

    nearest_above = min([lvl for lvl in sh + [equilibrium] if lvl > last], default=None)
    nearest_below = max([lvl for lvl in sl + [equilibrium] if lvl < last], default=None)

    trend = "bullish" if last > h1_ema200 else "bearish"
    h4_trend = "bullish" if last > h4_ema50 else "bearish"
    d1_ema50 = float(ema(d1["close"], 50).iloc[-1])
    d1_trend = "bullish" if last > d1_ema50 else "bearish"
    w1 = d1.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    w_ema50 = float(ema(w1["close"], 50).iloc[-1])
    w_last = float(w1["close"].iloc[-1])
    w1_trend = "bullish" if w_last > w_ema50 else "bearish"
    h1_atr = float(atr(h1).iloc[-1])
    h1_rsi = float(rsi(h1["close"]).iloc[-1])
    cache = {"1d": d1, "1h": h1, "4h": h4}
    mtf = mtf_levels(yahoo_symbol, last, h1_atr * 4, cache)

    swing_window = 60
    fib_high = float(d1["high"].tail(swing_window).max())
    fib_low = float(d1["low"].tail(swing_window).min())
    fib = fib_levels(fib_high, fib_low)

    from trendlines import detect as tl_detect
    tl = {}
    try:
        tl = tl_detect(h4)
    except Exception:
        tl = {"support": None, "resistance": None, "break": None, "break_line": None}

    from strategy_signal import signal_from_features
    sig = signal_from_features(last, h1_ema200, h4_trend, d1_trend, liquidity_buy,
                               liquidity_sell, nearest_below, nearest_above, params,
                               w1_trend=w1_trend)
    signal = sig["signal"]

    return {
        "symbol": name,
        "last_price": round(last, 3),
        "d1_range": {"high": round(d1_high, 3), "low": round(d1_low, 3)},
        "equilibrium": round(equilibrium, 3),
        "swing_highs": [round(p, 3) for p in sh],
        "swing_lows": [round(p, 3) for p in sl],
        "liquidity_pool_above": round(liquidity_sell, 3) if liquidity_sell else None,
        "liquidity_pool_below": round(liquidity_buy, 3) if liquidity_buy else None,
        "h1_ema200": round(h1_ema200, 3),
        "h4_ema50": round(h4_ema50, 3),
        "h4_support": round(h4_support, 3) if h4_support else None,
        "h4_resistance": round(h4_resistance, 3) if h4_resistance else None,
        "nearest_resistance": round(nearest_above, 3) if nearest_above else None,
        "nearest_support": round(nearest_below, 3) if nearest_below else None,
        "trend_h1": trend,
        "trend_h4": h4_trend,
        "trend_d1": d1_trend,
        "trend_w1": w1_trend,
        "atr_h1": round(h1_atr, 3),
        "rsi_h1": round(h1_rsi, 1),
        "fib": fib,
        "fib_swing": {"high": round(fib_high, 3), "low": round(fib_low, 3)},
        "tline_support": tl.get("support"),
        "tline_resistance": tl.get("resistance"),
        "tline_break": tl.get("break"),
        "tline_break_value": tl.get("break_line"),
        "mtf_levels": mtf,
        "params": params,
        "signal": signal,
        "confluence": (h4_trend == trend) and nearest_above is not None and nearest_below is not None,
    }


def correlation_matrix() -> dict:
    """Pearson corr of daily returns between instruments (>=0.5 kept)."""
    closes = {}
    for name, yahoo in YAHOO_SYMBOLS.items():
        try:
            closes[name] = fetch_candles(yahoo, "1d", "6mo")["close"].pct_change().dropna()
        except Exception:
            continue
    names = list(closes)
    pairs = {}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            frame = pd.concat([closes[a], closes[b]], axis=1, join="inner").dropna()
            if len(frame) < 60:
                continue
            c = float(frame.iloc[:, 0].corr(frame.iloc[:, 1]))
            if abs(c) >= 0.5:
                pairs[f"{a}/{b}"] = round(c, 2)
    return pairs


def analyze_all(symbol: str = "ALL") -> tuple:
    symbols = list(YAHOO_SYMBOLS.items()) if symbol == "ALL" else [(symbol, YAHOO_SYMBOLS[symbol])]
    analysis, errors = [], []
    for name, yahoo in symbols:
        try:
            analysis.append(analyze_symbol(name, yahoo))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return analysis, errors
