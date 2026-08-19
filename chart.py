"""chart.py — render a H4 snapshot chart with all institutional levels + entry markers.

Saves PNG to snapshots/chart_<SYMBOL>_<ts>.png. Uses matplotlib only;
fails gracefully (no chart) if rendering is not possible.

Entry markers persist across renders (stored in chart_entries.json) until manually cleared.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from strategy import YAHOO_SYMBOLS, analyze_symbol, fetch_candles

HERE = Path(__file__).parent
ENTRIES_FILE = HERE / "chart_entries.json"


def load_entries():
    """Load persistent entry markers."""
    if not ENTRIES_FILE.exists():
        return []
    try:
        return json.loads(ENTRIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_entry(symbol: str, signal: str, price: float, sl: float, tp: float):
    """Save new entry marker (persists across renders)."""
    entries = load_entries()
    entries.append({
        "symbol": symbol,
        "signal": signal,
        "price": price,
        "sl": sl,
        "tp": tp,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    ENTRIES_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_entries(symbol: str = None):
    """Clear entry markers (all or for specific symbol)."""
    if symbol:
        entries = [e for e in load_entries() if e["symbol"] != symbol]
        ENTRIES_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        ENTRIES_FILE.write_text("[]", encoding="utf-8")


def render(symbol: str, out_dir: Path = None, add_entry_marker: bool = True) -> str:
    out_dir = out_dir or (HERE / "snapshots")
    out_dir.mkdir(exist_ok=True)
    a = analyze_symbol(symbol, YAHOO_SYMBOLS[symbol])
    h4 = fetch_candles(YAHOO_SYMBOLS[symbol], "4h", "2mo")
    if len(h4) < 40:
        return ""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except Exception:
        return ""
    
    fig, ax = plt.subplots(figsize=(14, 8), dpi=120)
    h4 = h4.tail(160)
    x = h4.index
    
    # Candles
    ax.plot(x, h4["close"], color="#1a9c4f", lw=1.2, label="close", zorder=2)
    ax.fill_between(x, h4["low"], h4["high"], color="#1a9c4f", alpha=0.08, zorder=1)
    
    # Levels
    levels = []
    for key, color, lab in [
        ("liquidity_pool_above", "#e0393e", "пул выше (sell-стопы)"),
        ("liquidity_pool_below", "#2f7fe0", "пул ниже (buy-стопы)"),
        ("h4_resistance", "#b06be0", "H4 сопротивление"),
        ("h4_support", "#b06be0", "H4 поддержка"),
        ("nearest_resistance", "#e08a1e", "ближ. сопротивление"),
        ("nearest_support", "#e08a1e", "ближ. поддержка"),
        ("tline_support", "#19b6c9", "тренд-линия (поддержка)"),
        ("tline_resistance", "#19b6c9", "тренд-линия (сопротивление)"),
    ]:
        v = a.get(key)
        if v:
            levels.append((v, color, lab))
    for v, color, lab in levels:
        ax.axhline(v, color=color, lw=0.9, ls="--", alpha=0.85, zorder=3)
        ax.text(x[-1], v, f" {v:.3f} {lab}", fontsize=8, color=color, va="center")
    
    # Fib
    for name, val in a.get("fib", {}).items():
        ax.axhline(val, color="#999", lw=0.6, ls=":", alpha=0.7, zorder=3)
        ax.text(x[-1], val, f" fib {name} {val:.3f}", fontsize=7, color="#777", va="center")
    
    # MTF levels
    mtf = a.get("mtf_levels") or {}
    for group, lvls in mtf.items():
        for lvl in lvls[:2]:
            ax.axhline(lvl, color="#d4a017", lw=0.5, ls="-", alpha=0.5, zorder=3)
    
    # Current price
    ax.axhline(a["last_price"], color="#000", lw=1.4, zorder=4)
    ax.text(x[-1], a["last_price"], f" {a['last_price']:.3f} {a['signal'].upper()}",
            fontsize=9, fontweight="bold", color="#000", va="center")
    
    # Entry markers (persistent)
    entries = [e for e in load_entries() if e["symbol"] == symbol]
    for entry in entries:
        signal = entry["signal"]
        price = entry["price"]
        sl = entry["sl"]
        tp = entry["tp"]
        
        # Arrow marker at entry price
        color = "#089981" if signal == "buy" else "#F23645"
        marker = "^" if signal == "buy" else "v"
        ax.scatter([x[-10]], [price], s=200, color=color, marker=marker, zorder=10, edgecolors="#fff", linewidths=1.5)
        
        # SL/TP lines
        ax.axhline(sl, color="#F23645", lw=1.2, ls=":", alpha=0.7, zorder=5)
        ax.text(x[-20], sl, f" SL {sl:.3f}", fontsize=8, color="#F23645", va="center", fontweight="bold")
        
        ax.axhline(tp, color="#089981", lw=1.2, ls=":", alpha=0.7, zorder=5)
        ax.text(x[-20], tp, f" TP {tp:.3f}", fontsize=8, color="#089981", va="center", fontweight="bold")
    
    # Add current signal as entry marker (if strong enough)
    if add_entry_marker and a["signal"] in ("buy", "sell"):
        params = a.get("params")
        if params:
            signal = a["signal"]
            entry_price = a["liquidity_pool_below"] if signal == "buy" else a["liquidity_pool_above"]
            sl_price = entry_price * params["sl_buf"] if signal == "buy" else entry_price * (2 - params["sl_buf"])
            tp_price = entry_price * params["tp_buf"] if signal == "buy" else entry_price * (2 - params["tp_buf"])
            
            # Save for persistence
            save_entry(symbol, signal, entry_price, sl_price, tp_price)
            
            # Draw projected entry
            color = "#089981" if signal == "buy" else "#F23645"
            marker = "^" if signal == "buy" else "v"
            ax.scatter([x[-1]], [entry_price], s=250, color=color, marker=marker, zorder=10, 
                      edgecolors="#FFD700", linewidths=2, alpha=0.9, label=f"ВХОД {signal.upper()}")
    
    # ── Future projections ───────────────────────────────────────────────────
    # Show expected price path: entry → TP and entry → SL as shaded triangles
    # Uses NEUTRAL signal zone data to draw probable future paths
    now_idx = len(x) - 1
    params = a.get("params")
    if params:
        for side, zone_key, sl_key_dir, tp_key_dir, arrow_color, sl_color in [
            ("buy",  "liquidity_pool_below", "sl_buf",  "tp_buf",  "#089981", "#F23645"),
            ("sell", "liquidity_pool_above", "sl_buf",  "tp_buf",  "#F23645", "#089981"),
        ]:
            zone_price = a.get(zone_key)
            if not zone_price:
                continue
            if side == "buy":
                sl = zone_price * params["sl_buf"]
                tp = zone_price * params["tp_buf"]
            else:
                # for sell: SL is above, TP is below
                sl = zone_price * (2 - params["sl_buf"])
                tp = zone_price * (2 - params["tp_buf"])
            
            # hold period in candles (4h candles, hold in days)
            hold_candles = params.get("hold", 10) * 6  # 6 x 4h per day
            proj_end = min(now_idx + hold_candles, len(x) - 1)
            proj_x = x[now_idx:proj_end + 1] if proj_end > now_idx else x[now_idx:now_idx + 2]
            
            # TP zone — filled green/red
            tp_alpha = 0.12 if a["signal"] == side else 0.06
            ax.fill_between(
                proj_x,
                [zone_price] * len(proj_x),
                [tp] * len(proj_x),
                color=arrow_color, alpha=tp_alpha, zorder=1,
            )
            ax.annotate(
                f"→ TP {tp:.3f}\n   (+{abs(tp - zone_price) / zone_price * 100:.2f}%)",
                xy=(proj_x[-1], tp),
                fontsize=8, color=arrow_color, fontweight="bold",
                ha="left", va="center",
            )
            
            # SL zone — filled red/green (smaller, danger zone)
            ax.fill_between(
                proj_x,
                [sl] * len(proj_x),
                [zone_price] * len(proj_x),
                color=sl_color, alpha=0.06, zorder=1,
            )
            ax.annotate(
                f"→ SL {sl:.3f}\n   (-{abs(sl - zone_price) / zone_price * 100:.2f}%)",
                xy=(proj_x[-1], sl),
                fontsize=8, color=sl_color, fontweight="bold",
                ha="left", va="center",
            )
            
            # Vertical dashed line at entry zone
            ax.axvline(x[now_idx], color="#aaa", lw=0.8, ls="--", alpha=0.5, zorder=3)
    # ─────────────────────────────────────────────────────────────────────────

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.set_title(f"{symbol} H4 — {a['signal'].upper()} | "
                 f"зелень=BUY-проекция / красн=SELL-проекция | "
                 f"стрелки=активные входы", fontsize=11, fontweight="bold")
    ax.grid(alpha=0.25)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    path = out_dir / f"chart_{symbol}_{int(datetime.now(timezone.utc).timestamp())}.png"
    fig.savefig(path, facecolor="#f8f9fa", bbox_inches="tight")
    plt.close(fig)
    return str(path)


def main() -> int:
    symbols = sys.argv[1:] or list(YAHOO_SYMBOLS)
    for s in symbols:
        try:
            p = render(s)
            print(f"{s}: {'ok ' + p if p else 'no chart (fetch/import failed)'}")
        except Exception as exc:
            print(f"{s}: error {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())