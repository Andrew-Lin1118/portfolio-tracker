"""
GitHub Actions 基本面 + 技術面資料更新腳本
每 6 小時執行一次，把 symbols.json 中所有代碼的
  基本面（PE/FPE/PEG/PS/PB/EPS/Rev Growth）
  技術面（日/週/小時 KD、MACD、RSI）
寫入 fundamentals.json，供 GitHub Pages 直接讀取，確保兩端數字一致。
"""
import json, time, os
from datetime import datetime, timezone, timedelta

import yfinance as yf
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(ROOT, 'symbols.json'), encoding='utf-8') as f:
    symbols = json.load(f)

# ── 代理標的對應表：HK 掛牌工具 → 真實標的 ───────────────────────────────
# 用於補充 yfinance 無法取得 HK 代理標的財報日期的情況
# key = 代理代碼（symbols.json 中的代碼），value = 原型代碼（同在 symbols.json 中）
PROXY_EARNINGS_MAP = {
    '7709.HK': '000660.KS',   # 南韓 SK 海力士 ETF → 000660.KS
    '9747.HK': '005930.KS',   # 南韓三星電子 ETF  → 005930.KS
}

# ── 財報日期手動覆蓋表（yfinance 時間戳偶有偏差，在此校正） ─────────────
# yfinance 對部份亞洲股（尤其韓股）的 earnings_dates 時間戳會用 EDT 清晨或
# 午後時段標記，換算 KST 後可能較實際發布日早 1 天。若已知下一季真實發布日
# 可在此指定 YYYY-MM-DD (交易所本地日期)，程式會以此覆寫 yfinance 回傳值。
# 每季發布完後建議更新或刪除對應條目。
EARNINGS_DATE_OVERRIDES = {
    # Q1 2026: SK Hynix 已於 2026-04-23 發布，yfinance 現已回傳正確的 Q2 日期，無需覆蓋
}


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

        # ── 季度 EPS 歷史 + 下次財報日期（供前端使用）──────────────────
        earnings_history = []
        next_earnings_date = None
        try:
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                # 動態找欄位名稱（yfinance 不同版本欄位名稱略有差異）
                col_actual = next((c for c in ed.columns if 'Reported' in c or ('EPS' in c and 'Estimate' not in c)), None)
                col_est    = next((c for c in ed.columns if 'Estimate' in c), None)
                col_surp   = next((c for c in ed.columns if 'Surprise' in c), None)
                if col_actual:
                    reported = ed[ed[col_actual].notna()].copy()
                    reported = reported.sort_index()  # 由舊到新
                    # 交易所本地時區（與 next_earnings_date 段共用相同對照表）
                    _sfx_hist = sym.split('.')[-1].upper() if '.' in sym else ''
                    _tz_name_hist = {
                        'KS': 'Asia/Seoul',  'KQ': 'Asia/Seoul',
                        'HK': 'Asia/Hong_Kong',
                        'TW': 'Asia/Taipei', 'TWO': 'Asia/Taipei',
                        'T':  'Asia/Tokyo',
                    }.get(_sfx_hist, 'America/New_York')
                    import pytz as _ptz_hist
                    _local_tz_hist = _ptz_hist.timezone(_tz_name_hist)
                    for date, row in reported.iterrows():
                        try:
                            dt = pd.Timestamp(date)
                            # 轉換到交易所本地時區（避免 UTC 跨日造成季度判斷錯誤）
                            if dt.tzinfo is not None:
                                dt = dt.tz_convert(_local_tz_hist).replace(tzinfo=None)
                            elif dt.tzinfo is None:
                                dt = dt.tz_localize('UTC').tz_convert(_local_tz_hist).replace(tzinfo=None)
                            q_num = (dt.month - 1) // 3 + 1
                            eps_actual = safe_float(row[col_actual])
                            eps_est    = safe_float(row[col_est]) if col_est else None
                            surp_pct   = None
                            if col_surp and pd.notna(row.get(col_surp, None)):
                                surp_pct = safe_float(float(row[col_surp]) / 100)
                            elif eps_actual is not None and eps_est is not None and eps_est != 0:
                                surp_pct = safe_float((eps_actual - eps_est) / abs(eps_est))
                            earnings_history.append({
                                'quarter':      f'{dt.year} Q{q_num}',
                                'eps_estimate': eps_est,
                                'eps_actual':   eps_actual,
                                'surprise_pct': surp_pct,
                            })
                        except Exception:
                            pass
                    # ── 下次財報日期：col_actual 為 NaN 的未來日期中最近的一個 ──
                    # 注意：yfinance earnings_dates 的 index 是 UTC Timestamp；
                    # 直接剝掉時區會讓亞洲市場（KS/HK）日期偏一天。
                    # 解法：先 tz_convert 到交易所本地時區，再取 .date()。
                    try:
                        import pytz as _ptz_ned
                        _sfx_ned = sym.split('.')[-1].upper() if '.' in sym else ''
                        _tz_name_ned = {
                            'KS': 'Asia/Seoul',  'KQ': 'Asia/Seoul',
                            'HK': 'Asia/Hong_Kong',
                            'TW': 'Asia/Taipei', 'TWO': 'Asia/Taipei',
                            'T':  'Asia/Tokyo',
                        }.get(_sfx_ned, 'America/New_York')
                        _local_tz_ned  = _ptz_ned.timezone(_tz_name_ned)
                        _today_local   = datetime.now(_local_tz_ned).date()
                        # 寬限：pending (NaN actual) 之財報日若在過去 7 天內，
                        # 仍視為「下一次」──處理 yfinance 時間戳略為過期但尚未
                        # 標示 Reported EPS 的情形。
                        _grace_past = _today_local - timedelta(days=7)
                        _pending = []
                        for _idx in ed.index:
                            try:
                                _ts = pd.Timestamp(_idx)
                                # tz-naive → 假設 UTC；tz-aware → 直接 convert
                                if _ts.tzinfo is None:
                                    _ts = _ts.tz_localize('UTC')
                                _local_date = _ts.tz_convert(_local_tz_ned).date()
                                if _local_date >= _grace_past and pd.isna(ed.loc[_idx, col_actual]):
                                    _pending.append(pd.Timestamp(_local_date))
                            except Exception:
                                pass
                        if _pending:
                            _today_ts = pd.Timestamp(_today_local)
                            _ftr = [x for x in _pending if x >= _today_ts]
                            # 優先選最近的未來 pending；若全是過去 pending (grace 內)，
                            # 取最接近今日的那一筆（最晚）當作「即將發布」。
                            if _ftr:
                                next_earnings_date = min(_ftr).strftime('%Y-%m-%d')
                            else:
                                next_earnings_date = max(_pending).strftime('%Y-%m-%d')
                    except Exception:
                        pass
                print(f'    earnings_history: {len(earnings_history)} quarters, next_earnings_date: {next_earnings_date}', flush=True)
        except Exception as e:
            print(f'    earnings_dates error: {e}', flush=True)

        # ── 毛利率 / 營益率 ──────────────────────────────────────────
        gross_margin     = safe_float(info.get('grossMargins'))
        operating_margin = safe_float(info.get('operatingMargins'))

        # ── 券商目標價 ────────────────────────────────────────────────
        target_mean_price   = safe_float(info.get('targetMeanPrice'))
        target_low_price    = safe_float(info.get('targetLowPrice'))
        target_high_price   = safe_float(info.get('targetHighPrice'))
        target_median_price = safe_float(info.get('targetMedianPrice'))
        number_of_analysts  = info.get('numberOfAnalystOpinions')
        if isinstance(number_of_analysts, float) and (number_of_analysts != number_of_analysts):
            number_of_analysts = None  # NaN guard
        elif number_of_analysts is not None:
            try:
                number_of_analysts = int(number_of_analysts)
            except Exception:
                number_of_analysts = None

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
        # 優先從 history 取精確收盤（避免 regularMarketPreviousClose 在盤後
        # 仍指向「前一交易日」而非當日收盤的時序問題）
        prev_close = None
        prev_prev_close = None
        try:
            d10 = t.history(period='10d', interval='1d')
            if not d10.empty:
                closes10 = d10['Close'].dropna().tolist()

                # ── 去除 yfinance 盤後插入的幻影重複 bar ──
                # （未開盤時 API 常在尾端補一條與前日相同的 bar，
                #   若不去除會造成 prevClose == prevPrevClose → 昨日漲跌 0%）
                while len(closes10) >= 2 and closes10[-1] == closes10[-2]:
                    closes10.pop()

                # 依交易所選擇正確時區判斷今日是否有 K 棒
                last_bar_date = d10.index[-1]
                import pytz as _ptz
                if sym.endswith('.KS') or sym.endswith('.KQ'):
                    _tz_name = 'Asia/Seoul'
                elif sym.endswith('.HK'):
                    _tz_name = 'Asia/Hong_Kong'
                elif sym.endswith('.T') or sym.endswith('.TW') or sym.endswith('.TWO'):
                    _tz_name = 'Asia/Taipei'
                else:
                    _tz_name = 'America/New_York'
                _local_tz = _ptz.timezone(_tz_name)
                now_local_date = datetime.now(_ptz.utc).astimezone(_local_tz).date()
                try:
                    _ts = pd.Timestamp(last_bar_date)
                    if _ts.tzinfo is None:
                        _ts = _ts.tz_localize('UTC')
                    last_local_date = _ts.tz_convert(_local_tz).date()
                except Exception:
                    last_local_date = pd.Timestamp(last_bar_date).date()
                today_open = (last_local_date == now_local_date)

                if today_open:
                    # closes[-1]=今日進行中, [-2]=昨日收盤(prevClose), [-3]=前日(prevPrevClose)
                    if len(closes10) >= 2: prev_close      = safe_float(closes10[-2])
                    if len(closes10) >= 3: prev_prev_close = safe_float(closes10[-3])
                else:
                    # closes[-1]=最近收盤(prevClose), [-2]=前日(prevPrevClose)
                    if len(closes10) >= 1: prev_close      = safe_float(closes10[-1])
                    if len(closes10) >= 2: prev_prev_close = safe_float(closes10[-2])

                print(f'    prevClose={prev_close}  prevPrevClose={prev_prev_close}  today_open={today_open}', flush=True)
        except Exception:
            pass

        # 備援：history 失敗時才使用 info 欄位
        if not prev_close:
            prev_close = info.get('regularMarketPreviousClose') or info.get('previousClose')
            if not prev_close:
                try:
                    prev_close = getattr(t.fast_info, 'previous_close', None) or None
                except Exception:
                    pass

        result[sym] = {
            # 基本面（全部透過 safe_float 防止 NaN 寫入 JSON）
            'pe':               safe_float(info.get('trailingPE')),
            'fpe':              safe_float(info.get('forwardPE')),
            'peg':              safe_float(info.get('trailingPegRatio')),
            'ps':               safe_float(info.get('priceToSalesTrailing12Months')),
            'pb':               safe_float(info.get('priceToBook')),
            'rev_yoy':          safe_float(info.get('revenueGrowth')),
            'rev_fwd':          rev_fwd,
            'hist_avg_pe':      hist_avg_pe,
            'eps_ttm':          safe_float(info.get('trailingEps')),
            'eps_cur_q':        eps_cur_q,
            'eps_next_q2':      eps_next_q,
            'eps_cur_y':        safe_float(info.get('epsCurrentYear')),
            'eps_next_y':       safe_float(info.get('epsForward')),
            'gross_margin':        gross_margin,
            'operating_margin':    operating_margin,
            'target_mean_price':   target_mean_price,
            'target_low_price':    target_low_price,
            'target_high_price':   target_high_price,
            'target_median_price': target_median_price,
            'number_of_analysts':  number_of_analysts,
            'earnings_history':    earnings_history,   # 季度 EPS 歷史
            'next_earnings_date':  next_earnings_date, # 下次財報日期（YYYY-MM-DD，未知則 null）
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

# ── 財報日期手動覆蓋：EARNINGS_DATE_OVERRIDES > yfinance ────────────────────
for _ov_sym, _ov_date in EARNINGS_DATE_OVERRIDES.items():
    if _ov_sym in result and isinstance(result[_ov_sym], dict):
        _old = result[_ov_sym].get('next_earnings_date')
        result[_ov_sym]['next_earnings_date'] = _ov_date
        print(f'  [{_ov_sym}] next_earnings_date 手動覆蓋: {_old} → {_ov_date}', flush=True)

# ── 代理標的財報日期補充：從原型標的複製 next_earnings_date ──────────────────
for proxy_sym, underlying_sym in PROXY_EARNINGS_MAP.items():
    if (proxy_sym in result and underlying_sym in result
            and result[proxy_sym].get('next_earnings_date') is None
            and result[underlying_sym].get('next_earnings_date') is not None):
        result[proxy_sym]['next_earnings_date'] = result[underlying_sym]['next_earnings_date']
        print(f'  [{proxy_sym}] next_earnings_date 從 {underlying_sym} 補充: {result[proxy_sym]["next_earnings_date"]}', flush=True)

# ── 寫出 ──────────────────────────────────────────────────────────────────
output = {
    'generated': datetime.now(timezone.utc).isoformat(),
    'data': result
}

out_path = os.path.join(ROOT, 'fundamentals.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f'\nDone. {len(result)} symbols → fundamentals.json  ({datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")})')
