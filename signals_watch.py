"""signals_watch.py — whitelist signal monitor: stats, direction, verdicts,
market consensus, mirrored into the mempalace palace.

Reads signals_cache.json (snapshots) and positions.json (current open
positions of each copied signal). Produces:
  - per-signal: direction (buy/sell/wait), risk score, lot-scaling verdict
  - consensus: overall market direction per instrument (buy/sell/wait)
  - markdown report + JSON, saved to palace wing_trading.

Usage:
  python signals_watch.py                 # report from cache
  python signals_watch.py --symbol XAUUSD # filter consensus
  python signals_watch.py --no-mem        # skip palace
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
PALACE = Path(os.environ.get("MEMPALACE_PALACE", r"C:\Users\h04x\.mempalace\palace"))
MEM_REPO = Path(r"C:\Users\h04x\Projects\h04xjrn_2.0\mempalace-memory-system\mempalace")
WING = "trading"
ROOM = "hall_advice"
DISCLAIMER = "DISCLAIMER: statistics-based framework, not a guarantee. 1-2% risk per trade, demo first."


def load_cache() -> dict:
    path = HERE / "signals_cache.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    snapshots = data.get("snapshots", [])
    return snapshots[-1] if snapshots else {"signals": [], "current_positions": []}


def load_positions() -> list:
    path = HERE / "positions.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def direction_for(signal_name: str, positions: list) -> dict:
    mine = [p for p in positions if p.get("name", "").lower() == signal_name.lower()]
    if not mine:
        return {"direction": "wait", "positions": []}
    sides = [p["side"] for p in mine]
    if all(s == "buy" for s in sides):
        direction = "buy"
    elif all(s == "sell" for s in sides):
        direction = "sell"
    else:
        direction = "mixed"
    return {"direction": direction, "positions": mine}


def risk_score(s: dict) -> tuple:
    score = 0
    reasons = []
    dd = s.get("dd_balance_pct")
    if dd is None:
        reasons.append("нет данных по просадке")
        score += 20
    elif dd <= 10:
        score += 5
        reasons.append(f"просадка {dd}% — низкая")
    elif dd <= 20:
        score += 15
        reasons.append(f"просадка {dd}% — умеренная")
    elif dd <= 35:
        score += 40
        reasons.append(f"просадка {dd}% — высокая")
    else:
        score += 70
        reasons.append(f"просадка {dd}% — критическая для разгона")
    wr = s.get("winrate_pct")
    if wr is None:
        reasons.append("нет данных по winrate")
    elif wr >= 70:
        score -= 10
        reasons.append(f"winrate {wr}% — высокий")
    elif wr >= 50:
        reasons.append(f"winrate {wr}% — средний")
    else:
        score += 20
        reasons.append(f"winrate {wr}% — низкий")
    if s.get("status") == "watch":
        score += 60
        reasons.append("в списке наблюдения (был стоп-аут)")
    if s.get("dd_equity_pct") and s["dd_equity_pct"] > 40:
        score += 30
        reasons.append(f"просадка по эквити {s['dd_equity_pct']}% — риск маржин-колла")
    if "grid" in (s.get("notes") or "").lower() or "margin call" in (s.get("notes") or "").lower():
        score += 25
        reasons.append("грид/маржин-колл история")
    return max(0, min(100, score)), reasons


def verdict(score: int) -> tuple:
    if score <= 25:
        return "РАЗГОН ОК", "можно разгонять лотность, риск-лимиты держать"
    if score <= 50:
        return "ОГРАНИЧЕННО", "разгонять с урезанным лотом (x0.3-0.5), стоп строго"
    return "НЕ РАЗГОНЯТЬ", "разгон запрещён: риск стоп-аута/маржин-колла высок"


def my_analysis(s: dict, direction: str, score: int, reasons: list) -> str:
    v, vwhy = verdict(score)
    line = f"{s['name']}: {direction.upper()} | риск-скор {score}/100 — {v} ({vwhy}). "
    if s.get("growth_pct") is not None:
        line += f"Рост {s['growth_pct']}%, "
    if s.get("winrate_pct") is not None:
        line += f"winrate {s['winrate_pct']}%, "
    if s.get("dd_balance_pct") is not None:
        line += f"просадка {s['dd_balance_pct']}%, "
    if s.get("trades"):
        line += f"сделок {s['trades']}, "
    if s.get("notes"):
        line += f"Примечание: {s['notes']} "
    line += "Почему: " + "; ".join(reasons) + "."
    return line


def consensus(signals: list, positions: list, target: str) -> dict:
    votes = []
    for s in signals:
        d = direction_for(s["name"], positions)
        if d["direction"] not in ("buy", "sell"):
            continue
        pos_symbols = {p["symbol"] for p in d["positions"]}
        if target and target.upper() not in {x.upper() for x in pos_symbols}:
            continue
        votes.append((s["name"], d["direction"]))
    if not votes:
        return {"direction": "wait", "strength": 0.0, "votes": [], "reason": "нет открытых позиций у сигналов — данных нет, жди"}
    buys = sum(1 for _, v in votes if v == "buy")
    sells = sum(1 for _, v in votes if v == "sell")
    total = len(votes)
    net = (buys - sells) / total
    direction = "buy" if net > 0.25 else "sell" if net < -0.25 else "wait"
    strength = abs(net)
    return {
        "direction": direction,
        "strength": round(strength * 100),
        "votes": votes,
        "buys": buys,
        "sells": sells,
        "total": total,
        "reason": f"сигналы с позициями: {total} (buy {buys} / sell {sells})",
    }


def mem_write(text: str, room: str, source: str) -> bool:
    try:
        if not PALACE.exists():
            return False
        sys.path.insert(0, str(MEM_REPO))
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


def render(snapshot: dict, positions: list, target: str, use_mem: bool) -> str:
    signals = snapshot["signals"]
    ts = snapshot.get("fetched", "?")
    lines = [f"# SIGNALS WATCH — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    lines.append(f"Снапшот данных: {ts} | источник: {snapshot.get('source', '?')}")
    lines.append(f"Позиций в positions.json: {len(positions)}", )
    lines.append("")
    lines.append("## По сигналам")
    for s in signals:
        d = direction_for(s["name"], positions)
        score, reasons = risk_score(s)
        analysis = my_analysis(s, d["direction"], score, reasons)
        lines.append(f"- {analysis}")
    lines.append("")
    cons = consensus(signals, positions, target)
    lines.append(f"## КОНСЕНСУС{' ' + target.upper() if target else ''}")
    if cons["direction"] == "wait":
        lines.append(f"- **WAIT** — {cons['reason']}")
    else:
        lines.append(f"- **{cons['direction'].upper()}** — сила {cons['strength']}% | {cons['reason']}")
        for name, vote in cons["votes"]:
            lines.append(f"  - {name}: {vote.upper()}")
    lines.append("")
    lines.append(DISCLAIMER)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Whitelist signals monitor + consensus")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--no-mem", action="store_true")
    args = parser.parse_args()

    snapshot = load_cache()
    positions = load_positions()
    report = render(snapshot, positions, args.symbol, not args.no_mem)
    print(report)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    out = HERE / "signals_report.md"
    out.write_text(report, encoding="utf-8")
    json_path = HERE / "signals_report.json"
    json_path.write_text(json.dumps({
        "generated": ts,
        "snapshot": snapshot.get("fetched"),
        "report": report,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.no_mem:
        ok = mem_write(report, ROOM, f"signals_watch_{ts}")
        print(f"[mem] saved to palace: {'yes' if ok else 'no'}")
    print("Saved:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())