import sys
sys.path.insert(0, r'C:\Users\h04x\Projects\h04xjrn_2.0\trading')
from strategy import analyze_all, YAHOO_SYMBOLS
from fxssi import get as fget

sym = 'GBPJPY'
s = fget(sym)
a = analyze_all(sym)[0][0]
buy = s['buy']
print('buy', buy, 'last', a['last_price'])
print('liquidity_pool_above', a.get('liquidity_pool_above'))
print('liquidity_pool_below', a.get('liquidity_pool_below'))
print('atr_h1', a.get('atr_h1'))
pool = a.get('liquidity_pool_above')
if pool:
    dist = abs(a['last_price'] - pool) / a['last_price'] * 100
    print('dist to pool above: %.3f%%' % dist)
