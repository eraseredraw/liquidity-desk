import json, sys
sys.path.insert(0, r'C:\Users\h04x\Projects\h04xjrn_2.0\trading')
from strategy_registry import check_strategy, list_strategies
from strategy import YAHOO_SYMBOLS
import fxssi

print('fxssi bias по символам:')
for sym, yahoo in YAHOO_SYMBOLS.items():
    s = fxssi.get(sym)
    b = fxssi.bias(sym)
    print(f"  {sym:8s} buy {s['buy']:5.1f}%  bias={b['bias']:8s} {b['reason']}")

print()
print('Проверка check_strategy("fxssi") для GBPJPY:')
res = check_strategy('fxssi', YAHOO_SYMBOLS['GBPJPY'], 'GBPJPY', {})
print(json.dumps(res, ensure_ascii=False, indent=2))
