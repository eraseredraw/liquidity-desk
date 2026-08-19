"""webui.py — локальная веб-морда: свечки + зоны + сигналы.
Запуск: python webui.py  ->  http://localhost:8000
Роуты: / (интерфейс), /api/status, /api/chart, /api/analysis
"""
import json
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))
from strategy import SYMBOL_PARAMS, YAHOO_SYMBOLS, analyze_symbol
import providers

HERE = Path(__file__).parent
app = FastAPI(title="Trading Dashboard")

CACHE_TTL = 120  # сек
_analysis_cache = {}
_chart_cache = {}


def _cache_get(cache: dict, key: str):
    hit = cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def _cache_put(cache: dict, key: str, value):
    cache[key] = (time.time(), value)


@app.get("/", response_class=HTMLResponse)
def index():
    from fastapi.responses import Response
    html = (HERE / "web" / "index.html").read_text(encoding="utf-8")
    return Response(content=html, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    return {
        sym: {"enabled": SYMBOL_PARAMS.get(sym) is not None,
              "params": SYMBOL_PARAMS.get(sym)}
        for sym in YAHOO_SYMBOLS
    }


@app.get("/api/chart")
def chart(symbol: str, interval: str = "1h", range_: str = "2mo"):
    yahoo = YAHOO_SYMBOLS.get(symbol)
    if not yahoo:
        raise HTTPException(404, f"no such symbol: {symbol}")
    key = (symbol, interval, range_)
    cached = _cache_get(_chart_cache, key)
    if cached:
        return cached
    try:
        df = providers.fetch(yahoo, interval, range_)
    except Exception as exc:
        raise HTTPException(502, f"data fetch failed ({interval}): {exc}")
    if df is None or len(df) < 2:
        raise HTTPException(502, f"no data for {symbol} {interval}")
    out = [{
        "time": int(ts.timestamp()),
        "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
    } for ts, r in df.iterrows()]
    _cache_put(_chart_cache, key, out)
    return {"symbol": symbol, "interval": interval, "candles": out}


@app.get("/api/analysis")
def analysis(symbol: str):
    yahoo = YAHOO_SYMBOLS.get(symbol)
    if not yahoo:
        raise HTTPException(404, f"no such symbol: {symbol}")
    cached = _cache_get(_analysis_cache, symbol)
    if cached:
        return cached
    try:
        a = analyze_symbol(symbol, yahoo)
    except Exception as exc:
        raise HTTPException(502, f"analysis failed: {exc}")
    out = {
        "symbol": symbol,
        "enabled": a.get("params") is not None,
        "last_price": a.get("last_price"),
        "signal": a.get("signal"),
        "trends": {"h1": a.get("trend_h1"), "h4": a.get("trend_h4"),
                   "d1": a.get("trend_d1"), "w1": a.get("trend_w1")},
        "confluence": a.get("confluence"),
        "rsi_h1": a.get("rsi_h1"),
        "atr_h1": a.get("atr_h1"),
        "equilibrium": a.get("equilibrium"),
        "liquidity_pool_above": a.get("liquidity_pool_above"),
        "liquidity_pool_below": a.get("liquidity_pool_below"),
        "h4_resistance": a.get("h4_resistance"),
        "h4_support": a.get("h4_support"),
        "nearest_resistance": a.get("nearest_resistance"),
        "nearest_support": a.get("nearest_support"),
        "h1_ema200": a.get("h1_ema200"),
        "h4_ema50": a.get("h4_ema50"),
        "params": a.get("params"),
    }
    _cache_put(_analysis_cache, symbol, out)
    return out


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")