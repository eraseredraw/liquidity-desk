"""bot.py — main bot loop: run strategies → execute signals via broker.

Usage:
    python bot.py                    # run forever (10min interval)
    python bot.py --once             # single cycle
    python bot.py --broker dry       # force dry-run (default)
    python bot.py --broker mt5       # force MT5
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agent_loop import run_loop
from strategy import YAHOO_SYMBOLS, SYMBOL_PARAMS
from strategy_registry import list_strategies
from execution.broker import get_broker, Broker, Order
from execution.broker import DryRunBroker
from risk_guard import check_risk_guard

HERE = Path(__file__).parent
CONFIG = HERE / "sentinel_config.json"
BOT_LOG = HERE / "bot.log"

DEFAULT_CONFIG = {
    "interval_min": 10,
    "symbols": ["XAUUSD", "GBPUSD", "EURUSD", "GBPJPY", "DAX"],
    "m5": {"enabled": True, "min_rr": 1.5, "max_rr": 6.0, "min_tp_dist": 0.0005, "dedup_h": 12},
    "ny_open": {"enabled": True},
    "zone": {"enabled": True, "zone_pct": 0.5, "min_rr": 1.0, "max_rr": 6.0},
    "risk": {"max_volume": 0.1, "max_concurrent": 2, "max_daily_trades": 5},
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(BOT_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def _volume_for_symbol(sym: str, params: dict) -> float:
    from strategy import SYMBOL_PARAMS
    sp = SYMBOL_PARAMS.get(sym, {})
    return sp.get("volume", 0.01) if sp else 0.01


def execute_signal(sig: dict, sym: str, broker: Broker, cfg: dict) -> None:
    """Отправить сигнал в исполнение через RiskGuard."""
    risk_cfg = cfg.get("risk", {})
    max_concurrent = risk_cfg.get("max_concurrent", 2)

    # Check RiskGuard
    guard = check_risk_guard(sym, sig["dir"], sig.get("rr", 0))
    if guard.get("veto"):
        log(f"  [VETO] {sym} {sig['dir']}: {guard['reason']}")
        return

    # Check concurrent positions
    positions = broker.get_positions()
    if len(positions) >= max_concurrent:
        log(f"  [SKIP] {sym}: {len(positions)} concurrent positions >= {max_concurrent}")
        return

    # Check existing position on same symbol
    if any(p.symbol == sym for p in positions):
        log(f"  [SKIP] {sym}: already has open position")
        return

    volume = _volume_for_symbol(sym, cfg)

    order = Order(
        symbol=sym,
        side=sig["dir"],
        volume=volume,
        entry=sig["entry"],
        sl=sig["sl"],
        tp=sig["tp"],
        strategy=sig.get("strategy", "unknown"),
        comment=f"bot_{sig.get('strategy', 'unknown')}",
    )

    order_id = broker.place_order(order)
    if order_id:
        kz = f" [KZ: {sig.get('kz_name')}]" if sig.get("kz_in") else ""
        dir_label = "LONG" if sig["dir"] == "buy" else "SHORT"
        log(f"  [EXECUTED] {sym} {dir_label}{kz} @ {sig['entry']} "
            f"SL {sig['sl']} TP {sig['tp']} RR {sig['rr']} "
            f"order={order_id} vol={volume}")
    else:
        log(f"  [FAIL] {sym} {sig['dir']}: order rejected")


def check_and_execute(broker: Broker, cfg: dict) -> dict:
    """Main cycle: run strategies, execute signals, update dry-run prices."""
    result = {"checked": datetime.now(timezone.utc).isoformat(), "symbols": {}, "executed": []}
    symbols = cfg.get("symbols", DEFAULT_CONFIG["symbols"])

    for sym in symbols:
        yahoo = YAHOO_SYMBOLS.get(sym, sym)
        log(f"Checking {sym}...")

        loop_result = run_loop(sym, yahoo, cfg, max_steps=6, trace=False)
        signals = loop_result.get("signals", {})
        result["symbols"][sym] = {
            "strategies_run": loop_result.get("strategies_run", []),
            "num_signals": len(signals),
        }

        if not signals:
            log(f"  No signals for {sym}")
            continue

        # Update dry-run price if applicable
        if isinstance(broker, DryRunBroker):
            try:
                from strategy import fetch_candles
                df = fetch_candles(yahoo, "1h", "1d")
                if df is not None and len(df) > 0:
                    broker.update_price(sym, float(df["close"].iloc[-1]))
            except Exception:
                pass

        for sid, sig in signals.items():
            execute_signal(sig, sym, broker, cfg)
            result["executed"].append({
                "symbol": sym,
                "strategy": sid,
                "dir": sig["dir"],
                "entry": sig["entry"],
            })

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Bot — execute signals via broker")
    parser.add_argument("--once", action="store_true", help="single cycle")
    parser.add_argument("--broker", default="dry", choices=["dry", "mt5", "auto"],
                        help="broker type (default: dry)")
    parser.add_argument("--mt5-server", default="")
    parser.add_argument("--mt5-login", type=int, default=0)
    parser.add_argument("--mt5-password", default="")
    parser.add_argument("--mt5-path", default="")
    args = parser.parse_args()

    cfg = load_config()
    strats = [s["id"] for s in list_strategies()]
    log(f"Bot started. interval={cfg['interval_min']}min, broker={args.broker}, "
        f"symbols={cfg['symbols']}, strategies={strats}")

    broker = get_broker(broker_type=args.broker, server=args.mt5_server,
                        login=args.mt5_login, password=args.mt5_password,
                        path=args.mt5_path)
    if not broker.is_connected():
        log("Broker failed to connect")
        return 1

    if args.once:
        check_and_execute(broker, cfg)
        return 0

    while True:
        try:
            check_and_execute(broker, cfg)
        except Exception as exc:
            log(f"[ERROR] {exc}")
        time.sleep(cfg["interval_min"] * 60)


if __name__ == "__main__":
    sys.exit(main())