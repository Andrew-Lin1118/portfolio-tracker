"""
GitHub Actions 基本面 + 技術面資料更新腳本
每 6 小時執行一次，把 symbols.json 中所有代碼的
  基本面（PE/FPE/PEG/PS/PB/EPS/Rev Growth）
  技術面（日/週/小時 KD、MACD、RSI）
寫入 fundamentals.json，供 GitHub Pages 直接讀取，確保兩端數字一致。
"""
import copy
import json, time, os
from datetime import datetime, timezone, timedelta

import yfinance as yf
import yfinance.cache as yf_cache
import pandas as pd

try:
    from curl_cffi import requests as curl_requests
    _YF_SESSION = curl_requests.Session(impersonate='chrome', verify=False)
except Exception:
    _YF_SESSION = None


def yf_ticker(symbol):
    if _YF_SESSION is not None:
        return yf.Ticker(symbol, session=_YF_SESSION)
    return yf.Ticker(symbol)

ROOT = os.path.dirname(os.path.abspath(__file__))
_YF_CACHE_DIR = os.path.join(ROOT, '.yfinance_cache')
try:
    os.makedirs(_YF_CACHE_DIR, exist_ok=True)
    yf_cache.set_cache_location(_YF_CACHE_DIR)
    if hasattr(yf, 'set_tz_cache_location'):
        yf.set_tz_cache_location(_YF_CACHE_DIR)
except Exception:
    pass

# Naver Finance 補資料模組（用於韓股 yfinance 滯後/漏季時補強）
try:
    from fetch_naver_earnings import (
        fetch_naver_eps_history,
        fetch_naver_fundamentals,
        fetch_naver_latest_earnings_disclosure,
        stock_code_from_yf_symbol,
    )
    NAVER_AVAILABLE = True
except ImportError:
    import sys as _sys
    _sys.path.insert(0, ROOT)
    try:
        from fetch_naver_earnings import (
            fetch_naver_eps_history,
            fetch_naver_fundamentals,
            fetch_naver_latest_earnings_disclosure,
            stock_code_from_yf_symbol,
        )
        NAVER_AVAILABLE = True
    except ImportError:
        NAVER_AVAILABLE = False

with open(os.path.join(ROOT, 'symbols.json'), encoding='utf-8') as f:
    symbols = json.load(f)

# ── 代理標的對應表：HK 掛牌工具 → 真實標的 ───────────────────────────────
# 用於補充 yfinance 無法取得 HK 代理標的財報日期的情況
# key = 代理代碼（symbols.json 中的代碼），value = 原型代碼（同在 symbols.json 中）
PROXY_EARNINGS_MAP = {
    '7709.HK': '000660.KS',   # 南韓 SK 海力士 ETF → 000660.KS
    '9747.HK': '005930.KS',   # 南韓三星電子 ETF  → 005930.KS
}

FUNDAMENTAL_ALIAS_MAP = {
    'GOOG': 'GOOGL',           # Alphabet A/C shares share the same fundamentals/EPS history
}

def _load_proxy_valuation_map():
    """讀 symbol_mapping.json，取得槓桿/代理商品 → 原型股對照。"""
    mapping_path = os.path.join(ROOT, 'symbol_mapping.json')
    proxy_map = dict(PROXY_EARNINGS_MAP)
    try:
        with open(mapping_path, encoding='utf-8') as f:
            mapping = json.load(f)
        for section in ('leveraged_etf', 'hk_proxy'):
            for proxy_sym, meta in (mapping.get(section) or {}).items():
                proto = meta.get('proto') if isinstance(meta, dict) else None
                if proto:
                    proxy_map[proxy_sym] = proto
    except Exception:
        pass
    return proxy_map

PROXY_VALUATION_MAP = _load_proxy_valuation_map()

# ── 財報日期手動覆蓋表（yfinance 時間戳偶有偏差，在此校正） ─────────────
# yfinance 對部份亞洲股（尤其韓股）的 earnings_dates 時間戳會用 EDT 清晨或
# 午後時段標記，換算 KST 後可能較實際發布日早 1 天。若已知下一季真實發布日
# 可在此指定 YYYY-MM-DD (交易所本地日期)，程式會以此覆寫 yfinance 回傳值。
# 每季發布完後建議更新或刪除對應條目。
EARNINGS_DATE_OVERRIDES = {
    # Q1 2026: SK Hynix 已於 2026-04-23 發布，yfinance 現已回傳正確的 Q2 日期，無需覆蓋
}


# ── 工具函數 ──────────────────────────────────────────────────────────────
EPS_HISTORY_OVERRIDES = {
    'AAOI': {
        '2026-05-07': {
            'eps_estimate': -0.05,
            'eps_actual': -0.07,
            'source': 'company_press_release_non_gaap',
        },
    },
    'WDC': {
        '2025-07-30': {
            'eps_estimate': 1.48,
            'eps_actual': 1.66,
            'source': 'company_press_release_non_gaap',
        },
        '2025-10-30': {
            'eps_estimate': 1.57,
            'eps_actual': 1.78,
            'source': 'company_press_release_non_gaap',
        },
        '2026-01-29': {
            'eps_estimate': 1.93,
            'eps_actual': 2.13,
            'source': 'company_press_release_non_gaap',
        },
        '2026-04-30': {
            'eps_estimate': 2.39,
            'eps_actual': 2.72,
            'source': 'company_press_release_non_gaap',
        },
    },
}


def safe_float(v, decimals=4):
    """安全轉 float；NaN/None 回傳 None"""
    try:
        f = float(v)
        if f != f:      # NaN
            return None
        return round(f, decimals)
    except Exception:
        return None


def trailing_eps_from_history(history, count=4):
    vals = []
    for item in reversed(history or []):
        if not isinstance(item, dict):
            continue
        v = safe_float(item.get('eps_actual'))
        if v is not None:
            vals.append(v)
            if len(vals) >= count:
                break
    if len(vals) < count:
        return None
    return safe_float(sum(vals))


def apply_eps_history_overrides(sym, history):
    overrides = EPS_HISTORY_OVERRIDES.get(sym.upper())
    if not overrides:
        return
    for item in history or []:
        if not isinstance(item, dict):
            continue
        override = overrides.get(item.get('report_date'))
        if not override:
            continue
        old_actual = item.get('eps_actual')
        old_est = item.get('eps_estimate')
        item['eps_actual'] = safe_float(override.get('eps_actual'))
        item['eps_estimate'] = safe_float(override.get('eps_estimate'))
        est = item.get('eps_estimate')
        actual = item.get('eps_actual')
        item['surprise_pct'] = safe_float((actual - est) / abs(est)) if est not in (None, 0) and actual is not None else None
        item['eps_basis'] = 'non_gaap'
        item['eps_override_source'] = override.get('source')
        print(
            f'    EPS override {sym} {item.get("report_date")}: '
            f'actual {old_actual} -> {item.get("eps_actual")}, '
            f'est {old_est} -> {item.get("eps_estimate")}',
            flush=True,
        )


def derive_pe_from_eps(price, eps_ttm):
    p = safe_float(price)
    e = safe_float(eps_ttm)
    if p is None or e is None or p <= 0 or e <= 0:
        return None
    return safe_float(p / e)


def derive_peg_from_eps_forecast(sym_data):
    """
    PEG fallback：Yahoo trailingPegRatio 缺值時，以「預期本益比 / 明年 EPS 成長率(%)」推導。
    只在 current/next-year EPS 都為正且明年 EPS 成長為正時使用；虧轉盈或負成長不硬算。
    """
    eps_cur_y = safe_float(sym_data.get('eps_cur_y'))
    eps_next_y = safe_float(sym_data.get('eps_next_y'))
    if eps_cur_y is None or eps_next_y is None or eps_cur_y <= 0 or eps_next_y <= eps_cur_y:
        return None, None

    growth_pct = (eps_next_y - eps_cur_y) / eps_cur_y * 100.0
    if growth_pct <= 0:
        return None, None

    base_pe = safe_float(sym_data.get('fpe')) or safe_float(sym_data.get('pe'))
    if base_pe is None or base_pe <= 0:
        return None, None

    return safe_float(base_pe / growth_pct), safe_float(growth_pct, 2)


def fill_missing_peg_values(result_map):
    """
    補 PEG：
      1) 個股：Yahoo PEG 缺值時，用 EPS 今年/明年預估推導。
      2) 槓桿/代理商品：本身通常沒有 PEG，沿用原型股 PEG。
    """
    filled = []

    for sym, sym_data in result_map.items():
        if not isinstance(sym_data, dict) or sym_data.get('error') or sym_data.get('peg') is not None:
            continue
        peg, growth_pct = derive_peg_from_eps_forecast(sym_data)
        if peg is None:
            continue
        sym_data['peg'] = peg
        sym_data['peg_source'] = 'derived_eps_yoy_fpe'
        sym_data['peg_growth_pct'] = growth_pct
        filled.append(f'{sym}={peg} (EPS +{growth_pct}%)')

    for proxy_sym, proto_sym in PROXY_VALUATION_MAP.items():
        proxy = result_map.get(proxy_sym)
        proto = result_map.get(proto_sym)
        if not isinstance(proxy, dict) or not isinstance(proto, dict):
            continue
        if proxy.get('peg') is not None or proto.get('peg') is None:
            continue
        proxy['peg'] = proto.get('peg')
        proxy['peg_source'] = 'underlying_proxy'
        proxy['peg_underlying'] = proto_sym
        if proto.get('peg_source'):
            proxy['peg_underlying_source'] = proto.get('peg_source')
        filled.append(f'{proxy_sym}={proxy["peg"]} (from {proto_sym})')

    if filled:
        print('  PEG fallback filled: ' + ', '.join(filled), flush=True)
    else:
        print('  PEG fallback: no additional values', flush=True)


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
# 並行抓取：所有標的同時送 yfinance 請求（避免每天 GitHub Actions 一檔抓 3-5 秒
# × 42 檔 ≈ 3 分鐘的延遲，導致剛公布的財報要等下次排程才更新）
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
result = {}
_print_lock = threading.Lock()  # 確保多 thread 印 log 不互相蓋掉同一行

def _safe_print(msg):
    with _print_lock:
        print(msg, flush=True)

def _process_symbol(sym):
    _safe_print(f'  fetching {sym}...')
    try:
        t    = yf_ticker(sym)
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
                                'report_date':  dt.strftime('%Y-%m-%d'),
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

        apply_eps_history_overrides(sym, earnings_history)
        eps_ttm_from_history = trailing_eps_from_history(earnings_history)
        eps_ttm_value = eps_ttm_from_history or safe_float(info.get('trailingEps'))
        pe_value = safe_float(info.get('trailingPE')) or derive_pe_from_eps(prev_close, eps_ttm_value)

        result[sym] = {
            # 基本面（全部透過 safe_float 防止 NaN 寫入 JSON）
            'pe':               pe_value,
            'fpe':              safe_float(info.get('forwardPE')),
            'peg':              safe_float(info.get('trailingPegRatio')),
            'ps':               safe_float(info.get('priceToSalesTrailing12Months')),
            'pb':               safe_float(info.get('priceToBook')),
            'rev_yoy':          safe_float(info.get('revenueGrowth')),
            'rev_fwd':          rev_fwd,
            'hist_avg_pe':      hist_avg_pe,
            'eps_ttm':          eps_ttm_value,
            'eps_cur_q':        eps_cur_q,
            'eps_next_q2':      eps_next_q,
            'eps_cur_y':        safe_float(info.get('epsCurrentYear')),
            'eps_next_y':       safe_float(info.get('epsForward')),
            'eps_ttm_provider':  'earnings_history' if eps_ttm_from_history is not None else 'yahoo_trailingEps',
            'eps_ttm_yahoo':     safe_float(info.get('trailingEps')),
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
        _safe_print(f'    [{sym}] ERROR: {e}')
        result[sym] = {'error': str(e)}


# ── 平行執行所有標的（max_workers=6 為 yfinance 可承受區間，~30 秒完成 42 檔）──
print(f'\n[parallel] 並行抓取 {len(symbols)} 個標的（max_workers=6）...', flush=True)
_t_start = time.time()
with ThreadPoolExecutor(max_workers=6) as _pool:
    _futs = {_pool.submit(_process_symbol, sym): sym for sym in symbols}
    _completed = 0
    for _fut in as_completed(_futs):
        _completed += 1
        try:
            _fut.result()
        except Exception as e:
            _safe_print(f'    [parallel] worker exception: {e}')
print(f'[parallel] 完成 {_completed}/{len(symbols)} 標的，耗時 {time.time()-_t_start:.1f} 秒\n', flush=True)


# ── Naver Finance 補資料：韓股 yfinance 滯後或缺季時補強 ─────────────────
# 規則：
#   1. yfinance 已有的季度（依 'quarter' 標籤匹配）→ 用 Naver 的 eps_actual 覆蓋
#      （使用者選擇 Naver 為準）；保留 yfinance 的 eps_estimate / report_date。
#   2. yfinance 沒有但 Naver 有 actual 的季度 → 新增到 earnings_history。
#   3. 若所有 entries 都有 estimate + actual → 重算 surprise_pct。
#   4. Naver disclosure 最近一筆「실적」公告日期 → 若 yfinance next_earnings_date
#      仍 ≤ 此日期，視為 yfinance 滯後，依 Naver 加 90 天推估下次。
def _merge_naver_for_korean_stock(yf_sym, sym_data):
    code = stock_code_from_yf_symbol(yf_sym)
    if not code:
        return
    try:
        naver_rows = fetch_naver_eps_history(code, include_consensus=False)
    except Exception as e:
        print(f'  [{yf_sym}] Naver 抓 EPS 失敗（跳過）: {e}', flush=True)
        return
    if not naver_rows:
        return

    history = list(sym_data.get('earnings_history') or [])
    by_quarter = {h.get('quarter'): h for h in history}

    added = 0
    overridden = 0
    for nv in naver_rows:
        q = nv['quarter']
        if q in by_quarter:
            existing = by_quarter[q]
            old_actual = existing.get('eps_actual')
            new_actual = nv['eps_actual']
            if new_actual is not None and old_actual != new_actual:
                existing['eps_actual'] = new_actual
                est = existing.get('eps_estimate')
                if est not in (None, 0):
                    existing['surprise_pct'] = round((new_actual - est) / abs(est), 4)
                overridden += 1
        else:
            est = nv.get('eps_estimate')
            actual = nv.get('eps_actual')
            surprise = None
            if actual is not None and est not in (None, 0):
                surprise = round((actual - est) / abs(est), 4)
            history.append({
                'quarter':      q,
                'report_date':  nv['report_date'],
                'eps_estimate': est,
                'eps_actual':   actual,
                'surprise_pct': surprise,
            })
            added += 1

    history.sort(key=lambda h: h.get('report_date') or '')
    sym_data['earnings_history'] = history
    if added or overridden:
        print(f'  [{yf_sym}] Naver merge: +{added} 季新增 / {overridden} 季覆蓋', flush=True)

    # next_earnings_date 校正：Naver disclosure 顯示最近一次「實績」日期
    try:
        naver_fund = fetch_naver_fundamentals(code)
    except Exception as e:
        naver_fund = {}
        print(f'  [{yf_sym}] Naver fundamentals FAIL: {e}', flush=True)

    if naver_fund:
        replaced = []
        for field in ('pe', 'pb', 'fpe', 'eps_next_q2'):
            value = safe_float(naver_fund.get(field))
            if value is not None:
                old = sym_data.get(field)
                sym_data[field] = value
                if old != value:
                    replaced.append(field)

        # Naver usually exposes one annual consensus year. Keep Yahoo's
        # current-year value if it exists, but use the Naver consensus to
        # fill the forward/next-year slot that Yahoo often leaves blank.
        eps_cur_y = safe_float(naver_fund.get('eps_cur_y'))
        if eps_cur_y is not None:
            old = sym_data.get('eps_cur_y')
            sym_data['eps_cur_y'] = eps_cur_y
            if old != eps_cur_y:
                replaced.append('eps_cur_y')

        eps_next_y = safe_float(naver_fund.get('eps_next_y'))
        if eps_next_y is not None:
            old = sym_data.get('eps_next_y')
            sym_data['eps_next_y'] = eps_next_y
            if old != eps_next_y:
                replaced.append('eps_next_y')

        if replaced:
            sym_data['fundamental_source'] = 'naver'
            sym_data['naver_code'] = code
            if 'eps_ttm' in replaced:
                sym_data['eps_ttm_provider'] = 'naver'
            if naver_fund.get('eps_estimate_period'):
                sym_data['naver_eps_estimate_period'] = naver_fund.get('eps_estimate_period')
            print(f'  [{yf_sym}] Naver fundamentals filled: {", ".join(replaced)}', flush=True)

    try:
        latest_disc, title = fetch_naver_latest_earnings_disclosure(code)
    except Exception as e:
        latest_disc = None
        print(f'  [{yf_sym}] Naver disclosure 抓取失敗（跳過 next_earnings 校正）: {e}', flush=True)
    if latest_disc:
        cur_next = sym_data.get('next_earnings_date')
        # 若 yfinance 的 next 已過去（≤ Naver 最近實績日），加 ~90 天推下一季
        if cur_next and cur_next <= latest_disc:
            try:
                base = datetime.strptime(latest_disc, '%Y-%m-%d').date()
                projected = (base + timedelta(days=90)).strftime('%Y-%m-%d')
                sym_data['next_earnings_date'] = projected
                print(f'  [{yf_sym}] next_earnings_date 校正: {cur_next} → {projected} '
                      f'(Naver 最近實績 {latest_disc})', flush=True)
            except ValueError:
                pass

if NAVER_AVAILABLE:
    print('\n[Naver] 韓股補資料 ───────────────────────────', flush=True)
    for _yf_sym in list(result.keys()):
        if isinstance(result[_yf_sym], dict) and _yf_sym.upper().endswith('.KS'):
            _merge_naver_for_korean_stock(_yf_sym, result[_yf_sym])
            time.sleep(0.4)  # 對 Naver 客氣一點
else:
    print('\n[Naver] fetch_naver_earnings 模組未載入，跳過韓股補資料', flush=True)


print('\n[PEG] 缺值補齊 ───────────────────────────────', flush=True)
fill_missing_peg_values(result)


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
def _fund_entry_useful(d):
    if not isinstance(d, dict) or d.get('error'):
        return False
    keys = (
        'pe', 'fpe', 'peg', 'ps', 'pb', 'hist_avg_pe',
        'eps_ttm', 'eps_cur_q', 'eps_cur_y', 'eps_next_y',
        'daily_k', 'weekly_k', 'hourly_k',
        'daily_macd', 'weekly_macd', 'hourly_macd',
        'daily_rsi', 'weekly_rsi', 'hourly_rsi',
        'prev_close', 'prev_prev_close', 'next_earnings_date',
    )
    if any(d.get(k) not in (None, '', '-') for k in keys):
        return True
    return bool(d.get('earnings_history'))

def _json_safe(v):
    if isinstance(v, dict):
        return {k: _json_safe(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_json_safe(item) for item in v]
    if isinstance(v, float) and (v != v or v in (float('inf'), float('-inf'))):
        return None
    return v

for alias_sym, source_sym in FUNDAMENTAL_ALIAS_MAP.items():
    if alias_sym not in symbols:
        continue
    source = result.get(source_sym)
    alias = result.get(alias_sym)
    if not _fund_entry_useful(source):
        continue
    if _fund_entry_useful(alias):
        continue
    cloned = copy.deepcopy(source)
    cloned['symbol'] = alias_sym
    cloned['fundamental_alias_source'] = source_sym
    result[alias_sym] = cloned
    print(f'  [{alias_sym}] fundamentals filled from {source_sym}', flush=True)

out_path = os.path.join(ROOT, 'fundamentals.json')
old_data = {}
old_useful = 0
if os.path.exists(out_path):
    try:
        with open(out_path, encoding='utf-8') as f:
            old_data = json.load(f).get('data', {})
        old_useful = sum(1 for v in old_data.values() if _fund_entry_useful(v))
    except Exception:
        old_data = {}
        old_useful = 0

preserved = []
for sym in symbols:
    if _fund_entry_useful(result.get(sym)):
        continue
    old_entry = old_data.get(sym)
    if _fund_entry_useful(old_entry):
        result[sym] = old_entry
        result[sym]['preserved_from_previous'] = True
        preserved.append(sym)
if preserved:
    print(f'  preserved previous useful fundamentals for: {", ".join(preserved)}', flush=True)

new_useful = sum(1 for v in result.values() if _fund_entry_useful(v))

min_useful = max(5, int(max(len(result), len(symbols)) * 0.25))
if old_useful and new_useful < max(min_useful, int(old_useful * 0.60)):
    raise RuntimeError(
        f'Abort fundamentals.json write: only {new_useful} useful rows '
        f'(previous {old_useful}). This usually means the data provider failed.'
    )
if not old_useful and len(result) >= 10 and new_useful < min_useful:
    raise RuntimeError(
        f'Abort fundamentals.json write: only {new_useful} useful rows out of {len(result)}.'
    )

output = {
    'generated': datetime.now(timezone.utc).isoformat(),
    'data': _json_safe(result)
}

with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2, allow_nan=False)

print(f'\nDone. {len(result)} symbols → fundamentals.json  ({datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")})')

# ── 順便更新大盤本益比（市場本益比/河流圖資料；fetch_market_pe.py 獨立可運行）──
try:
    print('\n更新大盤本益比 (market_pe.json) ...', flush=True)
    import subprocess, sys as _sys
    subprocess.run([_sys.executable, os.path.join(ROOT, 'fetch_market_pe.py')], check=False, timeout=180)
except Exception as _e:
    print(f'  [market_pe] 更新失敗（不影響 fundamentals）：{_e}', flush=True)

# ── 順便更新 FRED 經濟指標（失業率/CPI/PCE/PPI；fetch_economic.py 獨立可運行）──
try:
    print('\n更新經濟指標 (economic_data.json) ...', flush=True)
    import subprocess as _sp, sys as _sy
    # timeout 放寬到 300s（6 個 FRED × 12s × 3 retry = 上限 ~216s，留 buffer）
    _sp.run([_sy.executable, os.path.join(ROOT, 'fetch_economic.py')], check=False, timeout=300)
except Exception as _e:
    print(f'  [economic_data] 更新失敗（不影響 fundamentals）：{_e}', flush=True)

# ── 順便更新 FedWatch（CME Fed Funds 期貨隱含的 FOMC 利率機率分布）──
try:
    print('\n更新 FedWatch (fedwatch.json) ...', flush=True)
    import subprocess as _sp_fw, sys as _sy_fw
    _sp_fw.run([_sy_fw.executable, os.path.join(ROOT, 'fetch_fedwatch.py')], check=False, timeout=120)
except Exception as _e:
    print(f'  [fedwatch] 更新失敗（不影響 fundamentals）：{_e}', flush=True)

# ── 順便更新財報深度分析（針對近 90 天有發財報的標的）──
try:
    print('\n更新財報深度分析 (earnings_analysis.json) ...', flush=True)
    import subprocess as _sp2, sys as _sy2
    # 每檔 yfinance call ~3s，假設 30 檔上限 → 90s + buffer
    _sp2.run([_sy2.executable, os.path.join(ROOT, 'fetch_earnings_deep.py')], check=False, timeout=600)
except Exception as _e:
    print(f'  [earnings_deep] 更新失敗（不影響 fundamentals）：{_e}', flush=True)

# ── 順便更新 ETF 持股（USD swap + 直接持股、SOXX/SMH/XSD/PSI 對照）──
try:
    print('\n更新 ETF 持股 (etf_holdings.json) ...', flush=True)
    import subprocess as _sp3, sys as _sy3
    _sp3.run([_sy3.executable, os.path.join(ROOT, 'fetch_etf_holdings.py')], check=False, timeout=180)
except Exception as _e:
    print(f'  [etf_holdings] 更新失敗（不影響 fundamentals）：{_e}', flush=True)

# ── 順便更新台指成分股權重（taiex_weights.json；期貨持倉曝險回推用）──
try:
    print('\n更新台指成分股權重 (taiex_weights.json) ...', flush=True)
    import subprocess as _sp4, sys as _sy4
    _sp4.run([_sy4.executable, os.path.join(ROOT, 'fetch_taiex_weights.py')], check=False, timeout=60)
except Exception as _e:
    print(f'  [taiex_weights] 更新失敗（不影響 fundamentals）：{_e}', flush=True)
