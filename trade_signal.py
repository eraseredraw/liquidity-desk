#!/usr/bin/env python3
"""trade_signal.py — institutional liquidity strategy scanner.

Fetches H1/D1 candles from Yahoo Finance (no API key), computes bank levels,
liquidity pools, H1 EMA200 trend and H4 confluence. Pulls macro news from RSS
feeds for context. Emits JSON + markdown report.

Usage:
  python trade_signal.py [--symbol XAUUSD|GBPUSD|EURUSD|ALL] [--out DIR]

Strategy source: mempalace.yaml (Institutional Level + Liquidity Pool Strategy).
"""

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests

YAHOO_SYMBOLS = {
    "XAUUSD": "GC=F",
    "GBPUSD": "GBPUSD=X",
    "EURUSD": "EURUSD=X",
    "GBPJPY": "GBPJPY=X",
}

RSS_FEEDS = [
    ("ForexLive", "https://www.forexlive.com/feed/news"),
    ("FXStreet", "https://feeds.fxstreet.com/news/forex-news"),
    ("Investing", "https://www.investing.com/rss/news.rss"),
]

NEWS_KEYWORDS = [
    "nfp", "cpi", "ppi", "fomc", "fed", "rate", "gold", "silver", "dollar",
    "inflation", "jobs", "gdp", "ecb", "boe", "gdp", "yield", "treasury",
    "dovish", "hawkish", "tariff", "powell", "nonfarm", "payrolls",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

DISCLAIMER = (
    "DISCLAIMER: analysis framework increases probability, not guarantee. "
    "Backtest on demo first. Never risk more than 1-2% per trade. "
    "Market context changes; adapt or stop trading."
)


def fetch_candles(yahoo_symbol: str, interval: str, range_: str) -> pd.DataFrame:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
        f"?interval={interval}&range={range_}&includePrePost=false"
    )
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    result = data["chart"]["result"][0]
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


def analyze_symbol(name: str, yahoo_symbol: str) -> dict:
    d1 = fetch_candles(yahoo_symbol, "1d", "6mo")
    h1 = fetch_candles(yahoo_symbol, "1h", "2mo")
    h4 = h1.resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()

    last = float(d1["close"].iloc[-1])
    d1_high = float(d1["high"].max())
    d1_low = float(d1["low"].min())
    equilibrium = (d1_high + d1_low) / 2

    swings = find_swings(d1, k=2, max_points=6)
    sh = [p for _, p in swings["swing_highs"]]
    sl = [p for _, p in swings["swing_lows"]]

    liquidity_sell = max(sh) if sh else None  # above recent swing high — sell stops
    liquidity_buy = min(sl) if sl else None  # below recent swing low — buy stops

    h1_ema200 = float(ema(h1["close"], 200).iloc[-1])
    h4_ema50 = float(ema(h4["close"], 50).iloc[-1])

    h4_swings = find_swings(h4, k=2, max_points=4)
    h4_resistance = max([p for _, p in h4_swings["swing_highs"]], default=None)
    h4_support = min([p for _, p in h4_swings["swing_lows"]], default=None)

    nearest_above = min([lvl for lvl in sh + [equilibrium] if lvl > last], default=None)
    nearest_below = max([lvl for lvl in sl + [equilibrium] if lvl < last], default=None)

    trend = "bullish" if last > h1_ema200 else "bearish"
    h4_trend = "bullish" if last > h4_ema50 else "bearish"

    signal = "neutral"
    if nearest_below and nearest_above:
        rr = (nearest_above - last) / max(last - nearest_below, 1e-9)
        if last < h1_ema200 * 0.999 and nearest_below and liquidity_buy and last - liquidity_buy < (last - nearest_below):
            signal = "buy" if h4_trend == "bullish" else "wait"
        elif last > h1_ema200 * 1.001 and nearest_above and liquidity_sell and liquidity_sell - last < (nearest_above - last):
            signal = "sell" if h4_trend == "bearish" else "wait"

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
        "signal": signal,
        "confluence": (h4_trend == trend) and nearest_above is not None and nearest_below is not None,
    }


ASSET_KEYWORDS = {
    "XAUUSD": ["gold", "silver", "bullion", "metals", "xau"],
    "GBPUSD": ["pound", "sterling", "gbp", "uk", "boe", "britain"],
    "EURUSD": ["euro", "eur", "ecb", "germany", "france"],
    "GBPJPY": ["pound", "sterling", "gbp", "yen", "jpy", "japan", "boj", "uk"],
    "DAX": ["dax", "xetra", "german", "germany", "dax40", "frankfurt", "euro stoxx", "ifo", "zew"],
}
MACRO_KEYWORDS = ["fed", "fomc", "nfp", "cpi", "ppi", "rate", "dollar", "inflation", "yield", "tariff", "powell"]


def fetch_news(fresh_hours: int = 24) -> list:
    from email.utils import parsedate_to_datetime

    items = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=fresh_hours)
    for feed_name, url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                pub = item.findtext("pubDate") or ""
                if not title:
                    continue
                pub_dt = None
                try:
                    pub_dt = parsedate_to_datetime(pub)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=dt.timezone.utc)
                except (TypeError, ValueError):
                    pass
                if pub_dt is not None and pub_dt < cutoff:
                    continue
                items.append({"feed": feed_name, "title": title, "pub": pub})
        except (ET.ParseError, requests.RequestException):
            continue
    seen, unique = set(), []
    for it in items:
        key = it["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    unique.sort(key=lambda x: x["pub"], reverse=True)
    relevant = [it for it in unique if any(kw in it["title"].lower() for kw in NEWS_KEYWORDS)]
    return relevant[:20]


def news_for_symbol(news: list, symbol: str, limit: int = 4) -> list:
    asset_kws = ASSET_KEYWORDS[symbol]
    matched = [n for n in news if any(kw in n["title"].lower() for kw in asset_kws)]
    if matched:
        return matched[:limit]
    macro = [n for n in news if any(kw in n["title"].lower() for kw in MACRO_KEYWORDS)]
    return macro[:limit]


def news_sentiment(news: list, symbol: str) -> str:
    asset_kws = ASSET_KEYWORDS[symbol]
    items = [n for n in news if any(kw in n["title"].lower() for kw in asset_kws)]
    if not items:
        items = [n for n in news if any(kw in n["title"].lower() for kw in MACRO_KEYWORDS)]
    if not items:
        return "новостей нет"
    hawkish = ["hawkish", "tighten", "rate hike", "higher yields", "inflation rises", "strong dollar"]
    dovish = ["dovish", "cut", "rate cut", "lower yields", "weak dollar", "recession", "slowdown", "stimulus"]
    pos, neg = 0, 0
    for it in items:
        t = it["title"].lower()
        if any(k in t for k in dovish):
            pos += 1
        if any(k in t for k in hawkish):
            neg += 1
    if pos > neg:
        return f"про-риск ({pos} позитивных / {neg} негативных заголовков)"
    if neg > pos:
        return f"анти-риск ({neg} негативных / {pos} позитивных заголовков)"
    return f"нейтрально ({pos}/{neg})"


def render_report(analysis: list, news: list, timestamp: str) -> str:
    lines = [f"# TRADE REPORT {timestamp}", ""]
    for a in analysis:
        plan_tp = a["nearest_resistance"] if a["nearest_resistance"] else "open (above structure — no resistance)"
        lines += [
            f"## {a['symbol']} — {a['last_price']}",
            f"- Signal: **{a['signal'].upper()}** (H1 {a['trend_h1']}, H4 {a['trend_h4']}, confluence={a['confluence']})",
            f"- D1 range: {a['d1_range']['high']} / {a['d1_range']['low']}, equilibrium {a['equilibrium']}",
            f"- Bank levels: swing highs {a['swing_highs']}, swing lows {a['swing_lows']}",
            f"- Liquidity: sell-stops above {a['liquidity_pool_above']}, buy-stops below {a['liquidity_pool_below']}",
            f"- H1 EMA200 {a['h1_ema200']} | H4 EMA50 {a['h4_ema50']} (support {a['h4_support']}, resistance {a['h4_resistance']})",
            f"- Plan: SL beyond {a['nearest_support']}, TP {plan_tp} (min RR 1:2)",
        ]
        symbol_news = news_for_symbol(news, a["symbol"])
        if symbol_news:
            lines.append("- News:")
            for n in symbol_news:
                lines.append(f"  - [{n['feed']}] {n['title']}")
        lines.append("")
    lines += [DISCLAIMER, ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Institutional liquidity strategy scanner")
    parser.add_argument("--symbol", default="ALL", choices=["XAUUSD", "GBPUSD", "EURUSD", "ALL"])
    parser.add_argument("--out", default=str(Path(__file__).parent))
    args = parser.parse_args()

    symbols = list(YAHOO_SYMBOLS.items()) if args.symbol == "ALL" else [(args.symbol, YAHOO_SYMBOLS[args.symbol])]

    analysis, errors = [], []
    for name, yahoo in symbols:
        try:
            analysis.append(analyze_symbol(name, yahoo))
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    news = fetch_news()
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d_%H%M")
    report = render_report(analysis, news, timestamp)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"report_{timestamp}.md"
    json_path = out_dir / f"report_{timestamp}.json"
    md_path.write_text(report, encoding="utf-8")
    json_path.write_text(
        json.dumps({"generated": timestamp, "analysis": analysis, "news": news[:10], "errors": errors},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(report)
    if errors:
        print("ERRORS:", "; ".join(errors))
    print(f"Report: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())