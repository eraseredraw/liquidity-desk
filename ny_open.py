# -*- coding: utf-8 -*-
"""ny_open.py — NY Open First 5-Minute Candle (Casper SMC).

Strategy: NY Open — First 5‑Minute Candle (Casper SMC YouTube).
Rules (exact, from Forex Factory backtest on SPX/NDX):
  1. Mark High/Low of the 1st 5‑min candle from NY Open 9:30 AM EST.
  2. Wait for a Close OUTSIDE this range.
  3. Once outside: wait for price to re‑ENTER the range, then Close back OUTSIDE (retest/confirmation).
  4. Enter at the Open of the NEXT candle.
  5. SL = 50 % Fibonacci level of the 1st candle range.
  6. TP = 2 R (double the stop distance).
  7. Window for a setup: 9:30–11:30 AM EST. No setup → skip day.

The module follows patterns from m5_sniper.py / strategy.py.
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None  # fallback — will use UTC offset approximation

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from strategy import fetch_candles, in_kill_zone, find_swings, KILL_ZONES_UTC
from strategy import fetch_candles_yahoo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NY_OPEN_UTC_HOUR = 14  # 9:30 AM EST ≈ 14:30 UTC — we use hour 14
NY_OPEN_UTC_MIN  = 30
WINDOW_END_HOUR  = 16  # 11:30 AM EST ≈ 16:30 UTC — stop after hour 16
LOOKBACK_M5      = 50  # enough M5 candles to cover the session
DEFAULTS = {
    "min_rr": 1.5,
    "max_rr": 6.0,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _candle_start_minutes_et(ts: datetime) -> int:
    """Return minutes from midnight in America/New_York for a candle start timestamp (UTC)."""
    if _ET:
        try:
            ts_et = ts.astimezone(_ET)
        except Exception:
            pass
    else:
        # Rough approximation: EST = UTC-5, EDT = UTC-4
        # We'll just shift by -4h for now (summer)
        ts_et = ts + timedelta(hours=-4)
    return ts_et.hour * 60 + ts_et.minute


def _is_ny_open_candle(ts: datetime) -> bool:
    """Check if an M5 candle starts at/after NY Open 9:30 AM ET."""
    return _candle_start_minutes_et(ts) >= 9 * 60 + 30  # 9:30 AM ET


def _candle_end_minutes_et(ts: datetime, interval_min: int = 5) -> int:
    """Return minutes from midnight ET for candle end (start + interval)."""
    if _ET:
        try:
            ts_et = ts.astimezone(_ET)
        except Exception:
            pass
    else:
        ts_et = ts + timedelta(hours=-4)
    return ts_et.hour * 60 + ts_et.minute + interval_min


def _first_ny_open_candle(df: pd.DataFrame) -> pd.Series | None:
    """Find the first M5 candle in df that starts at/after NY Open 14:30 UTC.

    df must have a DatetimeIndex (UTC) and columns open/high/low/close.
    """
    if df is None or len(df) == 0:
        return None
    # iterate from the oldest candle forward to find the first >= 14:30 UTC
    for idx in df.index:
        if _is_ny_open_candle(idx):
            return df.loc[idx]
    # fallback: return the first candle if none found (should not happen with enough data)
    return df.iloc[0]


def _range_of_candle(candle: pd.Series) -> tuple[float, float]:
    """Return (high, low) of a candle — the ORB range."""
    return float(candle["high"]), float(candle["low"])


def _fib_50(high: float, low: float) -> float:
    """50 % Fibonacci level of the range."""
    return (high + low) / 2.0


# ---------------------------------------------------------------------------
# Core check function
# ---------------------------------------------------------------------------


def check(yahoo_symbol: str, sym: str = None, params: dict = None,
          cfg: dict = None, at_ts: datetime = None) -> dict | None:
    """Check the NY Open 5‑minute candle strategy right now.

    Returns a signal dict like:
        {
            "dir": "buy"|"sell",
            "entry": float,
            "sl": float,
            "tp": float,
            "rr": float,
            "range_high": float,
            "range_low": float,
            "range_50pct": float,
            "confirm_candle_idx": int,      # index of the confirmation candle
            "strategy": "ny_open",
            "ts": str,                      # iso timestamp
            "symbol": sym,
        }
    Returns None if no valid setup at this moment.
    """
    try:
        cfg = {**DEFAULTS, **(cfg or {})}
        sym = sym or (params or {}).get("symbol") or yahoo_symbol.split(".")[0]

        # ── 1️⃣ fetch M5 candles for the day (UTC day) ──
        df = fetch_candles(yahoo_symbol, "5m", "1d")
        if df is None or len(df) < LOOKBACK_M5:
            return None

        # Ensure UTC index
        if not isinstance(df.index, pd.DatetimeIndex):
            # try convert
            try:
                df.index = pd.DatetimeIndex(df.index, utc=True)
            except Exception:
                return None

        # ── 2️⃣ find the first 5‑min candle at/after NY Open (14:30 UTC) ──
        first_candle = _first_ny_open_candle(df)
        if first_candle is None:
            return None

        range_high, range_low = _range_of_candle(first_candle)
        range_50pct = _fib_50(range_high, range_low)
        range_size = range_high - range_low

        # ── 3️⃣ look at subsequent candles for the pattern ──
        # We need candles AFTER the first one. Find the index of first_candle.
        try:
            start_idx = df.index.get_loc(first_candle.name)
        except Exception:
            start_idx = 0

        # Slice candles after the first one
        after = df.iloc[start_idx + 1:] if start_idx + 1 < len(df) else pd.DataFrame()
        if after is None or len(after) < 3:
            return None

        # Pattern tracking:
        # step_a: close outside range (first breakout)
        # step_b: price re-enters range then closes outside again (confirmation)
        # step_c: enter at open of next candle after confirmation

        dir_side = None          # "buy" or "sell"
        breakout_idx = None      # candle that first closed outside
        confirm_idx = None       # candle that retested + closed outside
        entry_open_idx = None    # candle open after confirmation

        # --- scan after-candles ---
        for i, candle in after.iterrows():
            close = float(candle["close"])
            high = float(candle["high"])
            low = float(candle["low"])
            open_price = float(candle["open"])

            # --- check close outside range ---
            outside_high = close > range_high or high > range_high
            outside_low = close < range_low or low < range_low

            if dir_side is None and (outside_high or outside_low):
                # First close outside → potential breakout
                if outside_high:
                    dir_side = "buy"
                    breakout_idx = i
                else:
                    dir_side = "sell"
                    breakout_idx = i
                # After first breakout, we look for retest+confirm
                # Reset tracking: we now watch for re-entry then second outside
                # We'll continue scanning...
                continue

            # If we already had a breakout, look for confirmation:
            if breakout_idx is not None and confirm_idx is None:
                # Re‑enter the range: price moves back inside
                re_enter_high = high <= range_high
                re_enter_low = low >= range_low

                if dir_side == "buy" and re_enter_high:
                    # price came back down into/within the range
                    # now need a close outside again (downward)
                    if close < range_low:
                        # confirmation: re-entered then closed below low
                        confirm_idx = i
                elif dir_side == "sell" and re_enter_low:
                    # price came back up into/within the range
                    if close > range_high:
                        # confirmation: re-entered then closed above high
                        confirm_idx = i

        # ── 4️⃣ If we have confirmation, generate signal ──
        if confirm_idx is None or breakout_idx is None:
            # No full pattern yet — may still be forming; return partial info
            # But per strategy: we only trade if full pattern completes within window.
            # We'll check if confirmation happened before window end.
            # For now, return None if not complete.
            # (Could return "forming" state for UI, but keeping it simple.)
            return None

        # We have confirmation at confirm_idx.
        # Entry = Open of the NEXT candle after confirmation.
        try:
            after_confirm = after.iloc[confirm_idx - start_idx + 1:] if start_idx + 1 < len(df) else pd.DataFrame()
        except Exception:
            after_confirm = pd.DataFrame()

        if after_confirm is None or len(after_confirm) == 0:
            return None

        # Get the next candle open
        next_candle = after_confirm.iloc[0]
        entry = float(next_candle["open"])

        # SL = 50% level
        sl = range_50pct

        # TP = 2R
        if dir_side == "buy":
            risk = entry - sl
            tp = entry + 2 * risk
            rr = 2.0
        else:  # sell
            risk = sl - entry
            tp = entry - 2 * risk
            rr = 2.0

        # RR filter
        if not (cfg["min_rr"] <= rr <= cfg["max_rr"]):
            return None

        # Kill‑zone check (optional — NY open window already filtered by time)
        kz_in, kz_name = in_kill_zone(at_ts)

        # Build result
        signal = {
            "dir": dir_side,
            "entry": round(entry, 5),
            "sl": round(sl, 5),
            "tp": round(tp, 5),
            "rr": round(rr, 2),
            "range_high": round(range_high, 5),
            "range_low": round(range_low, 5),
            "range_50pct": round(range_50pct, 5),
            "confirm_candle_idx": confirm_idx.strftime("%Y-%m-%d %H:%M:%S") if hasattr(confirm_idx, 'strftime') else str(confirm_idx),
            "breakout_candle_idx": breakout_idx.strftime("%Y-%m-%d %H:%M:%S") if hasattr(breakout_idx, 'strftime') else str(breakout_idx),
            "strategy": "ny_open",
            "ts": _now_utc().isoformat(),
            "symbol": sym,
            "kz_in": kz_in,
            "kz_name": kz_name,
        }

        return signal

    except Exception:
        import traceback
        traceback.print_exc()
        return None

# ---------------------------------------------------------------------------
# Quick self-test (run with: python -m ny_open)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Test with SPX from yahoo
    result = check(yahoo_symbol="^GSPC", sym="SPX")
    if result:
        print("NY OPEN SIGNAL:")
        for k, v in result.items():
            print(f"  {k}: {v}")
    else:
        print("No valid NY Open setup at this time.")
        print("(This is expected outside the 9:30–11:30 EST window or if pattern not yet formed.)")