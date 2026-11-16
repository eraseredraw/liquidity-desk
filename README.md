# Liquidity Desk

Personal institutional-liquidity trading system: levels/zones detection, signal engine,
backtesting, 24/7 sentinel with alerts, multi-timeframe oracle, and a local web dashboard.

> **Status: beta.** The core works end-to-end (analysis, backtests, alerts, dashboard),
> but it is a research/demo project, not a production trading product.
> No guarantee of profitability; use at your own risk.

![python](https://img.shields.io/badge/python-3.13-blue)
![status](https://img.shields.io/badge/status-beta-orange)

## What it does

| Module | Purpose |
|---|---|
| `strategy.py` / `strategy_signal.py` | Support/resistance pools (H4/D1), liquidity zones, POI, market structure; single source of truth for signals |
| `backtest.py` / `grid_backtest.py` | Event-driven backtests identical to live logic; grid search + holdout stats |
| `sentinel.py` | Background watcher: adaptive ATR-based zones, M5 BOS confirmation, kill-zone and econ-calendar filters, Telegram/desktop alerts |
| `advice.py` / `chart.py` | On-demand symbol report: signal, score, confidence, risk levels, rendered chart |
| `oracle.py` | Multi-timeframe consensus: records advice vs. actual outcome for calibration |
| `journal.py` | Trade journal with R-based risk bookkeeping |
| `telegram_bot.py` / `telegram_alert.py` | Long-polling bot (`/market`, `/advice`, `/status`) and push alerts |
| `webui.py` | Local dashboard (`http://localhost:8000`): market now, signals, journal, charts |
| `econ_calendar.py` | High-impact news veto window |

## Architecture

```
 data (providers.py)          HTTP: market candles, econ calendar
        |
 core logic                  strategy / zones / structure -> signal
        |
 consumers                   sentinel (pull loop) | advice (one-shot) | oracle (tracking)
        |
 outputs                     Telegram | web dashboard | markdown reports | journal
```

Config-driven: risk limits (`risk_guard.json`), sentinel params (`sentinel_config.json`),
ATR baselines (`atr_baseline.json`). Secrets (API keys, bot tokens) live in an
untracked `data_source.json` — see `data_source.json.example`.

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install -r requirements.txt
copy data_source.json.example data_source.json     # fill in your keys
python sentinel.py                                 # start background watcher
python webui.py                                    # open http://localhost:8000
```

One-shot tools:

```bash
python advice.py --symbol XAUUSD          # full report for a symbol
python backtest.py                        # backtest vs live logic
python signals_watch.py                   # track signal outcomes
```

## Notes

- Runs on Windows batch wrappers (`run_*.bat`) or directly with Python.
- No MT5 terminal dependency for analysis; live execution (`execution/broker.py`)
  uses the MetaTrader Python API and is optional.
