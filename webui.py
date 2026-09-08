"""webui.py — локальная веб-морда: свечки + зоны + сигналы.
Запуск: python webui.py  ->  http://localhost:8000
Роуты: / (интерфейс), /api/status, /api/chart, /api/analysis
"""
import html
import json
import re
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


@app.get("/tv", response_class=HTMLResponse)
def tradingview():
    from fastapi.responses import Response
    html = (HERE / "web" / "tradingview.html").read_text(encoding="utf-8")
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


_RU_KEY_STOP = (
    "сигнал:", "цена:", "вход-скор", "уверенность", "действие",
    "волатильность", "mean-reversion", "фибо", "трендовые линии",
    "уровни по", "сессия:", "индивидуальная", "факторы скора",
    "структура:", "журнал", "новости:", "дисциплина:", "kill zone",
    "kz:", "m5 снайпер", "asian range", "бэктест", "дисклеймер",
    "disclaimer", "цены закрытия", "тренды",
)


def _parse_advice(text: str) -> dict:
    """Parse advice .md (RU labels) into dashboard fields. Falls back to EN keys."""
    out: dict = {}
    last_price = signal = entry_score = entry = sl = tp = lot = None
    rsi_h1 = atr_h1 = None
    trends = {}
    reasons = []

    def _f(v):
        try:
            s = str(v).strip().rstrip(".,;)]}% ")
            return float(s.replace(",", "."))
        except Exception:
            return None

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        body = re.sub(r"^[-*]\s*", "", line).replace("\u2022", "").strip()
        low = body.lower()

        # ВЕТО / СТОП-ТОРГОВЛЯ
        if "вето" in low or "стоп-торговля" in low:
            out["veto"] = html.escape(line.strip("*").strip())
            continue

        # ВХОД-СКОР: 80/100
        if "вход-скор" in low or "вход скор" in low:
            m = re.search(r"(\d+)\s*/\s*100", line)
            if m:
                entry_score = int(m.group(1))
            continue

        # - Цена: 1.353 | Тренды D1 bullish / ...
        if low.startswith("- цена:") or low.startswith("цена:"):
            after = line.split(":", 1)[1] if ":" in line else line
            last_price = _f(after.split("|")[0])
            continue

        # Тренды D1 x / H4 y / H1 z / W1 w
        if "тренды" in low:
            for tf in ("w1", "d1", "h4", "h1"):
                m = re.search(rf"\b{tf}\b\s*[:=]?\s*([a-z]+)", low)
                if m:
                    trends[tf] = m.group(1)
            continue

        # Сигнал: NEUTRAL / BUY / SELL
        if low.startswith("сигнал:"):
            val = line.split(":", 1)[1].strip().lower()
            signal = "buy" if "buy" in val else "sell" if "sell" in val else "neutral"
            continue

        # ДЕЙСТВИЕ: шорт от 1.158 (...), SL 1.164, TP 1.152
        if low.startswith("действие"):
            m = re.search(r"\b(?:от|у)\s+([\d.,]+)", low)
            if m:
                entry = _f(m.group(1))
            m = re.search(r"\bsl\s+([\d.,]+)", low)
            if m:
                sl = _f(m.group(1))
            m = re.search(r"\btp\s+([\d.,]+)", low)
            if m:
                tp = _f(m.group(1))
            continue

        # Mean-reversion: RSI H1 = 41.5 ...
        if "rsi" in low and "=" in low:
            m = re.search(r"rsi\s*h?1?\s*=\s*([\d.,]+)", low)
            if m:
                rsi_h1 = _f(m.group(1))
            continue

        # Волатильность H1 ATR: 0.001
        if "atr" in low:
            m = re.search(r"atr\s*[:=]\s*([\d.,]+)", low)
            if m:
                atr_h1 = _f(m.group(1))
            continue

        # --- EN fallbacks ---
        if "signal:" in low:
            val = line.split(":", 1)[1].strip().lower()
            signal = signal or ("buy" if "buy" in val else "sell" if "sell" in val else "neutral")
            continue
        if "entry_score:" in low and entry_score is None:
            m = re.search(r"(\d+)", line.split(":", 1)[1])
            if m:
                entry_score = int(m.group(1))
            continue
        if "last_price:" in low and last_price is None:
            last_price = _f(line.split(":", 1)[1])
            continue
        if "entry:" in low and "entry_score" not in low and entry is None:
            entry = _f(line.split(":", 1)[1])
            continue
        if "sl:" in low and sl is None:
            sl = _f(line.split(":", 1)[1])
            continue
        if "tp:" in low and tp is None:
            tp = _f(line.split(":", 1)[1])
            continue
        if "lot:" in low and lot is None:
            lot = _f(line.split(":", 1)[1])
            continue
        if "rsi_h1:" in low and rsi_h1 is None:
            rsi_h1 = _f(line.split(":", 1)[1])
            continue
        if "atr_h1:" in low and atr_h1 is None:
            atr_h1 = _f(line.split(":", 1)[1])
            continue
        en_trend = False
        for tf in ("d1", "h4", "h1"):
            if f"trend_{tf}:" in low and tf not in trends:
                trends[tf] = line.split(":", 1)[1].strip()
                en_trend = True
        if en_trend:
            continue

        # bullet reasons (skip structured/header lines)
        if (line.startswith("- ") or line.startswith("• ")) and len(line) > 3:
            reason = line.lstrip("- ").lstrip("• ").strip()
            if reason and not any(k in reason.lower() for k in _RU_KEY_STOP):
                reasons.append(reason)

    if last_price is not None:
        out["last_price"] = last_price
    if signal:
        out["signal"] = signal
    if entry_score is not None:
        out["entry_score"] = entry_score
    if entry is not None:
        out["entry"] = entry
    if sl is not None:
        out["sl"] = sl
    if tp is not None:
        out["tp"] = tp
    if lot is not None:
        out["lot"] = lot
    if rsi_h1 is not None:
        out["rsi_h1"] = rsi_h1
    if atr_h1 is not None:
        out["atr_h1"] = atr_h1
    if trends:
        out["trends"] = {tf: html.escape(v) for tf, v in trends.items()}
    if reasons:
        out["entry_reasons"] = [html.escape(r) for r in reasons[:8]]
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

            data = {"enabled": stats.get(sym, {}).get("disabled") == False}
            data["advice_text"] = html.escape(text).replace("\n", "<br>")
            data.update(_parse_advice(text))
            data.setdefault("signal", "neutral")

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


@app.get("/api/fxssi")
def fxssi():
    """Настроение рынка FXSSI по всем нашим символам (% розничных покупок)."""
    try:
        import fxssi as fxssi_mod
        pairs = fxssi_mod.fetch_sentiment()
    except Exception as exc:
        raise HTTPException(502, f"fxssi fetch failed: {exc}")
    if not pairs:
        raise HTTPException(502, "fxssi недоступен")
    rows = {}
    for sym, fpair in fxssi_mod.FXSSI_SYMBOLS.items():
        p = pairs["pairs"].get(fpair)
        if not p:
            continue
        b = fxssi_mod.bias(sym)
        rows[sym] = {
            "pair": fpair,
            "buy_pct": p["buy"],
            "sell_pct": p["sell"],
            "broker_spread": p.get("spread"),
            "bias": b.get("bias", "neutral"),
            "reason": b.get("reason", ""),
        }
    return {
        "symbols": rows,
        "server_time": pairs.get("server_time_text"),
        "formed": pairs.get("formed"),
        "fetched": pairs.get("fetched"),
    }


@app.get("/snapshots/{filename}")
def snapshot(filename: str):
    from fastapi.responses import FileResponse
    name = Path(filename).name
    if name != filename or not name.lower().endswith((".png", ".jpg", ".jpeg")):
        raise HTTPException(400, "bad filename")
    path = HERE / "snapshots" / name
    if not path.is_file():
        raise HTTPException(404, "snapshot not found")
    return FileResponse(str(path), media_type="image/png")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")