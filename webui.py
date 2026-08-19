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
    html = (HERE / "web" / "dashboard.html").read_text(encoding="utf-8")
    return Response(content=html, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


@app.get("/old", response_class=HTMLResponse)
def old_index():
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


@app.get("/api/advices")
def advices():
    """Load all latest advice files and backtest stats."""
    adv_dir = HERE / "advices"
    if not adv_dir.exists():
        return {}
    
    stats_path = HERE / "backtest_stats.json"
    stats = {}
    if stats_path.exists():
        try:
            data = json.loads(stats_path.read_text(encoding="utf-8"))
            for s in data.get("symbols", []):
                stats[s["symbol"]] = s
        except Exception:
            pass
    
    result = {}
    for sym in YAHOO_SYMBOLS:
        files = sorted(adv_dir.glob(f"advice_{sym}_*.md"), reverse=True)
        if not files:
            result[sym] = {"enabled": False, "signal": "neutral"}
            continue
        
        latest = files[0]
        try:
            text = latest.read_text(encoding="utf-8")
            lines = text.split("\n")
            
            data = {"enabled": stats.get(sym, {}).get("disabled") == False}
            data["advice_text"] = text.replace("\n", "<br>")
            
            for line in lines:
                if "signal:" in line.lower():
                    data["signal"] = line.split(":")[-1].strip().lower()
                elif "entry_score:" in line.lower():
                    try:
                        data["entry_score"] = int(line.split(":")[-1].strip().split("/")[0])
                    except:
                        pass
                elif "last_price:" in line.lower():
                    try:
                        data["last_price"] = float(line.split(":")[-1].strip())
                    except:
                        pass
                elif "entry:" in line.lower() and "entry_score" not in line.lower():
                    try:
                        data["entry"] = float(line.split(":")[-1].strip())
                    except:
                        pass
                elif "sl:" in line.lower():
                    try:
                        data["sl"] = float(line.split(":")[-1].strip())
                    except:
                        pass
                elif "tp:" in line.lower():
                    try:
                        data["tp"] = float(line.split(":")[-1].strip())
                    except:
                        pass
                elif "lot:" in line.lower():
                    try:
                        data["lot"] = float(line.split(":")[-1].strip())
                    except:
                        pass
                elif "rsi_h1:" in line.lower():
                    try:
                        data["rsi_h1"] = float(line.split(":")[-1].strip())
                    except:
                        pass
                elif "atr_h1:" in line.lower():
                    try:
                        data["atr_h1"] = float(line.split(":")[-1].strip())
                    except:
                        pass
                elif "trend_d1:" in line.lower():
                    if "trends" not in data:
                        data["trends"] = {}
                    data["trends"]["d1"] = line.split(":")[-1].strip()
                elif "trend_h4:" in line.lower():
                    if "trends" not in data:
                        data["trends"] = {}
                    data["trends"]["h4"] = line.split(":")[-1].strip()
                elif "trend_h1:" in line.lower():
                    if "trends" not in data:
                        data["trends"] = {}
                    data["trends"]["h1"] = line.split(":")[-1].strip()
                elif "ВЕТО:" in line or "СТОП-ТОРГОВЛЯ:" in line:
                    data["veto"] = line.strip()
                elif line.strip().startswith("- ") or line.strip().startswith("• "):
                    if "entry_reasons" not in data:
                        data["entry_reasons"] = []
                    reason = line.strip().lstrip("- ").lstrip("• ")
                    if reason:
                        data["entry_reasons"].append(reason)
            
            # Find chart
            chart_dir = HERE / "snapshots"
            if chart_dir.exists():
                charts = sorted(chart_dir.glob(f"chart_{sym}_*.png"), reverse=True)
                if charts:
                    data["chart_path"] = str(charts[0])
            
            # Add backtest
            if sym in stats:
                data["backtest"] = stats[sym]
            
            result[sym] = data
        except Exception as e:
            result[sym] = {"enabled": False, "signal": "neutral", "error": str(e)}
    
    return result


@app.get("/snapshots/{filename}")
def snapshot(filename: str):
    from fastapi.responses import FileResponse
    path = HERE / "snapshots" / filename
    if not path.exists():
        raise HTTPException(404, "snapshot not found")
    return FileResponse(str(path), media_type="image/png")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")