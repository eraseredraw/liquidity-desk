"""signal.py — единая логика входа/выхода для лайва (strategy.py) и бэктеста (backtest.py).

Один источник правды: что проверяет лайв, то же валидирует бэктест.
"""


def signal_from_features(last, ema200, h4_trend, d1_trend, liquidity_buy,
                         liquidity_sell, nearest_below, nearest_above, params,
                         w1_trend=None) -> dict:
    """Вычислить сигнал + уровни SL/TP из рассчитанных фич.

    Параметры:
      last           — текущая цена (close последней свечи)
      ema200         — трендовый фильтр (лайв: H1 EMA200; бэктест: D1 EMA200)
      h4_trend/d1_trend — "bullish"/"bearish"
      w1_trend       — "bullish"/"bearish" (None = не учитывать, обратная совместимость)
      liquidity_buy  — зона buy-стопов (ниже цены) или None
      liquidity_sell — зона sell-стопов (выше цены) или None
      nearest_below/nearest_above — ближайшие уровни поддержки/сопротивления
      params         — SYMBOL_PARAMS[symbol] или None (инструмент выключен)

    Фильтры тренда (params["filter"]): h4 | d1 | w1 | both (h4+d1) |
    w1d1 | w1h4. Для buy требуются BULLISH все источники фильтра, для sell —
    BEARISH.

    Возвращает {"signal", "sl", "tp", "zone_lvl"}.
    signal: neutral | buy | sell | disabled. SL/TP посчитаны, но БЭКТЕСТ
    обязан проверить их против фактического entry (см. backtest.py).
    """
    signal, zone_lvl = "neutral", None
    sl = tp = None
    if params is None:
        return {"signal": "disabled", "sl": None, "tp": None, "zone_lvl": None}
    if nearest_below and nearest_above:
        zone = params["zone"]
        f = params["filter"]
        required = {
            "h4": ["h4"], "d1": ["d1"], "w1": ["w1"],
            "both": ["h4", "d1"], "w1d1": ["w1", "d1"], "w1h4": ["w1", "h4"],
        }.get(f, ["h4", "d1"])
        trends = {"h4": h4_trend, "d1": d1_trend}
        if w1_trend is not None:
            trends["w1"] = w1_trend
        buy_ok = all(trends.get(t) == "bullish" for t in required)
        sell_ok = all(trends.get(t) == "bearish" for t in required)
        if (last < ema200 * 0.999 and liquidity_buy and last <= liquidity_buy * zone
                and last - liquidity_buy <= (last - nearest_below) and buy_ok):
            signal, zone_lvl = "buy", liquidity_buy
        elif (last > ema200 * 1.001 and liquidity_sell and last >= liquidity_sell / zone
              and liquidity_sell - last <= (nearest_above - last) and sell_ok):
            signal, zone_lvl = "sell", liquidity_sell
    if signal == "buy" and zone_lvl:
        sl = zone_lvl * params["sl_buf"]
        tp = nearest_above * params["tp_buf"] if nearest_above else zone_lvl * (2 - params["sl_buf"])
    elif signal == "sell" and zone_lvl:
        sl = zone_lvl / params["sl_buf"]
        tp = nearest_below / params["tp_buf"] if nearest_below else zone_lvl * (2 - params["sl_buf"])
    return {"signal": signal, "sl": sl, "tp": tp, "zone_lvl": zone_lvl}
