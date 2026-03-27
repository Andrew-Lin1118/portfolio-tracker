"""
GitHub Actions 基本面 + 技術面資料更新腳本
每 6 小時執行一次，把 symbols.json 中所有代碼的
  基本面（PE/FPE/PEG/PS/PB/EPS/Rev Growth）
  技術面（日/週/小時 KD、MACD、RSI）
寫入 fundamentals.json，供 GitHub Pages 直接讀取，確保兩端數字一致。
"""
import json, time, os
from datetime import datetime, timezone

import yfinance as yf
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(ROOT, 'symbols.json'), encoding='utf-8') as f:
    symbols = json.load(f)


# ── 工具函數 ──────────────────────────────────────────────────────────────
def safe_float(v, decimals=4):
    """安全轉 float；NaN/None 回傳 None"""
    try:
        f = float(v)
        if f != f:      # NaN
            return None
        return round(f, decimals)
    except Exception:
        return None


def calc_kd(df, period=9):
    """
    KD 隨機指標（與前端 JS calcKD 邏輯完全相同）
    回傳 (k, d, cross)  cross ∈ {'golden', 'death', 'none'}
    """
    if df is None or len(df) < period + 3:
        return None, None, None
    try:
        highs  = df['High'].values.tolist()
        lows   = df['Low'].values.tolist()
        closes = df['Close'].values.tolist()
        n = len(closes)

        # RSV
        rsv = []
        for i in range(n):
            lo = min(lows[max(0, i - period + 1): i + 1])
            hi = max(highs[max(0, i - period + 1): i + 1])
            rsv.append((closes[i] - lo) / (hi - lo) * 100 if hi != lo else 50.0)

        # K / D  (1/3 平滑)
        k, d = 50.0, 50.0
        ks, ds = [k], [d]
        for rv in rsv[1:]:
            k = 2 / 3 * k + 1 / 3 * rv
            d = 2 / 3 * d + 1 / 3 * k
            ks.append(k)
            ds.append(d)

        # 交叉判斷
        cross = 'none'
        if len(ks) >= 2:
            if ks[-2] < ds[-2] and ks[-1] > ds[-1]:
                cross = 'golden'
            elif ks[-2] > ds[-2] and ks[-1] < ds[-1]:
                cross = 'death'

        return round(ks[-1], 1), round(ds[-1], 1), cross
    except Exception:
        return None, None, None


def calc_macd(closes_list):
    """
    MACD（EMA12/26/Signal9，與前端 JS calcMACD 相同）
    回傳 {'macd', 'signal', 'histogram', 'bullish'} 或 None
    """
    if not closes_list or len(closes_list) < 27:
        return None
    try:
        s = pd.Series([float(x) for x in closes_list]).dropna()
        if len(s) < 27:
            return None
        ema12  = s.ewm(span=12, adjust=False).mean()
        ema26  = s.ewm(span=26, adjust=False).mean()
        macd_l = ema12 - ema26
        sig    = macd_l.ewm(span=9, adjust=False).mean()
        hist   = macd_l - sig
        return {
            'macd':      safe_float(macd_l.iloc[-1]),
            'signal':    safe_float(sig.iloc[-1]),
            'histogram': safe_float(hist.iloc[-1]),
            'bullish':   bool(macd_l.iloc[-1] > sig.iloc[-1])
        }
    except Exception:
        return None


def calc_rsi(closes_list, period=14):
    """
    RSI（與前端 JS calcRSI 相同）
    回傳 {'rsi', 'overbought', 'oversold'} 或 None
    """
    if not closes_list or len(closes_list) < period + 2:
        return None
    try:
        closes = [float(c) for c in closes_list if c == c]   # 過濾 NaN
        if len(closes) < period + 2:
            return None
        ag = al = 0.0
        for i in range(1, period + 1):
            d = closes[i] - closes[i - 1]
            ag += max(d, 0)
            al += max(-d, 0)
        ag /= period
        al /= period
        for i in range(period + 1, len(closes)):
            d = closes[i] - closes[i - 1]
            ag = (ag * 13 + max(d, 0)) / 14
            al = (al * 13 + max(-d, 0)) / 14
        rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        return {
            'rsi':        round(rsi, 1),
            'overbought': rsi >= 70,
            'oversold':   rsi <= 30
        }
    except Exception:
        return None


# ── 主迴圈 ────────────────────────────────────────────────────────────────
result = {}

for sym in symbols:
    print(f'  fetching {sym}...', flush=True)
    try:
        t    = yf.Ticker(sym)
        info = t.info

        # ── 基本面 ──────────────────────────────────────────
        eps_cur_q = eps_next_q = rev_fwd = hist_avg_pe = None

        try:
            ee = t.earnings_estimate
            if ee is not None:
                if '0q' in ee.index:
                    v = ee.loc['0q', 'avg']
                    eps_cur_q = float(v) if pd.notna(v) else None
                if '+1q' in ee.index:
                    v = ee.loc['+1q', 'avg']
                    eps_next_q = float(v) if pd.notna(v) else None
        except Exception:
            pass

        try:
            re_df = t.revenue_estimate
            if re_df is not None and '+1y' in re_df.index:
                v = re_df.loc['+1y', 'growth']
                rev_fwd = float(v) if pd.notna(v) else None
        except Exception:
            pass

        try:
            hist = t.history(period='5y', interval='3mo')['Close']
            hist.index = hist.index.tz_localize(None)
            fin = t.quarterly_financials
            if fin is not None and not fin.empty and 'Net Income' in fin.index:
                shares = info.get('sharesOutstanding')
                if shares:
                    eps_s = fin.loc['Net Income'] / shares
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

        # ── 技術面 ──────────────────────────────────────────
        daily_k = daily_d = daily_cross = None
        weekly_k = weekly_d = weekly_cross = None
        hourly_k = hourly_d = hourly_cross = None
        daily_macd = weekly_macd = hourly_macd = None
        daily_rsi  = weekly_rsi  = hourly_rsi  = None

        try:
            daily_df = t.history(period='3mo', interval='1d')
            if not daily_df.empty:
                daily_k, daily_d, daily_cross = calc_kd(daily_df)
                daily_macd = calc_macd(daily_df['Close'].tolist())
                daily_rsi  = calc_rsi(daily_df['Close'].tolist())
                print(f'    daily  K={daily_k} D={daily_d} cross={daily_cross}', flush=True)
        except Exception as e:
            print(f'    daily tech error: {e}', flush=True)

        try:
            weekly_df = t.history(period='2y', interval='1wk')
            if not weekly_df.empty:
                weekly_k, weekly_d, weekly_cross = calc_kd(weekly_df)
                weekly_macd = calc_macd(weekly_df['Close'].tolist())
                weekly_rsi  = calc_rsi(weekly_df['Close'].tolist())
                print(f'    weekly K={weekly_k} D={weekly_d} cross={weekly_cross}', flush=True)
        except Exception as e:
            print(f'    weekly tech error: {e}', flush=True)

        try:
            hourly_df = t.history(period='5d', interval='1h')
            if not hourly_df.empty:
                hourly_k, hourly_d, hourly_cross = calc_kd(hourly_df)
                hourly_macd = calc_macd(hourly_df['Close'].tolist())
                hourly_rsi  = calc_rsi(hourly_df['Close'].tolist())
                print(f'    hourly K={hourly_k} D={hourly_d} cross={hourly_cross}', flush=True)
        except Exception as e:
            print(f'    hourly tech error: {e}', flush=True)

        # 昨收價 & 前日收盤價（用於前端計算當日/前日漲跌）
        prev_close = info.get('regularMarketPreviousClose') or info.get('previousClose')
        prev_prev_close = None
        try:
            d5 = t.history(period='5d', interval='1d')
            closes5 = d5['Close'].dropna().tolist()
            if len(closes5) >= 3:
                prev_prev_close = safe_float(closes5[-3])
            elif len(closes5) >= 2:
                prev_prev_close = safe_float(closes5[-2])
        except Exception:
            pass

        result[sym] = {
            # 基本面
            'pe':          info.get('trailingPE'),
            'fpe':         info.get('forwardPE'),
            'peg':         info.get('trailingPegRatio'),
            'ps':          info.get('priceToSalesTrailing12Months'),
            'pb':          info.get('priceToBook'),
            'rev_yoy':     info.get('revenueGrowth'),
            'rev_fwd':     rev_fwd,
            'hist_avg_pe': hist_avg_pe,
            'eps_ttm':     info.get('trailingEps'),
            'eps_cur_q':   eps_cur_q,
            'eps_next_q2': eps_next_q,
            'eps_cur_y':   info.get('epsCurrentYear'),
            'eps_next_y':  info.get('epsForward'),
            'prev_close':      safe_float(prev_close),
            'prev_prev_close': prev_prev_close,
            # 技術面（日/週/小時）
            'daily_k':  daily_k,  'daily_d':  daily_d,  'daily_cross':  daily_cross,
            'weekly_k': weekly_k, 'weekly_d': weekly_d, 'weekly_cross': weekly_cross,
            'hourly_k': hourly_k, 'hourly_d': hourly_d, 'hourly_cross': hourly_cross,
            'daily_macd':  daily_macd,
            'weekly_macd': weekly_macd,
            'hourly_macd': hourly_macd,
            'daily_rsi':   daily_rsi,
            'weekly_rsi':  weekly_rsi,
            'hourly_rsi':  hourly_rsi,
        }
        print(f'    PE={result[sym]["pe"]}  FPE={result[sym]["fpe"]}', flush=True)

    except Exception as e:
        print(f'    ERROR: {e}', flush=True)
        result[sym] = {'error': str(e)}

    time.sleep(1.0)   # 避免觸發 rate-limit

# ── 寫出 ──────────────────────────────────────────────────────────────────
output = {
    'generated': datetime.now(timezone.utc).isoformat(),
    'data': result
}

out_path = os.path.join(ROOT, 'fundamentals.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f'\nDone. {len(result)} symbols → fundamentals.json  ({datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")})')
