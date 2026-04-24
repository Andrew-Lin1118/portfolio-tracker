"""
GitHub Actions 即時報價更新腳本
每 5 分鐘執行一次（美股交易日），寫入 data/prices.json
前端 silentRefreshPrices() 優先讀取此檔，不依賴 Google Sheet。
"""
import json, os, time, urllib.request
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo

import yfinance as yf


def _market_tz(sym: str) -> str:
    """依代碼後綴推斷市場時區；用於「今日」判斷，避免把已收盤的今日 bar 誤當成昨日。"""
    if sym.endswith('.KS'): return 'Asia/Seoul'
    if sym.endswith('.HK'): return 'Asia/Hong_Kong'
    if sym.endswith('.TW'): return 'Asia/Taipei'
    if sym.endswith('-USD') or sym.endswith('-USDT'): return 'UTC'
    return 'America/New_York'  # 美股 / 其他預設

ROOT = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(ROOT, 'price_symbols.json'), encoding='utf-8') as f:
    symbols = json.load(f)

def fetch_price(sym):
    """三層備援抓現價：fast_info → info dict → download 1m bar"""
    # 層 1: fast_info（最快）
    try:
        fi = yf.Ticker(sym).fast_info
        for attr in ('last_price', 'previous_close'):
            v = getattr(fi, attr, None)
            if v is not None and v == v and float(v) > 0:   # not None, not NaN
                return round(float(v), 4)
    except Exception:
        pass
    # 層 2: info dict（加密貨幣 / 部分 ETF 較可靠）
    try:
        info = yf.Ticker(sym).info
        for key in ('regularMarketPrice', 'currentPrice', 'previousClose',
                    'regularMarketPreviousClose'):
            v = info.get(key)
            if v and float(v) > 0:
                return round(float(v), 4)
    except Exception:
        pass
    # 層 3: 下載最近 1 分鐘 K 棒取最後收盤價
    try:
        import pandas as pd
        df = yf.download(sym, period='1d', interval='1m',
                         progress=False, auto_adjust=True)
        if not df.empty:
            closes = df['Close'].dropna()
            if not closes.empty:
                v = float(closes.iloc[-1])
                if v > 0:
                    return round(v, 4)
    except Exception:
        pass
    return None

def fetch_prev_close(sym):
    """抓昨收價（前一交易日收盤）：fast_info.previous_close → info dict"""
    try:
        fi = yf.Ticker(sym).fast_info
        v = getattr(fi, 'previous_close', None)
        if v is not None and v == v and float(v) > 0:
            return round(float(v), 4)
    except Exception:
        pass
    try:
        info = yf.Ticker(sym).info
        for key in ('regularMarketPreviousClose', 'previousClose'):
            v = info.get(key)
            if v and float(v) > 0:
                return round(float(v), 4)
    except Exception:
        pass
    return None

def fetch_prev_two_closes(sym):
    """一次抓最近兩個「過去交易日」收盤價，供前端「昨日漲跌/昨日損益」計算。
    關鍵：用市場當地時區判斷「今日」並排除今日的 bar —
      若台股 18:43（已收盤）跑，history 會含 4/23 today bar；
      若不排除，前端會把 4/23 當昨收、4/22 當前日，結果「昨日漲跌」顯示的變成前天 vs 大前天。
    回傳 (prev_close, prev_prev_close)。"""
    try:
        df = yf.Ticker(sym).history(period='15d', auto_adjust=True)
        if df is None or df.empty:
            return None, None
        tz_name = _market_tz(sym)
        today_mkt = datetime.now(ZoneInfo(tz_name)).date()
        past_closes = []
        for ts, v in df['Close'].dropna().items():
            try:
                # ts 可能是 tz-aware（國際股）或 tz-naive（美股）；統一拿 date
                d = ts.to_pydatetime().date() if hasattr(ts, 'to_pydatetime') else ts.date()
            except Exception:
                continue
            if d < today_mkt:  # 嚴格小於今日 → 過去交易日
                past_closes.append(float(v))
        if len(past_closes) >= 2:
            return round(past_closes[-1], 4), round(past_closes[-2], 4)
        if len(past_closes) == 1:
            return round(past_closes[-1], 4), None
    except Exception:
        pass
    return None, None

prices = {}
prev_closes = {}
prev_prev_closes = {}
for sym in symbols:
    price = fetch_price(sym)
    if price:
        prices[sym] = price
    # 先嘗試一次抓兩天收盤（比較準，避開假日）；失敗則退用單天 fast_info
    pc, ppc = fetch_prev_two_closes(sym)
    if pc is None:
        pc = fetch_prev_close(sym)
    if pc:
        prev_closes[sym] = pc
    if ppc:
        prev_prev_closes[sym] = ppc
    print(f'  {sym}: price={prices.get(sym,"N/A")}  prev={prev_closes.get(sym,"N/A")}  prev_prev={prev_prev_closes.get(sym,"N/A")}', flush=True)
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
    'generated':        datetime.now(timezone.utc).isoformat(),
    'prices':           prices,
    'prev_closes':      prev_closes,      # 昨收價，供前端 prevCloseCache 使用（避免 CORS yfFetch 失敗）
    'prev_prev_closes': prev_prev_closes, # 前日收盤，供「昨日漲跌/昨日損益」計算（手機 yfFetch 常失敗時的 fallback）
    'rates':            rates,
}

out_path = os.path.join(ROOT, 'prices.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f'\nDone. {len(prices)} prices, {len(rates)} rates → prices.json  '
      f'({datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")})')
