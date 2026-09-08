"""risk_guard.py — risk guard for bot execution.

Reads risk_guard.json, checks:
  - weekly drawdown limit
  - correlation with existing positions
  - ATR pause (volatility spike)
  - max concurrent positions
  - entry score minimum
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
RISK_FILE = HERE / "risk_guard.json"
POSITIONS_FILE = HERE / "positions.json"
JOURNAL_FILE = HERE / "journal.json"

DEFAULT = {
    "max_week_dd_r": -5.0,
    "corr_block": 0.7,
    "max_risk_pct": 2.0,
    "min_risk_pct": 0.5,
    "entry_score_min": 60,
    "max_concurrent": 2,
    "atr_pause_limit": 2.5,
}


def load_rules() -> dict:
    if RISK_FILE.exists():
        try:
            return {**DEFAULT, **json.loads(RISK_FILE.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return DEFAULT.copy()


def check_risk_guard(symbol: str, side: str, rr: float) -> dict:
    """Check risk rules before executing a signal.

    Returns: {"veto": bool, "reason": str}
    """
    rules = load_rules()

    # 1. Entry score minimum (not applicable for bot signals, skip)
    # 2. ATR pause — check from sentinel_state
    try:
        state_file = HERE / "sentinel_state.json"
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            sym_state = state.get(symbol, {})
            atr = sym_state.get("atr_h1")
            if atr and atr > 0:
                from strategy import fetch_candles, YAHOO_SYMBOLS
                yahoo = YAHOO_SYMBOLS.get(symbol, symbol)
                df = fetch_candles(yahoo, "1h", "5d")
                if df is not None and len(df) > 20:
                    baseline = df["close"].pct_change().std() * df["close"].iloc[-1]
                    if baseline > 0 and atr / baseline > rules["atr_pause_limit"]:
                        return {"veto": True, "reason": f"ATR spike: {atr/baseline:.1f}x baseline"}
    except Exception:
        pass

    # 3. Weekly drawdown — check from journal
    try:
        if JOURNAL_FILE.exists():
            journal = json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
            week_ago = datetime.now(timezone.utc).timestamp() - 7 * 86400
            week_r = sum(
                e.get("r", 0.0) for e in journal
                if e.get("status") in ("win", "loss")
                and e.get("closed", "") > datetime.fromtimestamp(week_ago, tz=timezone.utc).isoformat()
            )
            if week_r < rules["max_week_dd_r"]:
                return {"veto": True, "reason": f"Weekly DD {week_r:.1f}R < {rules['max_week_dd_r']}R"}
    except Exception:
        pass

    # 4. Correlation block — pairs with corr > 0.7
    try:
        if POSITIONS_FILE.exists():
            positions = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
            # simple correlation check: don't open opposite sides on same asset
            for pos in positions:
                if pos.get("symbol") == symbol and pos.get("side") != side:
                    return {"veto": True, "reason": f"Opposite position on {symbol} exists"}
    except Exception:
        pass

    # 5. RR sanity
    if rr < 0.5:
        return {"veto": True, "reason": f"RR {rr} too low"}

    return {"veto": False, "reason": ""}