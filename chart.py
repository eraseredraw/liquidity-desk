"""chart.py — render a H4 snapshot chart with all institutional levels.

Saves PNG to snapshots/chart_<SYMBOL>_<ts>.png. Uses matplotlib only;
fails gracefully (no chart) if rendering is not possible.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from strategy import YAHOO_SYMBOLS, analyze_symbol, fetch_candles

HERE = Path(__file__).parent


def render(symbol: str, out_dir: Path = None) -> str:
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
    fig, ax = plt.subplots(figsize=(12, 7), dpi=110)
    h4 = h4.tail(160)
    x = h4.index
    ax.plot(x, h4["close"], color="#1a9c4f", lw=1.2, label="close")
    ax.fill_between(x, h4["low"], h4["high"], color="#1a9c4f", alpha=0.08)
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
        ax.axhline(v, color=color, lw=0.9, ls="--", alpha=0.85)
        ax.text(x[-1], v, f" {v} {lab}", fontsize=8, color=color, va="center")
    for name, val in a.get("fib", {}).items():
        ax.axhline(val, color="#999", lw=0.6, ls=":", alpha=0.7)
        ax.text(x[-1], val, f" fib {name} {val}", fontsize=7, color="#777", va="center")
    mtf = a.get("mtf_levels") or {}
    for group, lvls in mtf.items():
        for lvl in lvls[:2]:
            ax.axhline(lvl, color="#d4a017", lw=0.5, ls="-", alpha=0.5)
    ax.axhline(a["last_price"], color="#000", lw=1.4)
    ax.text(x[-1], a["last_price"], f" {a['last_price']} {a['signal'].upper()}",
            fontsize=9, fontweight="bold", color="#000", va="center")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.set_title(f"{symbol} H4 — {a['signal']} | скор-вход в совете | "
                 f"зоны: пулы/уровни/фибо/тренд-линии", fontsize=11)
    ax.grid(alpha=0.25)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    path = out_dir / f"chart_{symbol}_{a.get('last_price')}.png"
    fig.savefig(path)
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