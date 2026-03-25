"""
GitHub Actions 基本面資料更新腳本
每 6 小時執行一次，把 symbols.json 中所有代碼的基本面寫入 fundamentals.json
"""
import json, re, time, os
from datetime import datetime, timezone

import yfinance as yf
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(ROOT, 'symbols.json'), encoding='utf-8') as f:
    symbols = json.load(f)

result = {}

for sym in symbols:
    print(f'  fetching {sym}...', flush=True)
    try:
        t = yf.Ticker(sym)
        info = t.info

        # EPS 季度預估
        eps_next_q = None
        try:
            ee = t.earnings_estimate
            if ee is not None and '0q' in ee.index:
                v = ee.loc['0q', 'avg']
                eps_next_q = float(v) if pd.notna(v) else None
        except Exception:
            pass

        # Revenue Growth FWD
        rev_fwd = None
        try:
            re_df = t.revenue_estimate
            if re_df is not None and '+1y' in re_df.index:
                v = re_df.loc['+1y', 'growth']
                rev_fwd = float(v) if pd.notna(v) else None
        except Exception:
            pass

        # 歷史平均 PE（5 年，季頻）
        hist_avg_pe = None
        try:
            hist = t.history(period='5y', interval='3mo')['Close']
            hist.index = hist.index.tz_localize(None)
            fin = t.quarterly_financials
            if fin is not None and not fin.empty and 'Net Income' in fin.index:
                shares = info.get('sharesOutstanding')
                eps_s  = fin.loc['Net Income'] / shares if shares else pd.Series(dtype=float)
                eps_s.index = pd.to_datetime(eps_s.index).tz_localize(None)
                pe_list = []
                for date, price in hist.items():
                    ttm = eps_s[eps_s.index <= date].head(4).sum()
                    if ttm > 0:
                        pe_list.append(price / ttm)
                if pe_list:
                    hist_avg_pe = round(sum(pe_list) / len(pe_list), 2)
        except Exception:
            pass

        result[sym] = {
            'pe':          info.get('trailingPE'),
            'fpe':         info.get('forwardPE'),
            'peg':         info.get('trailingPegRatio'),
            'ps':          info.get('priceToSalesTrailing12Months'),
            'pb':          info.get('priceToBook'),
            'rev_yoy':     info.get('revenueGrowth'),
            'rev_fwd':     rev_fwd,
            'hist_avg_pe': hist_avg_pe,
            'eps_ttm':     info.get('trailingEps'),
            'eps_next_q':  eps_next_q,
            'eps_cur_y':   info.get('epsCurrentYear'),
            'eps_next_y':  info.get('epsForward'),
        }
        print(f'    PE={result[sym]["pe"]}  FPE={result[sym]["fpe"]}', flush=True)
    except Exception as e:
        print(f'    ERROR: {e}', flush=True)
        result[sym] = {'error': str(e)}

    time.sleep(0.8)   # 避免觸發 rate-limit

output = {
    'generated': datetime.now(timezone.utc).isoformat(),
    'data': result
}

out_path = os.path.join(ROOT, 'fundamentals.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f'\nDone. {len(result)} symbols written to fundamentals.json')
