"""
GitHub Actions 即時報價更新腳本
每 5 分鐘執行一次（美股交易日），寫入 data/prices.json
前端 silentRefreshPrices() 優先讀取此檔，不依賴 Google Sheet。
"""
import json, os, time, urllib.request
from datetime import datetime, timezone

import yfinance as yf

ROOT = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(ROOT, 'price_symbols.json'), encoding='utf-8') as f:
    symbols = json.load(f)

prices = {}
for sym in symbols:
    try:
        fi = yf.Ticker(sym).fast_info
        price = getattr(fi, 'last_price', None)
        if price is None or price != price:          # None 或 NaN
            price = getattr(fi, 'previous_close', None)
        if price and float(price) > 0:
            prices[sym] = round(float(price), 4)
        print(f'  {sym}: {prices.get(sym, "N/A")}', flush=True)
    except Exception as e:
        print(f'  {sym}: ERROR {e}', flush=True)
    time.sleep(0.25)

# 匯率（open.er-api.com，免費，無需 API key）
rates = {}
try:
    with urllib.request.urlopen(
        'https://open.er-api.com/v6/latest/USD', timeout=10
    ) as r:
        data = json.loads(r.read())
    if data.get('rates'):
        twd = data['rates'].get('TWD')
        hkd = data['rates'].get('HKD')
        if twd and hkd:
            rates['USD_TWD'] = round(float(twd), 4)
            rates['HKD_TWD'] = round(float(twd) / float(hkd), 4)
    print(f'  Rates: {rates}', flush=True)
except Exception as e:
    print(f'  Rates ERROR: {e}', flush=True)

output = {
    'generated': datetime.now(timezone.utc).isoformat(),
    'prices':    prices,
    'rates':     rates,
}

out_path = os.path.join(ROOT, 'prices.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f'\nDone. {len(prices)} prices, {len(rates)} rates → prices.json  '
      f'({datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")})')
