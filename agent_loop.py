# -*- coding: utf-8 -*-
"""agent_loop.py — Agent Loop оркестратор стратегий.

Реализация паттерна Agent Loop (по рисерчу в mempalace system/agent_loop):

  маршрутизация → план → шаг → оценка → (стоп | продолжить) → синтез

Роли компонентов:
  - ROUTING:  какие стратегии применимы для символа (крипта/форекс/индекс)
  - PLANNER:  порядок запуска (самый дешёвый и информативный первым)
  - STEP:     выполнить стратегию через registry
  - EVALUATOR: достаточно данных? паттерн сформирован? противоречия?
  - STOP:     лимит шагов, повторные ошибки, нет данных, выход из окна
  - SYNTHESIS: собрать алерт с evidence (что вызвано, что вернуло, почему шаг)

Используется как в sentinel (фоновый цикл), так и standalone (CLI).
"""

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from strategy_registry import STRATEGIES, list_strategies, check_strategy


# ── Routing: применимость стратегии к символу ─────────────────────────────


def _is_crypto(sym: str, yahoo_symbol: str) -> bool:
    return bool(sym.endswith("USD") and ("=X" not in yahoo_symbol) and ("=" not in yahoo_symbol))


def _is_index(sym: str) -> bool:
    return sym in ("DAX", "SPX", "NDX", "NQ", "ES")


def _route(sym: str, yahoo_symbol: str, cfg: dict) -> list:
    """Вернуть список id стратегий, применимых к символу."""
    is_crypto = _is_crypto(sym, yahoo_symbol)
    is_index = _is_index(sym)

    applicable = []
    for sid, s in STRATEGIES.items():
        strategy_cfg = cfg.get(sid, {})
        if not strategy_cfg.get("enabled", True):
            continue
        if not s["enabled"]:
            continue
        # маршруты: M5 снайпер работает на форекс/крипте, NY Open на индексах
        if sid == "ny_open" and not is_index:
            continue
        if sid == "m5" and not (is_crypto or "=X" in yahoo_symbol or is_index):
            continue
        if sid == "fxssi":
            try:
                from fxssi import FXSSI_SYMBOLS
                if sym not in FXSSI_SYMBOLS:
                    continue
            except Exception:
                continue
        applicable.append(sid)
    return applicable


# ── Planner: порядок шагов ─────────────────────────────────────────────────


PLAN_PRIORITY = {"ny_open": 0, "m5": 1, "zone": 2, "fxssi": 3}


def _plan(applicable: list) -> list:
    """Отсортировать стратегии: сначала быстрые/дешёвые, потом тяжёлые."""
    return sorted(applicable, key=lambda sid: PLAN_PRIORITY.get(sid, 9))


# ── Agent Loop ядро ────────────────────────────────────────────────────────


def run_loop(sym: str, yahoo_symbol: str, cfg: dict,
             max_steps: int = 6, trace: bool = True) -> dict:
    """Запустить Agent Loop для одного символа.

    Возвращает:
      {
        "symbol": sym,
        "strategies_run": [sid, ...],        # что реально запускали
        "signals": {sid: result},            # сформированные сигналы
        "stop_reason": str,                  # почему цикл остановился
        "trace": [ {step, sid, ok, detail}, ... ],  # трассировка (если trace=True)
        "evaluations": [str, ...],           # выводы оценщика
      }
    """
    result = {
        "symbol": sym,
        "strategies_run": [],
        "signals": {},
        "stop_reason": None,
        "trace": [],
        "evaluations": [],
    }
    if trace:
        result["trace"].append({"step": 0, "sid": None, "ok": True,
                                "detail": f"routing для {sym}: вход в цикл"})

    # ROUTING
    applicable = _route(sym, yahoo_symbol, cfg)
    if not applicable:
        result["stop_reason"] = "no_applicable_strategies"
        if trace:
            result["trace"].append({"step": 0, "sid": None, "ok": False,
                                    "detail": "нет применимых стратегий"})
        return result

    # PLANNER
    plan = _plan(applicable)
    result["trace"].append({"step": 0, "sid": None, "ok": True,
                            "detail": f"план: {plan}"})

    # LOOP
    step = 0
    error_streak = 0
    for sid in plan:
        step += 1
        if step > max_steps:
            result["stop_reason"] = "max_steps"
            if trace:
                result["trace"].append({"step": step, "sid": sid, "ok": False,
                                        "detail": f"лимит шагов ({max_steps})"})
            break

        result["strategies_run"].append(sid)
        try:
            res = check_strategy(sid, yahoo_symbol, sym, cfg.get(sid, {}))
            ok = res is not None
            if trace:
                detail = "сигнал" if ok else "паттерн не сформирован"
                result["trace"].append({"step": step, "sid": sid, "ok": ok,
                                        "detail": detail})
            if ok:
                result["signals"][sid] = res
                # EVALUATOR: сигнал найден — продолжаем проверять остальные
                result["evaluations"].append(
                    f"{sym}: {sid} дал сигнал {res['dir']}@{res['entry']}, RR={res['rr']}")
            error_streak = 0
        except Exception as exc:
            error_streak += 1
            if trace:
                result["trace"].append({"step": step, "sid": sid, "ok": False,
                                        "detail": f"ошибка: {exc}"})
            result["evaluations"].append(f"{sym}: {sid} упал: {exc}")
            # STOP: повторные ошибки
            if error_streak >= 3:
                result["stop_reason"] = "repeated_errors"
                if trace:
                    result["trace"].append({"step": step, "sid": sid, "ok": False,
                                            "detail": "3 ошибки подряд — остановка"})
                break

    # SYNTHESIS
    if not result["signals"]:
        result["stop_reason"] = result["stop_reason"] or "no_signals"
    else:
        result["stop_reason"] = result["stop_reason"] or "signals_found"

    if trace:
        result["trace"].append({"step": step + 1, "sid": None, "ok": bool(result["signals"]),
                                "detail": f"синтез: {len(result['signals'])} сигналов"})
    return result


# ── CLI ────────────────────────────────────────────────────────────────────


def main() -> int:
    import argparse
    from strategy import YAHOO_SYMBOLS

    parser = argparse.ArgumentParser(description="Agent Loop — прогнать стратегии по символу")
    parser.add_argument("--symbol", default="DAX", help="символ (или ALL)")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    args = parser.parse_args()

    config = {}
    if args.symbol == "ALL":
        results = {}
        for sym, yahoo in YAHOO_SYMBOLS.items():
            results[sym] = run_loop(sym, yahoo, config)
            print(f"{sym}: {results[sym]['stop_reason']}, "
                  f"сигналов={len(results[sym]['signals'])}")
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    yahoo = YAHOO_SYMBOLS.get(args.symbol, args.symbol)
    res = run_loop(args.symbol, yahoo, config)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"Символ: {res['symbol']}")
    print(f"Стратегии: {res['strategies_run']}")
    print(f"Сигналы: {len(res['signals'])}")
    for sid, sig in res["signals"].items():
        print(f"  [{sid}] {sig['dir']} @ {sig['entry']} SL {sig['sl']} TP {sig['tp']} RR {sig['rr']}")
    print(f"Стоп: {res['stop_reason']}")
    print("\nТрассировка:")
    for t in res["trace"]:
        print(f"  step {t['step']} [{t['sid'] or '-'}] ok={t['ok']} — {t['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())