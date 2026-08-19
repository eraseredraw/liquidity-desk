"""calendar.py — экономический календарь: вето за 30 мин до high-impact событий.

Бесплатный API: https://nfs.faireconomy.media/ff_calendar_thisweek.json
Кэш 1 час. Graceful fallback: любые ошибки → {"veto": False}.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
CACHE_FILE = HERE / "calendar_cache.json"
CACHE_TTL = 3600

SYMBOL_CURRENCIES = {
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "GBPJPY": ["GBP", "JPY"],
    "XAUUSD": ["USD", "XAU"],
    "DAX":    ["EUR"],
}

RELEVANT_COUNTRIES = {"EUR": "EU", "USD": "US", "GBP": "UK", "JPY": "JP"}

HIGH_IMPACT_KEYWORDS = [
    "nfp", "non-farm", "payroll",
    "cpi", "inflation",
    "fomc", "fed rate", "interest rate",
    "gdp",
    "unemployment",
    "pmi manufacturing",
    "ecb rate", "boe rate", "boj rate",
]


def fetch_calendar() -> list:
    """High-impact события недели: кэш 1 час, при ошибке — пустой список."""
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - data.get("fetched_at", 0) < CACHE_TTL:
                return data.get("events", [])
        except Exception:
            pass

    events = []
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        raw = resp.json()
        for e in raw:
            if str(e.get("impact", "")).lower() == "high":
                events.append({
                    "title": e.get("title", ""),
                    "country": e.get("country", ""),
                    "date": e.get("date", ""),
                    "impact": "high",
                })
    except Exception:
        pass

    try:
        CACHE_FILE.write_text(
            json.dumps({"fetched_at": time.time(), "events": events}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    return events


def check_news_veto(symbol: str, window_minutes: int = 30) -> dict:
    """High-impact событие для валют символа в ближайшие window_minutes
    или прошедшее < 15 мин назад → вето. Иначе {"veto": False}."""
    currencies = SYMBOL_CURRENCIES.get(symbol, ["USD"])
    now = datetime.now(timezone.utc)
    events = fetch_calendar()

    for event in events:
        title = event.get("title", "")
        country = str(event.get("country", "")).upper()
        relevant = any(
            RELEVANT_COUNTRIES.get(c, "") == country or c in country
            for c in currencies
        )
        if not relevant:
            continue
        event_dt_str = event.get("date", "")
        if not event_dt_str:
            continue
        try:
            event_dt = datetime.fromisoformat(event_dt_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)

        delta_minutes = (event_dt - now).total_seconds() / 60
        if -15 <= delta_minutes <= window_minutes:
            minutes_to = int(delta_minutes)
            if minutes_to >= 0:
                reason = f"HIGH IMPACT событие через {minutes_to} мин: {title} ({country})"
            else:
                reason = f"HIGH IMPACT событие {abs(minutes_to)} мин назад: {title} ({country}) — ждать нормализации"
            return {
                "veto": True,
                "reason": reason,
                "event_title": title,
                "minutes_to": minutes_to,
            }

    return {"veto": False, "reason": None, "event_title": None, "minutes_to": None}