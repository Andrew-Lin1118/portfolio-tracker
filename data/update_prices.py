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

def _daily_closes_from_hourly(sym, days=15):
    """用 hourly K 自己 reduce 出每天的「最後一根 close」當該天收盤。
    避開 Yahoo daily K 偶爾漏掉某一天 bar 的 bug（實測 9747.HK 5/4 漏 bar，
    但 hourly K 完整 → daily K reduce 後就對得起來）。
    回傳 dict {date(in market tz): close}，依日期排序。"""
    try:
        df = yf.Ticker(sym).history(period=f'{days}d', interval='1h', auto_adjust=True)
        if df is None or df.empty:
            return {}
        tz_name = _market_tz(sym)
        tz = ZoneInfo(tz_name)
        by_date = {}
        for ts, v in df['Close'].dropna().items():
            try:
                dt = ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo('UTC'))
                local_d = dt.astimezone(tz).date()
                by_date[local_d] = float(v)   # 同一天後續 bar 覆蓋前面 = 取最後一根 close
            except Exception:
                continue
        return by_date
    except Exception:
        return {}


def fetch_prev_two_closes(sym, current_price=None):
    """抓最近兩個「過去交易日」收盤價，供前端「昨日漲跌/今日漲跌」計算。

    步驟：
      1. 先試 Yahoo daily K (period='15d', interval='1d')
      2. 比對 hourly K reduce 出的 daily closes — 若 daily K 缺天，用 hourly
         的補上（解決 Yahoo daily K 漏 bar bug，例：9747.HK 5/4 daily 沒 bar
         但 hourly 顯示 5/4 收盤 13.00）
      3. 排除「今日」的 bar (d < today_mkt)，回傳最近兩天的 close。

    回傳 (prev_close, prev_prev_close)。"""
    try:
        tz_name = _market_tz(sym)
        today_mkt = datetime.now(ZoneInfo(tz_name)).date()

        # ① daily K
        daily_by_date = {}
        try:
            df = yf.Ticker(sym).history(period='15d', auto_adjust=True)
            if df is not None and not df.empty:
                for ts, v in df['Close'].dropna().items():
                    try:
                        d = ts.to_pydatetime().date() if hasattr(ts, 'to_pydatetime') else ts.date()
                    except Exception:
                        continue
                    daily_by_date[d] = float(v)
        except Exception:
            pass

        # ② hourly K（補洞用）
        hourly_by_date = _daily_closes_from_hourly(sym, days=15)

        # ③ 合併：daily 為主，hourly 補 daily 沒有的日期
        merged = dict(daily_by_date)
        for d, v in hourly_by_date.items():
            if d not in merged:
                merged[d] = v

        # ④ 排除今日，依日期排序，取最近兩天
        past = [(d, c) for d, c in merged.items() if d < today_mkt]
        past.sort(key=lambda x: x[0])
        if not past:
            return None, None
        prev = past[-1][1] if len(past) >= 1 else None
        prev_prev = past[-2][1] if len(past) >= 2 else None
        return (round(prev, 4) if prev is not None else None,
                round(prev_prev, 4) if prev_prev is not None else None)
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
    # 傳入 current_price 讓 fetch_prev_two_closes 對齊 price 的日期，
    # 避免「price 與 prev_close 同為 Friday」造成前端今日漲跌 = 0%。
    pc, ppc = fetch_prev_two_closes(sym, current_price=price)
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
