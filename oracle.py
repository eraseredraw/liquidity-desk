"""oracle.py — проверяет прошлые советы (advices/*.md) против фактического рынка.

Для каждого совета старше 1 дня:
  - зона достигнута? (лонг-зона: low <= зона*zone_mult; шорт-зона: high >= зона/zone_mult)
  - если был сигнал ДЕЙСТВИЕ (buy/sell): симуляция входа в зоне, SL/TP из совета
  - результат кэшируется в oracle.json (повторный запуск ничего не пересчитывает)

Итог: oracle_report.md — реальный winrate зон и действий. Это оракул
для калибровки ВХОД-СКОРА и confidence в advice.py.

Usage:
  python oracle.py            # только новые (не проверенные) советы
  python oracle.py --all      # перепроверить всё
  python oracle.py --no-mem   # не писать в mempalace
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from strategy import SYMBOL_PARAMS, YAHOO_SYMBOLS, fetch_candles

HERE = Path(__file__).parent
ADVICE_DIR = HERE / "advices"
DB_PATH = HERE / "oracle.json"
REPORT_MD = HERE / "oracle_report.md"
REPORT_JSON = HERE / "oracle_report.json"
WING = "trading"
ROOM = "hall_events"
MIN_AGE_HOURS = 24
HOLD_DEFAULT = 20

FILE_RE = re.compile(r"advice_([A-Z]+)_(\d{8})_(\d{4})\.md")
PRICE_RE = re.compile(r"- Цена: ([\d.]+)")
SIGNAL_RE = re.compile(r"- Сигнал: (\w+)")
ZONE_RE = re.compile(r"- (liquidity_pool_below|liquidity_pool_above|h4_support|h4_resistance) = ([\d.]+)")
ACTION_RE = re.compile(r"ДЕЙСТВИЕ:\s*(лонг|шорт) от ([\d.]+).*?SL ([\d.]+).*?TP ([\d.]+)")


def mem_write(text: str, source: str) -> bool:
    try:
        palace = Path(r"C:\Users\h04x\.mempalace\palace")
        if not palace.exists():
            return False
        sys.path.insert(0, str(Path(r"C:\Users\h04x\Projects\h04xjrn_2.0\mempalace-memory-system\mempalace")))
        import mempalace.palace as mp
        collection = mp.get_collection(str(palace))
        drawer_id = f"drawer_{WING}_{ROOM}_{hashlib.sha256((source + text).encode()).hexdigest()[:24]}"
        collection.upsert(
            documents=[text],
            ids=[drawer_id],
            metadatas=[{
                "wing": WING,
                "room": ROOM,
                "source_file": source,
                "chunk_index": 0,
                "added_by": "oracle",
                "filed_at": datetime.now().isoformat(),
            }],
        )
        return True
    except Exception:
        return False


def parse_advice(path: Path) -> dict:
    m = FILE_RE.match(path.name)
    if not m:
        return None
    symbol, day, hhmm = m.groups()
    text = path.read_text(encoding="utf-8")
    zones = {k: float(v) for k, v in ZONE_RE.findall(text)}
    action = None
    am = ACTION_RE.search(text)
    if am:
        action = {
            "side": "buy" if am.group(1) == "лонг" else "sell",
            "entry_zone": float(am.group(2)),
            "sl": float(am.group(3)),
            "tp": float(am.group(4)),
        }
    return {
        "symbol": symbol,
        "ts": datetime(int(day[:4]), int(day[4:6]), int(day[6:8]),
                       int(hhmm[:2]), int(hhmm[2:]), tzinfo=timezone.utc),
        "price": float(PRICE_RE.search(text).group(1)) if PRICE_RE.search(text) else None,
        "signal": SIGNAL_RE.search(text).group(1) if SIGNAL_RE.search(text) else None,
        "zones": zones,
        "action": action,
    }


def evaluate(adv: dict) -> dict:
    sym = adv["symbol"]
    yahoo = YAHOO_SYMBOLS.get(sym)
    params = SYMBOL_PARAMS.get(sym)
    hold = params["hold"] if params else HOLD_DEFAULT
    zone_mult = params["zone"] if params else 1.01
    result = {"symbol": sym, "ts": adv["ts"].isoformat(), "signal": adv["signal"]}
    try:
        df = fetch_candles(yahoo, "1h", "2mo")
        df = df[df.index >= adv["ts"]]
        if len(df) < 3:
            result["error"] = "нет данных после совета"
            return result
        win = df[df.index <= adv["ts"] + timedelta(days=hold)]
        if len(win) < 1:
            result["error"] = "нет свечей в окне удержания"
            return result
        low_min = float(win["low"].min())
        high_max = float(win["high"].max())
        z = adv["zones"]
        if z.get("liquidity_pool_below"):
            result["buy_zone_level"] = z["liquidity_pool_below"]
            result["buy_zone_reached"] = low_min <= z["liquidity_pool_below"] * zone_mult
        if z.get("liquidity_pool_above"):
            result["sell_zone_level"] = z["liquidity_pool_above"]
            result["sell_zone_reached"] = high_max >= z["liquidity_pool_above"] / zone_mult
        if adv["action"]:
            act = adv["action"]
            trigger = act["entry_zone"] * zone_mult if act["side"] == "buy" else act["entry_zone"] / zone_mult
            hit = win[win["low"] <= trigger] if act["side"] == "buy" else win[win["high"] >= trigger]
            if len(hit) == 0:
                result["action_outcome"] = "zone_never_reached"
                return result
            entry = float(hit["open"].iloc[0])
            risk = abs(entry - act["sl"])
            if risk <= 0:
                result["action_outcome"] = "bad_levels"
                return result
            idx0 = win.index.get_loc(hit.index[0])
            outcome, exit_price = "flat", entry
            for j in range(idx0, min(idx0 + hold * 24, len(win))):
                high, low = float(win["high"].iloc[j]), float(win["low"].iloc[j])
                if act["side"] == "buy" and low <= act["sl"]:
                    outcome, exit_price = "loss", act["sl"]
                    break
                if act["side"] == "sell" and high >= act["sl"]:
                    outcome, exit_price = "loss", act["sl"]
                    break
                if act["side"] == "buy" and high >= act["tp"]:
                    outcome, exit_price = "win", act["tp"]
                    break
                if act["side"] == "sell" and low <= act["tp"]:
                    outcome, exit_price = "win", act["tp"]
                    break
            r = (exit_price - entry) / risk if act["side"] == "buy" else (entry - exit_price) / risk
            result["action_outcome"] = outcome
            result["action_entry"] = round(entry, 5)
            result["action_r"] = round(r, 2)
            result["action_rr"] = round(abs(act["tp"] - entry) / risk, 2)
    except Exception as exc:
        result["error"] = str(exc)
    return result


def load_db() -> dict:
    if DB_PATH.exists():
        try:
            return json.loads(DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Advice oracle: советы vs факт")
    parser.add_argument("--all", action="store_true", help="перепроверить все советы")
    parser.add_argument("--no-mem", action="store_true")
    args = parser.parse_args()

    db = load_db()
    now = datetime.now(timezone.utc)
    files = sorted(ADVICE_DIR.glob("advice_*.md"))
    new = 0
    for path in files:
        if path.name in db and not args.all:
            continue
        adv = parse_advice(path)
        if not adv:
            continue
        age = now - adv["ts"]
        if age < timedelta(hours=MIN_AGE_HOURS) and not args.all:
            continue
        db[path.name] = evaluate(adv)
        new += 1
    DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

    by_sym = {}
    for name, res in db.items():
        if "error" in res:
            continue
        by_sym.setdefault(res["symbol"], []).append(res)

    lines = [f"# ORACLE REPORT — {now.strftime('%Y-%m-%d %H:%M UTC')}", ""]
    lines.append(f"Проверено советов: {len(db)} (новых: {new}) | минимальный возраст: {MIN_AGE_HOURS}ч")
    lines.append("")
    agg = {}
    for sym in sorted(by_sym):
        lst = by_sym[sym]
        n = len(lst)
        buy_reached = sum(1 for r in lst if r.get("buy_zone_reached"))
        sell_reached = sum(1 for r in lst if r.get("sell_zone_reached"))
        zone_adv = [r for r in lst if "buy_zone_reached" in r or "sell_zone_reached" in r]
        actions = [r for r in lst if "action_outcome" in r]
        act_closed = [r for r in actions if r["action_outcome"] in ("win", "loss")]
        wins = sum(1 for r in act_closed if r["action_outcome"] == "win")
        exp = sum(r.get("action_r", 0.0) for r in act_closed) / len(act_closed) if act_closed else 0.0
        agg[sym] = {
            "advices": n,
            "buy_zone_reach": round(buy_reached / max(len([r for r in lst if "buy_zone_reached" in r]), 1) * 100, 1),
            "sell_zone_reach": round(sell_reached / max(len([r for r in lst if "sell_zone_reached" in r]), 1) * 100, 1),
            "actions": len(actions),
            "action_winrate": round(wins / len(act_closed) * 100, 1) if act_closed else None,
            "action_expectancy": round(exp, 3) if act_closed else None,
        }
        lines.append(f"## {sym} — советов {n}, зона-лонг {agg[sym]['buy_zone_reach']}%, "
                     f"зона-шорт {agg[sym]['sell_zone_reach']}%")
        if agg[sym]["actions"]:
            lines.append(f"- Действия: {agg[sym]['actions']}, winrate {agg[sym]['action_winrate']}%, "
                         f"ожидание {agg[sym]['action_expectancy']:+.3f}R")
        lines.append("")
        for r in lst[-8:]:
            ts = r["ts"][:16].replace("T", " ")
            parts = [f"  - {ts} {r['signal']}"]
            if "buy_zone_reached" in r:
                parts.append(f"buy-зона {'ДА' if r['buy_zone_reached'] else 'нет'}")
            if "sell_zone_reached" in r:
                parts.append(f"sell-зона {'ДА' if r['sell_zone_reached'] else 'нет'}")
            if "action_outcome" in r:
                parts.append(f"действие: {r['action_outcome']} {r.get('action_r', 0):+.2f}R")
            lines.append(" ".join(parts))
        lines.append("")
    lines.append("DISCLAIMER: статистика по прошлым советам, не гарантия. Использовать для калибровки скора.")
    report = "\n".join(lines)
    REPORT_MD.write_text(report, encoding="utf-8")
    REPORT_JSON.write_text(json.dumps({"generated": now.isoformat(), "by_symbol": agg, "checked": len(db)},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
    print(report)
    if not args.no_mem:
        ok = mem_write(report, f"oracle_{now.strftime('%Y%m%d_%H%M')}")
        print(f"[mem] oracle report -> palace: {'yes' if ok else 'no'}")
    print("Saved:", REPORT_MD)
    return 0


if __name__ == "__main__":
    sys.exit(main())