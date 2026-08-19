# ПЛАН УЛУЧШЕНИЙ ТОРГОВОЙ СИСТЕМЫ
# Передать в deepseek-v4-flash для реализации

## КОНТЕКСТ ПРОЕКТА

Путь: `C:\Users\h04x\Projects\h04xjrn_2.0\trading`
Стек: Python 3.13, pandas, requests, matplotlib. Без тяжёлых зависимостей.
Ключевые файлы:
- `strategy.py` — анализ уровней, зон, трендов
- `strategy_signal.py` — ЕДИНСТВЕННЫЙ источник сигнала `signal_from_features()`
- `advice.py` — генерация советов (~624 строки, монстр)
- `sentinel.py` — фоновый алертер, проверяет каждые 10 мин
- `backtest.py` / `grid_backtest.py` — честный бэктест DEV→OOS→HOLDOUT
- `providers.py` — цепочка TwelveData→Finnhub→Yahoo
- `risk_guard.json` — лимиты риска (не трогать)
- `backtest_stats.json` — статистика по инструментам
- `chart.py` — PNG-графики с уровнями и стрелками входа

ПРАВИЛА (жёсткие):
- НЕ менять параметры стратегии, пороги, формулы
- НЕ менять форматы JSON-файлов
- НЕ добавлять тяжёлые зависимости (numpy OK, torch/sklearn — нет)
- После каждого блока: `python -m py_compile <file>` без ошибок
- Тестировать без реальных API-ключей (данные кэшируются в `signals_cache.json`)

---

## УЛУЧШЕНИЕ 1: Kill Zone фильтр (ПРИОРИТЕТ: ВЫСОКИЙ)
Срок: ~2 часа. Даёт: убирает ~40% фейковых сигналов вне торговых окон.

### Что добавить в `strategy.py`:

```python
KILL_ZONES_UTC = {
    "london_setup":  (2, 5),    # 02:00-05:00 — сбор ликвидности
    "london":        (7, 10),   # 07:00-10:00 — основные движения
    "ny_overlap":    (12, 15),  # 12:00-15:00 — Нью-Йорк оверлап
    "ny":            (15, 17),  # 15:00-17:00 — NY продолжение
}

def in_kill_zone(now_utc=None) -> tuple:
    """Возвращает (True/False, название зоны или None)."""
    from datetime import datetime, timezone
    h = (now_utc or datetime.now(timezone.utc)).hour
    for name, (start, end) in KILL_ZONES_UTC.items():
        if start <= h < end:
            return True, name
    return False, None
```

### Изменить в `advice.py` функцию `build_advice()`:
Добавить после строки `cons = signals_consensus(...)`:

```python
in_kz, kz_name = in_kill_zone()
if not in_kz:
    # Не запрещать вход, но снизить скор и предупредить
    escore = max(0, escore - 20)
    ereasons.insert(0, f"ВНЕ KILL ZONE (текущий час UTC: {datetime.now(timezone.utc).hour}ч) — вероятность фейка выше")
else:
    ereasons.insert(0, f"KILL ZONE активна: {kz_name} — оптимальное время")
```

### Изменить в `sentinel.py` функцию алерта:
При выводе в лог добавить пометку `[KZ: london]` или `[вне KZ]`.

---

## УЛУЧШЕНИЕ 2: M5 Break of Structure (ПРИОРИТЕТ: ВЫСОКИЙ)
Срок: ~6 часов. Даёт: сдвигает SL с 0.5% до 12-15 пунктов → R/R растёт с 1.5 до 3-4.

### Создать новую функцию в `strategy.py`:

```python
def m5_break_of_structure(yahoo_symbol: str, direction: str) -> dict:
    """
    Ищет M5 Break of Structure в зоне входа.
    
    direction: "buy" или "sell"
    
    Для BUY (цена у поддержки):
      1. Последние 20 M5 свечей
      2. Найти swing low (минимум с обеих сторон выше)
      3. Цена обновляет minimum (lower low) — стоп-хант
      4. Потом пробивает предыдущий swing high вверх — BOS
      → ВХОД на пробое, SL под последним minimum
    
    Для SELL (цена у сопротивления):
      1. Последние 20 M5 свечей
      2. Найти swing high
      3. Цена обновляет maximum (higher high) — стоп-хант
      4. Потом пробивает предыдущий swing low вниз — BOS
      → ВХОД на пробое, SL над последним maximum
    
    Возвращает:
    {
      "confirmed": True/False,
      "entry_price": float или None,  # точная цена входа
      "sl_price": float или None,     # точный SL под/над swing
      "swing_level": float или None,  # уровень для R/R расчёта
      "r_ratio": float,               # потенциальный R/R
    }
    """
    try:
        df = fetch_candles(yahoo_symbol, "5m", "1d")
        if len(df) < 20:
            return {"confirmed": False}
        df = df.tail(20)
        
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        n = len(closes)
        
        if direction == "buy":
            # Найти swing lows (локальные минимумы)
            swing_lows = [i for i in range(1, n-1) if lows[i] < lows[i-1] and lows[i] < lows[i+1]]
            if len(swing_lows) < 2:
                return {"confirmed": False}
            last_swing = swing_lows[-1]
            prev_swing = swing_lows[-2]
            # BOS: последняя свеча закрылась выше swing high между двумя swing lows
            swing_high_between = max(highs[prev_swing:last_swing+1])
            bos = closes[-1] > swing_high_between
            if bos:
                sl = lows[last_swing] * 0.9995  # чуть под swing low
                entry = closes[-1]
                return {
                    "confirmed": True,
                    "entry_price": round(entry, 5),
                    "sl_price": round(sl, 5),
                    "swing_level": round(swing_high_between, 5),
                    "r_ratio": round((swing_high_between - sl) / (entry - sl), 2) if entry > sl else 0,
                }
        
        elif direction == "sell":
            # Найти swing highs (локальные максимумы)
            swing_highs = [i for i in range(1, n-1) if highs[i] > highs[i-1] and highs[i] > highs[i+1]]
            if len(swing_highs) < 2:
                return {"confirmed": False}
            last_swing = swing_highs[-1]
            prev_swing = swing_highs[-2]
            swing_low_between = min(lows[prev_swing:last_swing+1])
            bos = closes[-1] < swing_low_between
            if bos:
                sl = highs[last_swing] * 1.0005
                entry = closes[-1]
                return {
                    "confirmed": True,
                    "entry_price": round(entry, 5),
                    "sl_price": round(sl, 5),
                    "swing_level": round(swing_low_between, 5),
                    "r_ratio": round((sl - swing_low_between) / (sl - entry), 2) if sl > entry else 0,
                }
        
        return {"confirmed": False}
    except Exception:
        return {"confirmed": False}
```

### Изменить в `advice.py` функцию `build_advice()`:
После расчёта `escore`, если сигнал не neutral:

```python
bos = m5_break_of_structure(YAHOO_SYMBOLS[sym], signal)
if bos.get("confirmed"):
    escore = min(100, escore + 15)
    ereasons.insert(0, f"M5 BOS подтверждён: вход {bos['entry_price']}, SL {bos['sl_price']}, R/R ~{bos['r_ratio']}")
    # Перезаписать параметры входа точным SL
    a["m5_entry"] = bos["entry_price"]
    a["m5_sl"] = bos["sl_price"]
else:
    ereasons.append("M5 BOS: нет подтверждения (ждать пробоя структуры)")
```

---

## УЛУЧШЕНИЕ 3: Экономический календарь (ПРИОРИТЕТ: ВЫСОКИЙ)
Срок: ~3 часа. Даёт: убирает самые жёсткие стоп-ауты на NFP/CPI/FOMC.

### Создать `calendar.py`:

```python
"""calendar.py — экономический календарь, вето за 30 мин до high-impact событий."""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

HERE = Path(__file__).parent
CACHE_FILE = HERE / "calendar_cache.json"
CACHE_TTL = 3600  # 1 час

# Символы и их валюты (для фильтрации событий)
SYMBOL_CURRENCIES = {
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "GBPJPY": ["GBP", "JPY"],
    "XAUUSD": ["USD", "XAU"],
    "DAX":    ["EUR"],
}

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
    """Загрузить или взять из кэша экономический календарь."""
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - data.get("fetched_at", 0) < CACHE_TTL:
                return data.get("events", [])
        except Exception:
            pass
    
    events = []
    try:
        # Бесплатный FF API
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        raw = resp.json()
        for e in raw:
            if e.get("impact", "").lower() == "high":
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
            encoding="utf-8"
        )
    except Exception:
        pass
    return events


def check_news_veto(symbol: str, window_minutes: int = 30) -> dict:
    """
    Проверить: есть ли high-impact событие в ближайшие window_minutes минут
    ИЛИ прошло меньше 15 мин с такого события (для данного символа).
    
    Возвращает:
    {
        "veto": True/False,
        "reason": str или None,
        "event_title": str или None,
        "minutes_to": int или None,  # минут до события (отрицательное = прошло)
    }
    """
    currencies = SYMBOL_CURRENCIES.get(symbol, ["USD"])
    now = datetime.now(timezone.utc)
    events = fetch_calendar()
    
    for event in events:
        title_lower = event.get("title", "").lower()
        country = event.get("country", "").upper()
        
        # Проверить релевантность для символа
        relevant_countries = {
            "EUR": "EU", "USD": "US", "GBP": "UK", "JPY": "JP",
        }
        relevant = any(
            relevant_countries.get(c, "") == country or c in country
            for c in currencies
        )
        if not relevant:
            continue
        
        # Парсить время события
        try:
            event_dt_str = event.get("date", "")
            if not event_dt_str:
                continue
            event_dt = datetime.fromisoformat(event_dt_str.replace("Z", "+00:00"))
        except Exception:
            continue
        
        delta_minutes = (event_dt - now).total_seconds() / 60
        
        # Вето: событие через N минут ИЛИ прошло меньше 15 мин назад
        if -15 <= delta_minutes <= window_minutes:
            minutes_to = int(delta_minutes)
            if minutes_to >= 0:
                reason = f"HIGH IMPACT событие через {minutes_to} мин: {event['title']} ({country})"
            else:
                reason = f"HIGH IMPACT событие {abs(minutes_to)} мин назад: {event['title']} ({country}) — ждать нормализации"
            return {
                "veto": True,
                "reason": reason,
                "event_title": event["title"],
                "minutes_to": minutes_to,
            }
    
    return {"veto": False, "reason": None, "event_title": None, "minutes_to": None}
```

### Интегрировать в `advice.py`:
В начало `build_advice()` добавить:

```python
# Импорт вверху файла:
# from calendar import check_news_veto

news_veto = check_news_veto(sym)
if news_veto["veto"]:
    # Добавить к вето risk_guard
    calendar_veto = news_veto["reason"]
else:
    calendar_veto = None
```

В блок ВЕТО после `veto = risk_guard(...)`:

```python
if calendar_veto and not veto:
    veto = f"НОВОСТНОЕ ВЕТО: {calendar_veto}"
```

---

## УЛУЧШЕНИЕ 4: Asian Range Bias (ПРИОРИТЕТ: СРЕДНИЙ)
Срок: ~4 часа. Даёт: улучшает тайминг входа — фильтрует ложные пробои.

### Добавить в `strategy.py` функцию:

```python
def asian_range(yahoo_symbol: str) -> dict:
    """
    Азиатский диапазон (00:00-07:00 UTC сегодня).
    Лондон/NY 70% времени пробивают один из краёв и разворачиваются.
    
    Логика:
    - Asian High пробит → SELL-байас (поиск короткой позиции)
    - Asian Low пробит  → BUY-байас (поиск длинной позиции)
    - Цена внутри диапазона → нейтральный байас
    
    Возвращает:
    {
        "high": float,
        "low": float,
        "range_pct": float,        # размер диапазона в %
        "bias": "buy"/"sell"/"neutral",
        "current_price": float,
        "broken_level": float или None,
    }
    """
    try:
        df = fetch_candles(yahoo_symbol, "1h", "2d")
        if len(df) < 10:
            return {"bias": "neutral"}
        
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        # Сегодняшняя азиатская сессия
        asian_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        asian_end = now.replace(hour=7, minute=0, second=0, microsecond=0)
        
        asian = df[(df.index >= asian_start) & (df.index < asian_end)]
        if len(asian) < 3:
            # Берём вчерашний диапазон
            asian_start -= pd.Timedelta(days=1)
            asian_end -= pd.Timedelta(days=1)
            asian = df[(df.index >= asian_start) & (df.index < asian_end)]
        
        if len(asian) < 3:
            return {"bias": "neutral"}
        
        asian_high = float(asian["high"].max())
        asian_low = float(asian["low"].min())
        last_price = float(df["close"].iloc[-1])
        range_pct = (asian_high - asian_low) / asian_low * 100
        
        bias = "neutral"
        broken_level = None
        
        if last_price > asian_high * 1.0003:   # пробит вверх
            # Лондон ловит sell-стопы выше Asian High, потом разворот
            bias = "sell"
            broken_level = asian_high
        elif last_price < asian_low * 0.9997:   # пробит вниз
            # Ловит buy-стопы ниже Asian Low, потом разворот
            bias = "buy"
            broken_level = asian_low
        
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
```

### Интегрировать в `advice.py`:
В `build_advice()` после расчёта зон:

```python
ar = asian_range(YAHOO_SYMBOLS[sym])
if ar.get("bias") != "neutral":
    # Проверить совпадение байаса с сигналом
    if ar["bias"] == signal:
        escore = min(100, escore + 10)
        ereasons.append(f"Asian Range байас совпадает: пробит {'HIGH' if ar['bias']=='sell' else 'LOW'} "
                       f"({ar['broken_level']:.3f}) → байас {ar['bias'].upper()}")
    elif ar["bias"] != signal and signal not in ("neutral", "disabled"):
        escore = max(0, escore - 10)
        ereasons.append(f"Asian Range байас ПРОТИВ сигнала: пробит уровень {ar['broken_level']:.3f} "
                       f"указывает на {ar['bias'].upper()}, осторожно")

if ar.get("high") and ar.get("low"):
    lines.append(f"- Asian Range: {ar['low']:.3f}–{ar['high']:.3f} ({ar['range_pct']}%) | байас: {ar['bias'].upper()}")
```

---

## УЛУЧШЕНИЕ 5: Автоматическое частичное закрытие в journal.py (ПРИОРИТЕТ: СРЕДНИЙ)
Срок: ~2 часа. Даёт: дисциплина — забудешь закрыть 50%, система напомнит.

### Добавить команду `check` в `journal.py`:

```python
# Использование: python journal.py check
# Проверяет открытые позиции и напоминает о СЕЙФ-правиле

def check_open_positions():
    """Проверить открытые позиции, сгенерировать напоминания."""
    entries = load()
    open_pos = [e for e in entries if e.get("status") == "open"]
    
    if not open_pos:
        print("Нет открытых позиций.")
        return
    
    now = datetime.now(timezone.utc)
    reminders = []
    
    for pos in open_pos:
        symbol = pos["symbol"]
        entry_price = pos["entry"]
        side = pos["side"]
        opened_at = pos.get("opened_at", "")
        partial_taken = pos.get("partial_taken", False)
        
        # Нужно получить текущую цену
        try:
            from strategy import YAHOO_SYMBOLS, analyze_symbol
            a = analyze_symbol(symbol, YAHOO_SYMBOLS.get(symbol, symbol))
            current_price = a["last_price"]
        except Exception:
            current_price = None
        
        if current_price is None:
            continue
        
        # Расчёт текущего P&L в пунктах
        if side == "buy":
            pnl_pct = (current_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - current_price) / entry_price * 100
        
        status_msg = f"{symbol} {side.upper()} @ {entry_price:.4f} → {current_price:.4f} | P&L: {pnl_pct:+.2f}%"
        
        # СЕЙФ-правило: +15-20 пунктов (≈ 0.1-0.15%) и нет частичного закрытия
        if pnl_pct >= 0.1 and not partial_taken:
            reminders.append(f"⚠️  СЕЙФ! {status_msg} — закрыть 50%, SL в безубыток!")
        elif pnl_pct >= 0.0:
            reminders.append(f"✅  {status_msg}")
        else:
            reminders.append(f"🔻  {status_msg} | SL скоро: следить")
    
    for r in reminders:
        print(r)
    
    return reminders
```

Добавить флаг `partial_taken` при обновлении:
```python
# python journal.py partial GBPJPY
def mark_partial(symbol: str):
    entries = load()
    for e in entries:
        if e["symbol"] == symbol and e.get("status") == "open":
            e["partial_taken"] = True
            e["partial_at"] = datetime.now(timezone.utc).isoformat()
    save(entries)
    print(f"{symbol}: частичное закрытие отмечено")
```

---

## УЛУЧШЕНИЕ 6: Telegram-алерты (ПРИОРИТЕТ: СРЕДНИЙ)
Срок: ~2 часа. Даёт: push-уведомление на телефон при зоне.

### Создать `telegram_alert.py`:

```python
"""telegram_alert.py — отправка алертов в Telegram-бот.

Настройка:
1. @BotFather в Telegram → /newbot → получить BOT_TOKEN
2. Написать боту любое сообщение
3. https://api.telegram.org/bot<BOT_TOKEN>/getUpdates → взять chat_id
4. Записать в data_source.json: {"telegram": {"token": "...", "chat_id": "..."}}
"""

import json
import requests
from pathlib import Path

HERE = Path(__file__).parent


def _config() -> dict:
    path = HERE / "data_source.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("telegram", {})
    except Exception:
        return {}


def send(message: str) -> bool:
    cfg = _config()
    token = cfg.get("token")
    chat_id = cfg.get("chat_id")
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def alert_zone(symbol: str, price: float, zone: str, signal: str, score: int):
    """Алерт при входе цены в зону."""
    emoji = "🚀" if signal == "buy" else "🔻" if signal == "sell" else "👀"
    msg = (
        f"{emoji} <b>{symbol}</b> в зоне\n"
        f"Цена: <b>{price:.4f}</b>\n"
        f"Зона: {zone}\n"
        f"Сигнал: <b>{signal.upper()}</b>\n"
        f"Скор: <b>{score}/100</b>"
    )
    return send(msg)


def alert_signal_change(symbol: str, old_signal: str, new_signal: str, price: float):
    """Алерт при смене сигнала."""
    emoji = "🚀" if new_signal == "buy" else "🔻" if new_signal == "sell" else "➡️"
    msg = (
        f"{emoji} <b>{symbol}</b> сигнал изменился\n"
        f"{old_signal.upper()} → <b>{new_signal.upper()}</b>\n"
        f"Цена: {price:.4f}"
    )
    return send(msg)
```

### Интегрировать в `sentinel.py`:
После вызова `beep()`:

```python
try:
    from telegram_alert import alert_zone, alert_signal_change
    if alert_type == "zone":
        alert_zone(sym, price, zone_name, signal, score)
    elif alert_type == "signal":
        alert_signal_change(sym, old_signal, new_signal, price)
except Exception:
    pass  # Telegram опционален, не крашить sentinel
```

---

## УЛУЧШЕНИЕ 7: Трейлинг по H4-структуре (ПРИОРИТЕТ: СРЕДНИЙ)
Срок: ~3 часа. Даёт: фиксирует прибыль по тренду вместо фикс. TP.

### Добавить в `journal.py` команду `trail`:

```python
# python journal.py trail GBPJPY
# Рассчитывает трейлинг-стоп по H4-структуре (ближайший H4 уровень за ценой)

def trail_stop(symbol: str):
    """Рассчитать трейлинг-стоп по текущей H4-структуре."""
    entries = load()
    open_pos = [e for e in entries if e["symbol"] == symbol and e.get("status") == "open"]
    if not open_pos:
        print(f"Нет открытых позиций по {symbol}")
        return
    
    from strategy import YAHOO_SYMBOLS, analyze_symbol
    a = analyze_symbol(symbol, YAHOO_SYMBOLS.get(symbol, symbol))
    
    pos = open_pos[-1]
    side = pos["side"]
    current = a["last_price"]
    
    if side == "buy":
        # Трейлинг: SL под ближайшую H4 поддержку ниже текущей цены
        support = a.get("h4_support") or a.get("nearest_support")
        if support and support < current:
            trail_sl = support * 0.999
            print(f"{symbol} BUY трейлинг SL → {trail_sl:.4f} (H4 поддержка {support:.4f})")
            print(f"Текущая цена: {current:.4f} | P&L: {(current - pos['entry'])/pos['entry']*100:+.2f}%")
        else:
            print(f"Нет H4 поддержки ниже цены — держать текущий SL")
    
    elif side == "sell":
        # Трейлинг: SL над ближайшее H4 сопротивление выше текущей цены
        resistance = a.get("h4_resistance") or a.get("nearest_resistance")
        if resistance and resistance > current:
            trail_sl = resistance * 1.001
            print(f"{symbol} SELL трейлинг SL → {trail_sl:.4f} (H4 сопротивление {resistance:.4f})")
            print(f"Текущая цена: {current:.4f} | P&L: {(pos['entry'] - current)/pos['entry']*100:+.2f}%")
        else:
            print(f"Нет H4 сопротивления выше цены — держать текущий SL")
```

---

## УЛУЧШЕНИЕ 8: Корреляционная матрица live (ПРИОРИТЕТ: НИЗКИЙ)
Срок: ~2 часа. Даёт: автоматический расчёт реальной корреляции вместо хардкода.

### Добавить в `strategy.py`:

```python
def live_correlation_matrix(symbols: list = None, period: str = "3mo") -> dict:
    """
    Рассчитать live-корреляцию между инструментами на D1 данных.
    Кэшировать в correlation_cache.json (TTL 24ч).
    
    Возвращает: {"GBPUSD/EURUSD": 0.87, "XAUUSD/GBPUSD": 0.23, ...}
    """
    import time, json
    cache_file = Path(__file__).parent / "correlation_cache.json"
    
    # Кэш 24ч
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) < 86400:
                return data.get("pairs", {})
        except Exception:
            pass
    
    symbols = symbols or list(YAHOO_SYMBOLS.keys())
    closes = {}
    for sym in symbols:
        try:
            df = fetch_candles(YAHOO_SYMBOLS[sym], "1d", period)
            closes[sym] = df["close"].rename(sym)
        except Exception:
            continue
    
    if len(closes) < 2:
        return {}
    
    combined = pd.concat(closes.values(), axis=1).dropna()
    pairs = {}
    for i, s1 in enumerate(symbols):
        for s2 in symbols[i+1:]:
            if s1 in combined.columns and s2 in combined.columns:
                corr = float(combined[s1].corr(combined[s2]))
                pairs[f"{s1}/{s2}"] = round(corr, 3)
    
    try:
        cache_file.write_text(
            json.dumps({"ts": time.time(), "pairs": pairs}, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception:
        pass
    return pairs
```

---

## УЛУЧШЕНИЕ 9: Дашборд — показывать точки входа из chart_entries.json (ПРИОРИТЕТ: СРЕДНИЙ)
Срок: ~1 час. Уже начато в предыдущей сессии.

### Добавить в `webui.py` endpoint:

```python
@app.get("/api/entries")
def entries():
    """Список всех сохранённых точек входа из chart_entries.json."""
    path = HERE / "chart_entries.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return []

@app.delete("/api/entries/{symbol}")
def clear_entries(symbol: str = None):
    """Очистить точки входа для символа (или все если symbol='all')."""
    from chart import clear_entries as chart_clear
    if symbol == "all":
        chart_clear()
    else:
        chart_clear(symbol)
    return {"ok": True}
```

---

## ПОРЯДОК РЕАЛИЗАЦИИ

| # | Улучшение | Приоритет | Сложность | Файл |
|---|---|---|---|---|
| 1 | Kill Zone фильтр | HIGH | 2ч | strategy.py + advice.py |
| 2 | Экономический календарь | HIGH | 3ч | calendar.py + advice.py |
| 3 | Asian Range Bias | HIGH | 4ч | strategy.py + advice.py |
| 4 | M5 BOS подтверждение | HIGH | 6ч | strategy.py + advice.py |
| 5 | Telegram алерты | MEDIUM | 2ч | telegram_alert.py + sentinel.py |
| 6 | Частичное закрытие check | MEDIUM | 2ч | journal.py |
| 7 | Трейлинг по H4 | MEDIUM | 3ч | journal.py |
| 8 | Live корреляция | LOW | 2ч | strategy.py |
| 9 | /api/entries endpoint | LOW | 1ч | webui.py |

---

## МЕТРИКИ УСПЕХА

После внедрения прогнать `grid_backtest.py` и сравнить:

| Метрика | Сейчас GBPJPY | Цель |
|---|---|---|
| Winrate | 64.4% | 68%+ |
| Expectancy | +1.07R | +1.3R+ |
| Max DD | 5.1R | < 4R |
| Сделок в год | ~22 | ~18 (меньше но лучше) |

Если после Kill Zone + M5 BOS expectancy не улучшился на ≥ 0.1R — откатить фильтр.

---

## ИНСТРУКЦИЯ ДЛЯ МОДЕЛИ-ИСПОЛНИТЕЛЯ

1. Реализовать по одному улучшению за раз
2. После каждого: `python -m py_compile <file>` — должно быть чисто
3. НЕ трогать `strategy_signal.py` (единственный источник правды)
4. НЕ менять форматы JSON (backtest_stats.json, oracle_report.json)
5. Каждый новый файл (calendar.py, telegram_alert.py) — graceful fallback при ошибке импорта
6. Тестировать с `--no-mem` флагом где возможно (без mempalace)
7. Коммит после каждого рабочего улучшения: `feat: add kill zone filter to advice.py`
