"""journal.py — trade journal with stats, mirrored into the mempalace palace.

Commands:
  python journal.py add --symbol XAUUSD --side buy --entry 4480 --sl 4400 --tp 4600 [--note "..."]
  python journal.py close --id 3 --result win --exit 4600
  python journal.py stats
"""

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
JOURNAL = HERE / "journal.json"
PALACE = Path(os.environ.get("MEMPALACE_PALACE", r"C:\Users\h04x\.mempalace\palace"))
MEM_REPO = Path(r"C:\Users\h04x\Projects\h04xjrn_2.0\mempalace-memory-system\mempalace")
WING = "trading"
ROOM = "hall_events"


def load_journal() -> list:
    if JOURNAL.exists():
        return json.loads(JOURNAL.read_text(encoding="utf-8"))
    return []


def save_journal(entries: list) -> None:
    JOURNAL.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def mem_available() -> bool:
    try:
        if not PALACE.exists():
            return False
        sys.path.insert(0, str(MEM_REPO))
        import mempalace  # noqa: F401
        return True
    except Exception:
        return False


def mem_write(text: str, room: str = ROOM, source: str = "journal") -> bool:
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


def mem_search(query: str, n: int = 5) -> list:
    try:
        if not mem_available():
            return []
        import mempalace.searcher as ms
        return ms.search(query, str(PALACE), wing=WING, n_results=n)
    except Exception:
        return []


def cmd_add(args) -> None:
    entries = load_journal()
    entry = {
        "id": max([e["id"] for e in entries], default=0) + 1,
        "opened": datetime.now(timezone.utc).isoformat(),
        "symbol": args.symbol,
        "side": args.side,
        "entry": args.entry,
        "sl": args.sl,
        "tp": args.tp,
        "note": args.note,
        "status": "open",
    }
    entries.append(entry)
    save_journal(entries)
    text = (f"TRADE OPEN {entry['symbol']} {entry['side'].upper()} "
            f"entry={entry['entry']} sl={entry['sl']} tp={entry['tp']} note={entry['note'] or ''}")
    ok = mem_write(text, source=f"journal_trade_{entry['id']}")
    print(f"Trade #{entry['id']} recorded. mempalace={'yes' if ok else 'no'}")


def cmd_close(args) -> None:
    entries = load_journal()
    entry = next((e for e in entries if e["id"] == args.id), None)
    if not entry:
        print(f"Trade #{args.id} not found")
        return 1
    if entry["status"] != "open":
        print(f"Trade #{args.id} already {entry['status']}")
        return 1
    risk = abs(entry["entry"] - entry["sl"])
    if entry["side"] == "buy":
        r = (args.exit - entry["entry"]) / risk if risk else 0.0
    else:
        r = (entry["entry"] - args.exit) / risk if risk else 0.0
    entry.update({
        "status": args.result,
        "exit": args.exit,
        "closed": datetime.now(timezone.utc).isoformat(),
        "r": round(r, 2),
        "result_note": args.note,
    })
    save_journal(entries)
    text = (f"TRADE CLOSED #{entry['id']} {entry['symbol']} {entry['side'].upper()} "
            f"result={args.result} exit={args.exit} r={r:+.2f} note={args.note or ''}")
    ok = mem_write(text, source=f"journal_close_{entry['id']}")
    print(f"Trade #{args.id} closed: {args.result}, r={r:+.2f}. mempalace={'yes' if ok else 'no'}")
    return 0


def cmd_stats(args) -> int:
    entries = [e for e in load_journal() if e["status"] in ("win", "loss")]
    if not entries:
        print("No closed trades yet.")
        return 0
    wins = [e for e in entries if e["status"] == "win"]
    losses = [e for e in entries if e["status"] == "loss"]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for e in entries:
        equity += e.get("r", 0.0)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    print(f"Closed: {len(entries)} (win {len(wins)} / loss {len(losses)})")
    print(f"Winrate: {len(wins)/len(entries)*100:.1f}%")
    print(f"Total R: {sum(e.get('r', 0) for e in entries):+.2f}")
    print(f"Expectancy: {sum(e.get('r', 0) for e in entries)/len(entries):+.3f}R")
    print(f"Max drawdown: {max_dd:.2f}R")
    if args.by_symbol:
        by = {}
        for e in entries:
            s = by.setdefault(e["symbol"], [0, 0])
            s[0] += 1
            s[1] += e.get("r", 0.0)
        for sym, (n, r) in sorted(by.items()):
            print(f"  {sym}: {n} trades, {r:+.2f}R")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Trade journal with mempalace mirror")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("--symbol", required=True)
    p_add.add_argument("--side", required=True, choices=["buy", "sell"])
    p_add.add_argument("--entry", required=True, type=float)
    p_add.add_argument("--sl", required=True, type=float)
    p_add.add_argument("--tp", required=True, type=float)
    p_add.add_argument("--note", default="")
    p_add.set_defaults(func=cmd_add)

    p_close = sub.add_parser("close")
    p_close.add_argument("--id", required=True, type=int)
    p_close.add_argument("--result", required=True, choices=["win", "loss"])
    p_close.add_argument("--exit", required=True, type=float)
    p_close.add_argument("--note", default="")
    p_close.set_defaults(func=cmd_close)

    p_stats = sub.add_parser("stats")
    p_stats.add_argument("--by-symbol", action="store_true")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
