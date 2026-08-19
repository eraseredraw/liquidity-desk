"""providers.py — источники свечей: TwelveData -> Finnhub -> Yahoo.
Цепочка фолбэков: провайдер выше падает — берём следующий, Yahoo всегда последний.
Ключи: data_source.json или env (TWELVEDATA_API_KEY, FINNHUB_API_KEY).
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).parent

SYMBOL_MAP = {
    "GC=F": ("XAU/USD", "OANDA:XAU_USD"),
    "GBPUSD=X": ("GBP/USD", "OANDA:GBP_USD"),
    "EURUSD=X": ("EUR/USD", "OANDA:EUR_USD"),
    "GBPJPY=X": ("GBP/JPY", "OANDA:GBP_JPY"),
    "^GDAXI": ("GDAXI", "OANDA:GER30"),
}

TD_INTERVAL = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1day", "1wk": "1week", "5m": "5min"}


def _td_outputsize(interval: str, range_: str) -> int:
    table = {
        ("15m", "5d"): 600,
        ("1h", "2mo"): 1500,
        ("1h", "5d"): 400,
        ("4h", "3mo"): 700,
        ("4h", "2mo"): 500,
        ("1d", "6mo"): 300,
        ("1d", "2y"): 1000,
        ("1d", "3mo"): 200,
    }
    return table.get((interval, range_), 1500)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def load_config() -> dict:
    cfg = {"twelvedata_key": "", "finnhub_key": ""}
    path = HERE / "data_source.json"
    try:
        if path.exists():
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        pass
    import os
    cfg["twelvedata_key"] = os.environ.get("TWELVEDATA_API_KEY", cfg.get("twelvedata_key", ""))
    cfg["finnhub_key"] = os.environ.get("FINNHUB_API_KEY", cfg.get("finnhub_key", ""))
    return cfg


def _df_from_rows(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows).dropna()
    if len(df):
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.set_index("ts")
        df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def twelvedata(symbol: str, interval: str, range_: str) -> pd.DataFrame:
    key = load_config()["twelvedata_key"]
    if not key:
        raise RuntimeError("twelvedata: no API key")
    td_sym = SYMBOL_MAP.get(symbol, (None, None))[0]
    if not td_sym:
        raise RuntimeError(f"twelvedata: no mapping for {symbol}")
    url = ("https://api.twelvedata.com/time_series"
           f"?symbol={td_sym}&interval={TD_INTERVAL[interval]}"
           f"&outputsize={_td_outputsize(interval, range_)}&timezone=UTC&apikey={key}")
    data = requests.get(url, headers=HEADERS, timeout=20).json()
    if data.get("status") != "ok":
        raise RuntimeError(f"twelvedata: {data.get('message', data.get('code', 'fail'))}")
    rows = [{
        "ts": r["datetime"], "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
        "volume": float(r.get("volume") or 0),
    } for r in data["values"]]
    return _df_from_rows(rows)


def finnhub(symbol: str, interval: str, range_: str) -> pd.DataFrame:
    key = load_config()["finnhub_key"]
    if not key:
        raise RuntimeError("finnhub: no API key")
    fh_sym = SYMBOL_MAP.get(symbol, (None, None))[1]
    if not fh_sym:
        raise RuntimeError(f"finnhub: no mapping for {symbol}")
    fh_res = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": "D", "1wk": "W"}[interval]
    fh_days = {"5d": 6, "2mo": 62, "3mo": 92, "6mo": 184, "2y": 735}[range_]
    to = int(time.time())
    frm = to - fh_days * 86400
    url = (f"https://finnhub.io/api/v1/forex/candle?symbol={fh_sym}"
           f"&resolution={fh_res}&from={frm}&to={to}&token={key}")
    data = requests.get(url, headers=HEADERS, timeout=20).json()
    if data.get("s") != "ok":
        raise RuntimeError(f"finnhub: {data.get('s', 'fail')}")
    rows = [{
        "ts": t, "open": o, "high": h, "low": l, "close": c,
        "volume": v if v is not None else 0,
    } for t, o, h, l, c, v in zip(data["t"], data["o"], data["h"], data["l"], data["c"], data.get("v", [0] * len(data["t"])))]
    return _df_from_rows(rows)


def fetch(symbol: str, interval: str, range_: str, prefer: str = None) -> pd.DataFrame:
    """Провайдер-цепочка. symbol — Yahoo-тикер (GC=F, GBPUSD=X...).
    prefer: 'twelvedata' | 'finnhub' | 'yahoo' | None (порядок по умолчанию)."""
    import sys
    sys.path.insert(0, str(HERE))
    from strategy import fetch_candles_yahoo

    order = [prefer] if prefer else ["twelvedata", "finnhub"]
    errs = []
    for name in order:
        fn = {"twelvedata": twelvedata, "finnhub": finnhub}.get(name)
        if not fn:
            continue
        try:
            df = fn(symbol, interval, range_)
            if df is not None and len(df) >= 2:
                return df
        except Exception as exc:
            errs.append(f"{name}: {exc}")
    return fetch_candles_yahoo(symbol, interval, range_)