"""advice.py — the trading monster: analysis + stats + memory + advice.

Usage:
  python advice.py                 # full scan, all symbols
  python advice.py --symbol XAUUSD
  python advice.py --no-mem        # skip mempalace lookups/writes
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from strategy import SYMBOL_PARAMS, YAHOO_SYMBOLS, analyze_all, correlation_matrix
from strategy import asian_range, in_kill_zone, m5_break_of_structure
from trade_signal import fetch_news, news_for_symbol, news_sentiment
from signals_watch import consensus as signals_consensus
from signals_watch import load_cache as signals_load_cache
from signals_watch import load_positions as signals_load_positions

try:
    from econ_calendar import check_news_veto
except Exception:
    check_news_veto = None

HERE = Path(__file__).parent
PALACE = Path(os.environ.get("MEMPALACE_PALACE", r"C:\Users\h04x\.mempalace\palace"))
MEM_REPO = Path(r"C:\Users\h04x\Projects\h04xjrn_2.0\mempalace-memory-system\mempalace")
WING = "trading"
ROOM = "hall_advice"
ADVICE_DIR = HERE / "advices"
DISCLAIMER = (
    "DISCLAIMER: advice is a probability framework, not a guarantee. "
    "Risk 1-2% per trade, demo first, adapt or stop."
)


def mem_available() -> bool:
    try:
        if not PALACE.exists():
            return False
        sys.path.insert(0, str(MEM_REPO))
        import mempalace  # noqa: F401
        return True
    except Exception:
        return False


def mem_search(query: str, n: int = 4) -> list:
    try:
        if not mem_available():
            return []
        import mempalace.searcher as ms
        return ms.search(query, str(PALACE), wing=WING, n_results=n)
    except Exception:
        return []


def mem_write(text: str, room: str, source: str) -> bool:
    try:
        if not mem_available():
            return False
        import mempalace.palace as mp
        from mempalace.miner import add_drawer
        collection = mp.get_collection(str(PALACE))
        drawer_id = f"drawer_{WING}_{room}_{hashlib.sha256((source + text).encode()).hexdigest()[:24]}"
        metadata = {
            "wing": WING,
            "room": room,
            "source_file": source,
            "chunk_index": 0,
            "added_by": "trade_monster",
            "filed_at": datetime.now().isoformat(),
        }
        collection.upsert(documents=[text], ids=[drawer_id], metadatas=[metadata])
        return True
    except Exception:
        return False


def load_backtest() -> dict:
    path = HERE / "backtest_stats.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_journal_stats() -> dict:
    path = HERE / "journal.json"
    if not path.exists():
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    closed = [e for e in entries if e["status"] in ("win", "loss")]
    if not closed:
        return {"closed": 0}
    wins = sum(1 for e in closed if e["status"] == "win")
    r_total = sum(e.get("r", 0.0) for e in closed)
    return {
        "closed": len(closed),
        "wins": wins,
        "winrate": round(wins / len(closed) * 100, 1),
        "total_r": round(r_total, 2),
        "expectancy": round(r_total / len(closed), 3),
    }


def confidence(bt: dict, jr: dict) -> tuple:
    score = 50.0
    reasons = []
    if bt and bt.get("trades_closed", 0) >= 10:
        wr, exp = bt.get("winrate", 0.0), bt.get("expectancy_r", 0.0)
        if wr >= 45 and exp > 0.2:
            score += 20
            reasons.append(f"backtest: {bt['trades_closed']} сделок, winrate {wr}%, ожидание {exp:+.2f}R")
        elif wr < 40:
            score -= 15
            reasons.append(f"backtest слабый: winrate {wr}%, ожидание {exp:+.2f}R")
    else:
        score -= 10
        reasons.append("бэктеста мало/нет — гоняй backtest.py")
    if jr.get("closed", 0) >= 3:
        if jr.get("winrate", 0) >= 50 and jr.get("total_r", 0) > 0:
            score += 10
            reasons.append(f"реальные сделки: {jr['closed']}, winrate {jr['winrate']}%, {jr['total_r']:+.1f}R")
        elif jr.get("total_r", 0) < 0:
            score -= 15
            reasons.append(f"реальные сделки в минусе: {jr['total_r']:+.1f}R — ревизия")
    return max(0, min(95, score)), reasons


def entry_score(a: dict, bt: dict, jr: dict, news_sent: str, zone_dist_pct: float) -> tuple:
    score, reasons = 0, []
    if zone_dist_pct is not None and zone_dist_pct <= 0.5:
        score += 30
        reasons.append(f"цена в зоне входа (дист {zone_dist_pct}%)")
    elif zone_dist_pct is not None and zone_dist_pct <= 2:
        score += 10
        reasons.append(f"цена близко к зоне ({zone_dist_pct}%)")
    else:
        reasons.append(f"цена далеко от зоны ({zone_dist_pct}%)")
    if a.get("confluence"):
        score += 20
        reasons.append("confluence H1/H4")
    trends = [a.get("trend_d1"), a.get("trend_h4"), a.get("trend_h1")]
    aligned = len([t for t in trends if t == "bullish"]) == 3 or len([t for t in trends if t == "bearish"]) == 3
    if aligned:
        score += 20
        reasons.append("тренды D1+H4+H1 согласованы")
    elif len([t for t in trends if t == "bullish"]) >= 2 or len([t for t in trends if t == "bearish"]) >= 2:
        score += 10
        reasons.append("большинство ТФ в одну сторону")
    else:
        reasons.append("разнобой трендов по ТФ")
    if bt and bt.get("trades_closed", 0) >= 10:
        if bt.get("expectancy_r", 0) > 0:
            score += 10
            reasons.append(f"бэктест +{bt['expectancy_r']}R ожидание")
        else:
            score -= 15
            reasons.append(f"бэктест минус ({bt['expectancy_r']}R)")
    if jr and jr.get("closed", 0) >= 3:
        if jr.get("total_r", 0) > 0:
            score += 10
            reasons.append(f"журнал {jr['total_r']:+.1f}R")
        else:
            score -= 15
            reasons.append(f"журнал в минусе {jr['total_r']:+.1f}R")
    if "про-риск" in news_sent:
        score += 10
        reasons.append("новости про-риск")
    elif "анти-риск" in news_sent:
        score -= 10
        reasons.append("новости анти-риск")
    tl_break = a.get("tline_break")
    if tl_break:
        if (a.get("signal") == "buy" and tl_break == "buy") or (a.get("signal") == "sell" and tl_break == "sell"):
            score += 15
            reasons.append(f"пробой трендовой линии подтверждает вход ({tl_break})")
        elif tl_break in ("buy", "sell"):
            score += 8
            reasons.append(f"пробой трендовой линии {tl_break.upper()} — контекст")
    score = max(0, min(100, score))
    label = "СИЛЬНЫЙ ВХОД" if score >= 70 else "УМЕРЕННЫЙ ВХОД" if score >= 40 else "ЖДАТЬ"
    return score, label, reasons


def lot_size(balance: float, risk_pct: float, sl_dist: float, point_value_per_lot: float) -> float:
    risk_money = balance * risk_pct / 100
    if sl_dist <= 0 or point_value_per_lot <= 0:
        return 0.0
    return round(risk_money / (sl_dist * point_value_per_lot), 2)


def atr_risk_factor(sym: str, atr_now: float) -> tuple:
    """Риск обратно-пропорционален волатильности против базлайна (EWMA)."""
    path = HERE / "atr_baseline.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    base = data.get(sym)
    if base is None or base <= 0:
        data[sym] = atr_now
        factor = 1.0
    else:
        data[sym] = round(base * 0.9 + atr_now * 0.1, 6)
        factor = min(2.0, max(0.5, base / max(atr_now, 1e-9)))
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return factor, base


def session_label(now_utc=None) -> str:
    hour = (now_utc or datetime.now(timezone.utc)).hour
    if 0 <= hour < 7:
        return "Азия (Токио) — низкая волатильность"
    if 7 <= hour < 13:
        return "Лондон — высокая волатильность"
    if 13 <= hour < 21:
        return "Нью-Йорк (13-16 — лондонский оверлап)"
    return "Тихоокеанский переход"


def load_risk_guard() -> dict:
    path = HERE / "risk_guard.json"
    defaults = {"max_week_dd_r": -5.0, "corr_block": 0.7, "consensus_boost": 1.5,
                "max_risk_pct": 2.0, "min_risk_pct": 0.5, "entry_score_min": 60,
                "max_concurrent": 2, "atr_pause_limit": 2.5}
    try:
        if path.exists():
            defaults.update(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        pass
    return defaults


def risk_guard(a: dict, journal: list, corr_pairs: dict, cons: dict,
               cfg: dict = None) -> str:
    """Жёсткие правила вето. Возвращает причину блокировки или None.
    Вето сильнее любого скора: вход запрещён. Лимиты — risk_guard.json."""
    cfg = cfg or load_risk_guard()
    sym = a["symbol"]
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    closed_week = [e for e in journal if e.get("status") in ("win", "loss")
                   and e.get("closed", "") >= week_ago.isoformat()]
    week_r = sum(e.get("r", 0.0) for e in closed_week)
    if week_r <= cfg["max_week_dd_r"]:
        return (f"СТОП-ТОРГОВЛЯ: недельный DD {week_r:.1f}R за 7 дней "
                f"(лимит {cfg['max_week_dd_r']}R). Не открывать до понедельника.")
    if a.get("atr_h1"):
        factor, base = atr_risk_factor(sym, a["atr_h1"])
        if base and factor >= cfg["atr_pause_limit"]:
            return (f"ВЕТО: волатильность x{factor:.2f} базлайна "
                    f"(лимит x{cfg['atr_pause_limit']}) — рынок в разгоне, "
                    f"вход запрещён, ждать нормализации.")
    open_pos = [e for e in journal if e.get("status") == "open"]
    if len(open_pos) >= cfg["max_concurrent"]:
        return (f"ВЕТО: уже {len(open_pos)} открытых позиций "
                f"(лимит {cfg['max_concurrent']}) — не открывать новые, "
                f"пока не закроется хотя бы одна.")
    for p in open_pos:
        p_sym = p.get("symbol")
        if p_sym == sym:
            continue
        c = corr_pairs.get(f"{p_sym}/{sym}") or corr_pairs.get(f"{sym}/{p_sym}")
        if c is not None and abs(c) > cfg["corr_block"]:
            return (f"ВЕТО: открыта позиция {p_sym} (корр {c:.2f} > "
                    f"{cfg['corr_block']}) — не открывать {sym} одновременно "
                    f"(одна ставка на доллар).")
    if cons["direction"] in ("buy", "sell") and a["signal"] != "neutral" \
            and cons["direction"] != a["signal"]:
        return (f"ВЕТО: консенсус сигналов-копирования {cons['direction'].upper()} "
                f"против нашего {a['signal'].upper()} — противоречие, вход запрещён.")
    return None


def build_advice(a: dict, bt_map: dict, jr_map: dict, news: list, use_mem: bool, balance: float = 1000.0,
                 journal: list = None, corr_pairs: dict = None) -> str:
    sym = a["symbol"]
    bt = next((s for s in bt_map.get("symbols", []) if s.get("symbol") == sym), None)
    jr = jr_map.get(sym, {})
    score, reasons = confidence(bt, jr)
    conf = ("ВЫСОКАЯ" if score >= 65 else "СРЕДНЯЯ" if score >= 45 else "НИЗКАЯ")
    sent = news_sentiment(news, sym)
    zones = []
    for key, side in (("liquidity_pool_below", "buy"), ("liquidity_pool_above", "sell"),
                      ("h4_support", "buy"), ("h4_resistance", "sell")):
        lvl = a.get(key)
        if lvl:
            zones.append((key, side, abs(a["last_price"] - lvl) / a["last_price"] * 100))
    zones.sort(key=lambda z: z[2])
    zone_dist = zones[0][2] if zones else None
    escore, elabel, ereasons = entry_score(a, bt, jr, sent, zone_dist)

    cons = signals_consensus(signals_load_cache().get("signals", []), signals_load_positions(), sym)
    rg_cfg = load_risk_guard()
    veto = risk_guard(a, journal or [], corr_pairs or {}, cons, rg_cfg)

    # ── Kill Zone: вне окон — штраф, в окне — бонус (план, улучшение 1) ──
    is_crypto = bool((a.get("params") or {}).get("crypto"))
    in_kz, kz_name = in_kill_zone()
    if not in_kz and not is_crypto:
        escore = max(0, escore - 20)
        ereasons.insert(0, f"ВНЕ KILL ZONE (час UTC: {datetime.now(timezone.utc).hour}ч) — вероятность фейка выше")
    elif is_crypto:
        ereasons.insert(0, "Крипта 24/7 — kill zone не применима (сессий нет)")
    else:
        ereasons.insert(0, f"KILL ZONE активна: {kz_name} — оптимальное время")

    # ── Экономический календарь: вето за 30 мин до high-impact (улучшение 3) ──
    calendar_veto = None
    if check_news_veto is not None:
        try:
            nv = check_news_veto(sym)
            if nv["veto"]:
                calendar_veto = nv["reason"]
        except Exception:
            pass
    if calendar_veto and not veto:
        veto = f"НОВОСТНОЕ ВЕТО: {calendar_veto}"

    signal = a["signal"]

    # ── Asian Range Bias: совпадение/противоречие (улучшение 4) ──
    ar = asian_range(YAHOO_SYMBOLS[sym])
    if is_crypto:
        ar = {"bias": "neutral"}
    if ar.get("bias") != "neutral":
        if ar["bias"] == signal:
            escore = min(100, escore + 10)
            ereasons.append(f"Asian Range байас совпадает: пробит {'HIGH' if ar['bias'] == 'sell' else 'LOW'} "
                            f"({ar['broken_level']:.3f}) → байас {ar['bias'].upper()}")
        elif signal not in ("neutral", "disabled"):
            escore = max(0, escore - 10)
            ereasons.append(f"Asian Range байас ПРОТИВ сигнала: пробит уровень {ar['broken_level']:.3f} "
                            f"указывает на {ar['bias'].upper()}, осторожно")

    # ── M5 Break of Structure: подтверждение входа (улучшение 2) ──
    bos = None
    if signal in ("buy", "sell") and not veto:
        zone_price = a.get("liquidity_pool_below") if signal == "buy" else a.get("liquidity_pool_above")
        bos = m5_break_of_structure(YAHOO_SYMBOLS[sym], signal, zone_price=zone_price)
        if bos.get("confirmed"):
            escore = min(100, escore + 15)
            ereasons.insert(0, f"M5 BOS подтверждён ({bos['detail']}): вход {bos['entry_price']}, "
                               f"SL {bos['sl_price']}, R/R ~{bos['r_ratio']}")
            a["m5_entry"] = bos["entry_price"]
            a["m5_sl"] = bos["sl_price"]
        else:
            ereasons.append(f"M5 BOS: нет подтверждения ({bos.get('detail', 'ждать пробоя структуры')})")

    signal = a["signal"]
    if signal == "disabled":
        escore, elabel, ereasons = 0, "ИНСТРУМЕНТ ОТКЛЮЧЁН", ["решетка параметров показала переобучение — EURUSD не торгуем"]
        verdict = "НЕ ТОРГОВАТЬ: инструмент отключён ресерчем (OOS-проверка дала минус). Сигналы игнорировать."
    elif signal == "buy":
        verdict = f"ДЕЙСТВИЕ: лонг от {a['liquidity_pool_below']} (зона buy-стопов), "
        verdict += f"SL {a['liquidity_pool_below']*0.995:.3f}, TP {a['nearest_resistance']}"
        if bos and bos.get("confirmed"):
            verdict += f" | M5 BOS подтверждён: вход {a['m5_entry']}, SL {a['m5_sl']}"
    elif signal == "sell":
        verdict = f"ДЕЙСТВИЕ: шорт от {a['liquidity_pool_above']} (зона sell-стопов), "
        verdict += f"SL {a['liquidity_pool_above']*1.005:.3f}, TP {a['nearest_support']}"
        if bos and bos.get("confirmed"):
            verdict += f" | M5 BOS подтверждён: вход {a['m5_entry']}, SL {a['m5_sl']}"
    elif signal == "wait":
        verdict = f"ОЖИДАНИЕ: цена у {a['liquidity_pool_below'] or a['liquidity_pool_above']} "
        verdict += "без подтверждения тренда H4 — не входить, следить за зоной"
    elif zones and zones[0][2] <= 0.5:
        zkey, zside, zdist = zones[0]
        verdict = (f"ЦЕНА В ЗОНЕ {zkey.upper()} = {a[zkey]} (дист {zdist:.2f}%, "
                   f"{'покупка' if zside == 'buy' else 'продажа'}). Не открывать без подтверждения — "
                   f"ждать разворот/пробой по структуре, sentinel даст алерт")
    else:
        verdict = "НЕТ ВХОДА: цена в середине диапазона, далеко от зон ликвидности. "
        verdict += f"Ждать подход к {a['liquidity_pool_below']} (лонг) или {a['liquidity_pool_above']} (шорт)"
    if veto:
        verdict = f"ВЕТО: {veto}"
        if signal not in ("neutral", "disabled"):
            signal_orig = signal
            signal = "neutral"
    elif signal in ("buy", "sell") and escore < rg_cfg["entry_score_min"]:
        verdict = f"НЕ ВХОДИТЬ: сигнал {signal.upper()}, но скор {escore} < {rg_cfg['entry_score_min']}. "
        verdict += "Снайпер ждёт идеальный выстрел — пропуск сделки лучше убытка."

    lines = [f"# ADVICE {sym} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    lines += [
        f"- Цена: {a['last_price']} | Тренды D1 {a['trend_d1']} / H4 {a['trend_h4']} / H1 {a['trend_h1']} / W1 {a.get('trend_w1', 'n/a')}, confluence={a['confluence']}",
        f"- Сигнал: {signal.upper()}",
        f"- **ВХОД-СКОР: {escore}/100 — {elabel}**",
        f"- Уверенность: {conf} ({score}/100)",
        f"- {verdict}",
    ]
    lines.append(f"- Kill Zone: {kz_name.upper() if in_kz else ('n/a (крипта 24/7)' if is_crypto else 'ВНЕ KILL ZONE')}")
    if ar.get("high") and ar.get("low"):
        lines.append(f"- Asian Range: {ar['low']:.3f}–{ar['high']:.3f} ({ar['range_pct']}%) | байас: {ar['bias'].upper()}")
    if calendar_veto:
        lines.append(f"- Календарь: ВЕТО — {calendar_veto}")
    if zones:
        lines.append("- Ближайшие зоны:")
        for key, side, dist in zones[:3]:
            lines.append(f"  - {key} = {a[key]} ({'покупка' if side == 'buy' else 'продажа'}-зона, дист {dist:.2f}%)")
    if a.get("atr_h1"):
        lines.append(f"- Волатильность H1 ATR: {a['atr_h1']} (для расчёта SL под разгон)")
    if a.get("rsi_h1") is not None:
        rsi_val = a["rsi_h1"]
        mr = "перекупленность (контекст шорта/фиксации)" if rsi_val >= 70 else \
             "перепроданность (контекст лонга/отскока)" if rsi_val <= 30 else \
             "нейтрально"
        eq_dist = abs(a["last_price"] - a["equilibrium"]) / a["last_price"] * 100 if a.get("equilibrium") else None
        mr_line = f"- Mean-reversion: RSI H1 = {rsi_val} ({mr})"
        if eq_dist is not None:
            mr_line += f" | равновесие D1 {a['equilibrium']} (дист {eq_dist:.2f}%)"
        lines.append(mr_line)
    if a.get("fib"):
        fib_line = ", ".join(f"{k}: {v}" for k, v in a["fib"].items())
        swing = a.get("fib_swing", {})
        lines.append(f"- Фибо (свинг {swing.get('high')}/{swing.get('low')}): {fib_line}")
    tl_parts = []
    if a.get("tline_support"):
        tl_parts.append(f"поддержка {a['tline_support']}")
    if a.get("tline_resistance"):
        tl_parts.append(f"сопротивление {a['tline_resistance']}")
    if a.get("tline_break"):
        tl_parts.append(f"ПРОБОЙ {a['tline_break'].upper()} на {a['tline_break_value']}")
    if tl_parts:
        lines.append(f"- Трендовые линии (H4): {', '.join(tl_parts)}")
    if a.get("mtf_levels"):
        lines.append("- Уровни по таймфреймам (ближайшие к цене):")
        label = {"intraday": "интрадей (M5+H1)", "m15": "M15", "h1": "H1",
                 "h4": "H4", "d1": "D1", "global": "глобально (W1)"}
        for group, lvls in a["mtf_levels"].items():
            if not lvls:
                continue
            dists = [f"{lvl} ({abs(lvl - a['last_price']) / a['last_price'] * 100:.2f}%)" for lvl in lvls[:3]]
            lines.append(f"  - {label.get(group, group)}: {', '.join(dists)}")
    lines.append(f"- Сессия: {session_label()}")
    if a.get("params"):
        p = a["params"]
        lines.append(f"- Индивидуальная конфигурация: зона входа {p['zone']:.1%} | SL {1 - p['sl_buf']:.1%} "
                     f"| TP {p['tp_buf'] - 1:.1%} | удержание {p['hold']} дней | фильтр тренда {p['filter'].upper()}")
    lines.append("- Факторы скора:")
    for r in ereasons:
        lines.append(f"  - {r}")
    risk_pct, risk_note = 1.0, None
    if a.get("atr_h1"):
        factor, base = atr_risk_factor(sym, a["atr_h1"])
        if base:
            risk_pct = min(rg_cfg["max_risk_pct"], max(rg_cfg["min_risk_pct"], risk_pct * factor))
            risk_note = f"волатильность {'выше' if factor < 1 else 'ниже'} базлайна (x{factor:.2f})"
    if cons["direction"] != "wait":
        lines.append(f"- Сигналы-копирование ({sym}): консенсус **{cons['direction'].upper()}** "
                     f"(сила {cons['strength']}%, {cons['reason']})")
        if cons["direction"] == "buy" and signal == "buy":
            risk_pct = min(rg_cfg["max_risk_pct"], risk_pct * rg_cfg["consensus_boost"])
            risk_note = "консенсус сигналов поддерживает вход"
        elif cons["direction"] in ("buy", "sell") and signal != cons["direction"]:
            lines.append(f"- ВНИМАНИЕ: консенсус сигналов ({cons['direction']}) против нашего сигнала ({signal})")
    sl_dist = 0.0
    if signal == "buy" and a.get("liquidity_pool_below"):
        sl_dist = abs(a["last_price"] - a["liquidity_pool_below"])
    elif signal == "sell" and a.get("liquidity_pool_above"):
        sl_dist = abs(a["liquidity_pool_above"] - a["last_price"])
    if sl_dist > 0:
        lines.append(f"- Лот при балансе {balance:.0f}$ и риске {risk_pct:.1f}%: "
                     f"~{lot_size(balance, risk_pct, sl_dist, 10.0)} "
                     f"(оценка, проверь спецификацию у брокера)"
                     + (f" | риск {risk_note}" if risk_note else ""))
    lines.append("- Структура:")
    lines.append(f"  - Ликвидность: выше {a['liquidity_pool_above']} (sell-стопы), ниже {a['liquidity_pool_below']} (buy-стопы)")
    lines.append(f"  - Равновесие D1: {a['equilibrium']} | H4: поддержка {a['h4_support']}, сопротивление {a['h4_resistance']}")
    lines.append(f"  - H1 EMA200: {a['h1_ema200']} | H4 EMA50: {a['h4_ema50']}")
    if reasons:
        lines.append("- База уверенности:")
        for r in reasons:
            lines.append(f"  - {r}")
    if jr.get("closed"):
        lines.append(f"- Журнал {sym}: {jr['closed']} сделок, winrate {jr['winrate']}%, {jr['total_r']:+.1f}R")
    else:
        lines.append(f"- Журнал {sym}: пуст — записывай сделки (journal.py add)")
    sym_news = news_for_symbol(news, sym)
    lines.append(f"- Сентимент новостей: {sent}")
    if sym_news:
        lines.append("- Новости:")
        for n in sym_news:
            lines.append(f"  - [{n['feed']}] {n['title']}")
    cons = signals_consensus(signals_load_cache().get("signals", []), signals_load_positions(), sym)
    lines.append("- Дисциплина:")
    lines.append("  - СЕЙФ: при +15-20 пунктов закрыть 50%, SL в безубыток для остатка")
    lines.append("  - Правило дня: не открывать новые сделки после +40 пунктов прибыли")
    if use_mem:
        past = mem_search(f"{sym} advice", 3)
        if past:
            lines.append("- Память (прошлые советы/сделки):")
            for item in past:
                doc = getattr(item, "document", None) or item.get("document", "")
                lines.append(f"  - {str(doc)[:200]}")
    lines += ["", DISCLAIMER, ""]
    return "\n".join(lines)


def dump_state(analysis: list) -> bool:
    """Full state snapshot -> palace wing 'trading' (hall_events)."""
    try:
        if not PALACE.exists():
            return False
        sys.path.insert(0, str(MEM_REPO))
        import mempalace.palace as mp
        from mempalace.miner import add_drawer
        collection = mp.get_collection(str(PALACE))
        now = datetime.now(timezone.utc).isoformat()
        data = {
            "generated": now,
            "analysis": analysis,
            "signals_cache": json.loads((HERE / "signals_cache.json").read_text(encoding="utf-8")),
            "backtest": json.loads((HERE / "backtest_stats.json").read_text(encoding="utf-8")) if (HERE / "backtest_stats.json").exists() else {},
            "journal": json.loads((HERE / "journal.json").read_text(encoding="utf-8")) if (HERE / "journal.json").exists() else [],
            "positions": json.loads((HERE / "positions.json").read_text(encoding="utf-8")) if (HERE / "positions.json").exists() else [],
        }
        text = json.dumps(data, ensure_ascii=False, indent=2)
        drawer_id = f"drawer_trading_state_{hashlib.sha256(now.encode()).hexdigest()[:16]}"
        collection.upsert(
            documents=[text],
            ids=[drawer_id],
            metadatas=[{
                "wing": "trading",
                "room": "hall_events",
                "source_file": "full_state_dump",
                "chunk_index": 0,
                "added_by": "trade_monster",
                "filed_at": now,
            }],
        )
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Trading monster: analysis + stats + memory + advice")
    parser.add_argument("--symbol", default="ALL")
    parser.add_argument("--no-mem", action="store_true")
    parser.add_argument("--balance", type=float, default=1000.0, help="account balance for lot sizing")
    args = parser.parse_args()

    analysis, errors = analyze_all(args.symbol)
    if not analysis:
        print("No analysis produced:", "; ".join(errors) or "unknown")
        return 1

    bt_map = load_backtest()
    journal = json.loads((HERE / "journal.json").read_text(encoding="utf-8")) if (HERE / "journal.json").exists() else []
    jr_map = {}
    for e in journal:
        if e["status"] in ("win", "loss"):
            jr_map.setdefault(e["symbol"], []).append(e)
    jr_stats = {
        sym: {
            "closed": len(lst),
            "wins": sum(1 for x in lst if x["status"] == "win"),
            "winrate": round(sum(1 for x in lst if x["status"] == "win") / len(lst) * 100, 1),
            "total_r": round(sum(x.get("r", 0.0) for x in lst), 2),
        }
        for sym, lst in jr_map.items()
    }

    news = fetch_news()
    corr = correlation_matrix()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    ADVICE_DIR.mkdir(exist_ok=True)
    for a in analysis:
        if a["signal"] == "disabled":
            print(f"SKIP {a['symbol']}: инструмент отключён — совет не пишем")
            continue
        use_mem = not args.no_mem
        report = build_advice(a, bt_map, jr_stats, news, use_mem, args.balance,
                              journal=journal, corr_pairs=corr)
        linked = [pair for pair, c in corr.items() if a["symbol"] in pair.split("/") and abs(c) >= 0.6]
        if linked:
            report = report.replace(
                "- Дисциплина:",
                f"- Корреляция: {', '.join(f'{p} {c:.2f}' for p, c in corr.items() if a['symbol'] in p.split('/'))} "
                f"— не открывай связанные инструменты одновременно\n- Дисциплина:",
            )
        print(report)
        path = ADVICE_DIR / f"advice_{a['symbol']}_{ts}.md"
        try:
            from chart import render as chart_render
            chart_path = chart_render(a["symbol"])
            if chart_path:
                report = report.replace(
                    DISCLAIMER,
                    f"[Скрин с уровнями: {chart_path}]\n\n{DISCLAIMER}",
                )
        except Exception:
            pass
        path.write_text(report, encoding="utf-8")
        if use_mem:
            ok = mem_write(report, ROOM, f"advice_{a['symbol']}_{ts}")
            print(f"[mem] saved to palace: {'yes' if ok else 'no'}")
        print("Saved:", path)

    snapshots_dir = HERE / "snapshots"
    snapshots_dir.mkdir(exist_ok=True)
    snapshot = {
        "generated": ts,
        "data_as_of": datetime.now(timezone.utc).isoformat(),
        "analysis": [
            {
                "symbol": a["symbol"],
                "last_price": a["last_price"],
                "signal": a["signal"],
                "trend_h1": a["trend_h1"],
                "trend_h4": a["trend_h4"],
                "confluence": a["confluence"],
                "liquidity_pool_below": a["liquidity_pool_below"],
                "liquidity_pool_above": a["liquidity_pool_above"],
                "nearest_support": a["nearest_support"],
                "nearest_resistance": a["nearest_resistance"],
                "equilibrium": a["equilibrium"],
                "h4_support": a["h4_support"],
                "h4_resistance": a["h4_resistance"],
            }
            for a in analysis
        ],
    }
    (snapshots_dir / f"market_{ts}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_market_now(analysis, snapshot)
    if not args.no_mem:
        ok = dump_state(analysis)
        print(f"[mem] full state dump to palace: {'yes' if ok else 'no'}")
    if errors:
        print("ERRORS:", "; ".join(errors))
    return 0


def write_market_now(analysis: list, snapshot: dict) -> None:
    lines = [
        "# MARKET NOW — единый сводный отчёт",
        "",
        f"Сгенерирован: {snapshot['generated']} UTC | данные: {snapshot['data_as_of']}",
        "",
        "## ВЕРДИКТ: куда открываться прямо сейчас",
        "",
    ]
    for a in analysis:
        sym = a["symbol"]
        if a["signal"] == "buy":
            verdict = f"ЛОНГ от {a['liquidity_pool_below']} (SL под ним, TP {a['nearest_resistance']})"
        elif a["signal"] == "sell":
            verdict = f"ШОРТ от {a['liquidity_pool_above']} (SL над ним, TP {a['nearest_support']})"
        elif a["signal"] == "wait":
            verdict = f"ЖДАТЬ: цена у зоны {a['liquidity_pool_below'] or a['liquidity_pool_above']}, но тренд H4 не подтверждает"
        else:
            verdict = "НЕТ ВХОДА: цена в середине диапазона. Лонг-зона {0}, шорт-зона {1}".format(
                a["liquidity_pool_below"], a["liquidity_pool_above"])
        lines.append(f"- **{sym}** ({a['last_price']}): {verdict}")
    lines.append("")
    lines.append("## Детали")
    lines.append("")
    for a in analysis:
        lines += [
            f"### {a['symbol']} — {a['last_price']}",
            f"- Сигнал: {a['signal'].upper()} | H1 {a['trend_h1']}, H4 {a['trend_h4']}, confluence={a['confluence']}",
            f"- Пул ниже (лонг-зона): {a['liquidity_pool_below']} | Пул выше (шорт-зона): {a['liquidity_pool_above']}",
            f"- Поддержка: {a['nearest_support']} | Сопротивление: {a['nearest_resistance']} | Равновесие: {a['equilibrium']}",
            f"- H4: поддержка {a['h4_support']}, сопротивление {a['h4_resistance']}",
            f"- Фибо: {a['fib']}" if a.get("fib") else "-",
            "",
        ]
    lines.append(DISCLAIMER)
    (HERE / "market_now.md").write_text("\n".join(lines), encoding="utf-8")
    print("Saved:", HERE / "market_now.md")


if __name__ == "__main__":
    sys.exit(main())
