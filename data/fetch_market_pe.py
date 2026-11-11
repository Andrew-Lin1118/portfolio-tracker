# -*- coding: utf-8 -*-
"""
fetch_market_pe.py
==================
抓取 美股 / 台股 大盤本益比並產生 data/market_pe.json，供前端「總體經濟」分頁使用。

- 美股 S&P 500：multpl.com（trailing PE、Shiller CAPE、5 年歷史 → 用於本益比河流圖）
- Nasdaq：yfinance QQQ trailingPE（指數本身 yfinance 不提供，用 QQQ ETF 代理）
- 台股加權：yfinance 0050.TW trailingPE（指數本身 yfinance 不提供，用 0050.TW ETF 代理）
- 費城半導體：成分股由下而上加總（SOXQ 持股權重 × 各成分股 yfinance trailingPE / forwardPE，
  加權調和平均）。歷史 PE 不用「EPS 不變」反推（半導體盈餘週期性太強），改為每次執行累積快照。
- Forward PE：ETF 的 yfinance info 沒有 forwardPE，Nasdaq / 台股也改用成分股加總補上
  （Nasdaq 用 QQQ 持股、台股用 taiex_weights.json 的加權指數 Top30 權重）。

執行：
    python data/fetch_market_pe.py
排程建議：每日盤後一次（multpl.com 日更新）
"""

import os, sys, json, re, datetime
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(ROOT, 'market_pe.json')
# 成分股加總的最低權重覆蓋率（已取得估值的權重 / 持股總權重）；低於此值視為不可靠、不輸出
MIN_COVERAGE = 0.6
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; portfolio-tracker/1.0)'}

# ── 工具 ──────────────────────────────────────────────────────────────
def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='ignore')


def parse_multpl_current(html):
    """multpl 主頁的『Current ... is X.XX』數字。"""
    m = re.search(r'Current[^<]*?(\d+\.\d+)', html)
    return float(m.group(1)) if m else None


def parse_multpl_table(html, max_rows=None):
    """multpl 表格頁面的 <table id="datatable"> → [{'date': 'YYYY-MM-DD', 'value': float}, ...]"""
    out = []
    tab = re.search(r'<table[^>]*id="datatable"[^>]*>(.*?)</table>', html, re.S)
    if not tab:
        return out
    body = tab.group(1)
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', body, re.S)
    for r in rows:
        cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', r, re.S)
        if len(cells) < 2:
            continue
        date_text  = re.sub(r'<[^>]+>', '', cells[0]).strip()
        value_text = re.sub(r'<[^>]+>', '', cells[1]).strip()
        # 移除所有 HTML 實體（如 &#x2002; 會誤被當成 2002 數字）
        value_text = re.sub(r'&[#a-zA-Z0-9]+;', ' ', value_text)
        # value 可能含 † 註記 → 取最後一段純數字（且必須是小數，排除年份這類整數）
        m_val = re.search(r'(-?\d+\.\d+)', value_text)
        if not m_val:
            m_val = re.search(r'(-?\d+)', value_text)
        if not m_val:
            continue
        try:
            value = float(m_val.group(1))
        except ValueError:
            continue
        # 日期解析（"Apr 23, 2026" / "Mar 1, 2026"）
        try:
            d = datetime.datetime.strptime(date_text, '%b %d, %Y').date()
        except ValueError:
            continue
        out.append({'date': d.isoformat(), 'value': value})
        if max_rows and len(out) >= max_rows:
            break
    return out


# ── S&P 500（multpl.com）─────────────────────────────────────────────
def fetch_sp500():
    info = {'key': 'sp500', 'name': 'S&P 500', 'trailing_pe': None,
            'shiller_pe': None, 'history': []}
    try:
        info['trailing_pe'] = parse_multpl_current(http_get('https://www.multpl.com/s-p-500-pe-ratio'))
    except Exception as e:
        print(f'  [sp500] trailing PE 失敗: {e}', flush=True)
    try:
        info['shiller_pe']  = parse_multpl_current(http_get('https://www.multpl.com/shiller-pe'))
    except Exception as e:
        print(f'  [sp500] Shiller PE 失敗: {e}', flush=True)
    # 20 年歷史（每月一筆，約 240 筆）
    try:
        html = http_get('https://www.multpl.com/s-p-500-pe-ratio/table/by-month')
        rows = parse_multpl_table(html)
        cutoff = (datetime.date.today() - datetime.timedelta(days=20*365 + 30)).isoformat()
        rows20y = [r for r in rows if r['date'] >= cutoff]
        # 由舊到新排序
        rows20y.sort(key=lambda x: x['date'])
        info['history'] = [{'d': r['date'], 'pe': r['value']} for r in rows20y]
        print(f'  [sp500] history: {len(rows20y)} months', flush=True)
    except Exception as e:
        print(f'  [sp500] history 失敗: {e}', flush=True)
    return info


# ── 用「指數價格」+「ETF 當前 PE 當錨」反推歷史 PE ────────────────
def fetch_yf_index_with_etf_pe(index_symbol, pe_proxy_symbol, name, key, period='5y'):
    """
    用「指數本身」的歷史價格（避免 ETF 配息/拆股噪音），
    搭配「代理 ETF」當前 trailingPE 當錨點，反推歷史 PE。

    限制：仍假設 EPS 不變，最多回推 5 年（更久會因 EPS 成長而失真）。
    想看更早的真實 PE 歷史請用 S&P 500（multpl.com 有月度真實 EPS）。
    """
    info = {'key': key, 'name': name, 'symbol': index_symbol,
            'pe_proxy': pe_proxy_symbol,
            'trailing_pe': None, 'forward_pe': None, 'history': []}
    try:
        import yfinance as yf
        # 1) 從代理 ETF 拿當前 trailingPE / forwardPE
        proxy = yf.Ticker(pe_proxy_symbol)
        try:    pmeta = proxy.info or {}
        except Exception: pmeta = {}
        info['trailing_pe'] = pmeta.get('trailingPE')
        info['forward_pe']  = pmeta.get('forwardPE')
        cur_pe = info['trailing_pe']

        # 2) 從指數本身取歷史週線價格
        idx = yf.Ticker(index_symbol)
        hist = idx.history(period=period, interval='1wk', auto_adjust=False)
        if hist is None or hist.empty:
            print(f'  [{key}] 指數 {index_symbol} 無歷史資料', flush=True)
            return info
        if not (cur_pe and cur_pe > 0):
            print(f'  [{key}] 代理 ETF {pe_proxy_symbol} 無 trailingPE', flush=True)
            return info

        # 取「最後一個有效收盤」(避免當週未收盤的 NaN)
        close_series = hist['Close'].dropna()
        if close_series.empty:
            print(f'  [{key}] 指數收盤全為 NaN', flush=True)
            return info
        last_close = float(close_series.iloc[-1])
        eps_est = last_close / cur_pe
        if eps_est <= 0:
            return info

        pts = []
        for ts, row in hist.iterrows():
            c = float(row['Close']) if row['Close'] == row['Close'] else None
            if c is None: continue
            pts.append({'d': ts.date().isoformat(), 'pe': round(c / eps_est, 2)})
        # 抽稀為月線（每月最後一筆）
        by_month = {}
        for p in pts:
            ym = p['d'][:7]
            by_month[ym] = p
        info['history'] = sorted(by_month.values(), key=lambda x: x['d'])
    except Exception as e:
        print(f'  [{key}] yfinance 失敗: {e}', flush=True)
    return info


# ── 成分股由下而上加總（指數 PE / Forward PE）────────────────────────
def load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def to_yf_symbol(sym):
    """stockanalysis 的股別寫法（BRK.B）→ yfinance（BRK-B）；帶交易所後綴的（2330.TW）不動。"""
    if re.match(r'^[A-Z]+\.[A-Z]$', sym or ''):
        return sym.replace('.', '-')
    return sym


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float('inf') else None


def weights_from_etf(etf_symbol):
    """ETF 持股權重（stockanalysis.com；沿用 fetch_etf_holdings 的解析）。抓不到時退回 etf_holdings.json。"""
    try:
        sys.path.insert(0, ROOT)
        import fetch_etf_holdings as feh
        h = feh.fetch_etf(etf_symbol)
        if h and h.get('equity'):
            return [{'symbol': to_yf_symbol(e['symbol']), 'weight': e['weight']} for e in h['equity']]
    except Exception as e:
        print(f'  [{etf_symbol}] 持股抓取失敗: {e}', flush=True)
    cached = ((load_json(os.path.join(ROOT, 'etf_holdings.json')) or {}).get('etfs') or {}).get(etf_symbol)
    if cached and cached.get('equity'):
        print(f'  [{etf_symbol}] 改用 etf_holdings.json 既有持股', flush=True)
        return [{'symbol': to_yf_symbol(e['symbol']), 'weight': e['weight']} for e in cached['equity']]
    return []


def weights_from_taiex():
    """加權指數成分股權重（taiex_weights.json，TAIFEX Top30）。"""
    d = load_json(os.path.join(ROOT, 'taiex_weights.json')) or {}
    out = []
    for it in d.get('items') or []:
        w = _num(it.get('w'))
        if it.get('code') and w and w > 0:
            out.append({'symbol': f"{it['code']}.TW", 'weight': w})
    return out


def fetch_stock_valuation(symbol):
    """單一成分股的估值欄位（yfinance info）。失敗回傳 None。"""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
    except Exception as e:
        print(f'    [{symbol}] yfinance 失敗: {str(e)[:80]}', flush=True)
        return None
    cur, fin_cur = info.get('currency'), info.get('financialCurrency')
    return {
        'pe':    _num(info.get('trailingPE')),
        'fpe':   _num(info.get('forwardPE')),
        'price': _num(info.get('currentPrice') or info.get('regularMarketPrice') or info.get('previousClose')),
        'eps':   _num(info.get('trailingEps')),
        'feps':  _num(info.get('forwardEps')),
        # 報價幣別與財報幣別不同（ADR）時，EPS/股價 不可直接相除
        'same_currency': (not cur or not fin_cur or cur == fin_cur),
    }


def fetch_valuations(symbols):
    """併發抓多檔成分股估值；yfinance 沒資料的用 fundamentals.json 既有 pe/fpe 補。"""
    symbols = sorted(set(symbols))
    out = {}
    if symbols:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for sym, val in zip(symbols, ex.map(fetch_stock_valuation, symbols)):
                if val:
                    out[sym] = val
    fund = (load_json(os.path.join(ROOT, 'fundamentals.json')) or {}).get('data') or {}
    for sym in symbols:
        v = out.get(sym)
        if v and (v['pe'] or v['fpe'] or v['eps'] is not None):
            continue
        d = fund.get(sym) or fund.get(sym.replace('-', '.'))
        if d and (d.get('pe') or d.get('fpe')):
            out[sym] = {'pe': _num(d.get('pe')), 'fpe': _num(d.get('fpe')), 'price': None,
                        'eps': None, 'feps': None, 'same_currency': False}
    return out


def earnings_yield(val, forward=False):
    """
    個股盈餘殖利率（EPS / 股價）。
    優先用 Yahoo 自己的 PE（比率內部一致，ADR 也不會有幣別問題）；
    PE 缺值通常是虧損股 → 幣別一致時用 EPS/股價（可為負，讓虧損計入指數盈餘）。
    """
    if not val:
        return None
    pe  = val['fpe'] if forward else val['pe']
    eps = val['feps'] if forward else val['eps']
    if pe and pe > 0:
        return 1.0 / pe
    if eps is not None and val.get('price') and val['price'] > 0 and val.get('same_currency'):
        return eps / val['price']
    return None


def bottom_up_pe(weights, valuations):
    """
    指數 PE = Σw ÷ Σ(w × 盈餘殖利率)（加權調和平均，等同「總市值 ÷ 總盈餘」）。
    回傳 {'trailing_pe','forward_pe','coverage','forward_coverage','constituents'}；
    覆蓋率不足或總盈餘 ≤ 0 時該欄為 None。
    """
    total_w = sum(w['weight'] for w in weights)
    res = {'trailing_pe': None, 'forward_pe': None, 'coverage': 0.0,
           'forward_coverage': 0.0, 'constituents': len(weights)}
    if total_w <= 0:
        return res
    for field, cov_field, fwd in (('trailing_pe', 'coverage', False),
                                  ('forward_pe', 'forward_coverage', True)):
        cov_w = ey_sum = 0.0
        for w in weights:
            ey = earnings_yield(valuations.get(w['symbol']), forward=fwd)
            if ey is None:
                continue
            cov_w  += w['weight']
            ey_sum += w['weight'] * ey
        res[cov_field] = round(cov_w / total_w, 4)
        if cov_w / total_w >= MIN_COVERAGE and ey_sum > 0:
            res[field] = round(cov_w / ey_sum, 2)
    return res


def merge_snapshot(history, pe, today=None):
    """把今天的 PE 併入月度快照歷史（同月覆蓋為最新一筆），最多保留 20 年。"""
    history = [h for h in (history or []) if h.get('d') and h.get('pe') is not None]
    if pe is not None:
        d = (today or datetime.date.today()).isoformat()
        history = [h for h in history if h['d'][:7] != d[:7]]
        history.append({'d': d, 'pe': pe})
    history.sort(key=lambda x: x['d'])
    return history[-240:]


def build_sox(weights, valuations, prev):
    """費城半導體：trailing / forward 皆為成分股加總；歷史為逐次累積的真實快照。"""
    prev = prev or {}
    bu = bottom_up_pe(weights, valuations)
    info = {'key': 'sox', 'name': '費城半導體 (SOX)', 'symbol': '^SOX',
            'pe_proxy': 'SOXQ', 'pe_method': 'bottom_up', 'history_method': 'snapshot',
            'trailing_pe': bu['trailing_pe'], 'forward_pe': bu['forward_pe'],
            'coverage': bu['coverage'], 'forward_coverage': bu['forward_coverage'],
            'constituents': bu['constituents'],
            'history': merge_snapshot(prev.get('history'), bu['trailing_pe'])}
    # 本輪算不出來（資料源失敗）→ 沿用上一輪數值，避免前端閃回「暫無」
    for k in ('trailing_pe', 'forward_pe'):
        if info[k] is None and prev.get(k) is not None:
            info[k] = prev[k]
            info['stale'] = True
    return info


def fill_forward_pe(info, weights, valuations, prev):
    """ETF 代理拿不到 forwardPE 時，用成分股加總補上（並附上加總結果供對照）。"""
    bu = bottom_up_pe(weights, valuations)
    info['bottom_up'] = bu
    if info.get('forward_pe') is None:
        if bu['forward_pe'] is not None:
            info['forward_pe'] = bu['forward_pe']
            info['forward_pe_method'] = 'bottom_up'
        elif (prev or {}).get('forward_pe') is not None:
            info['forward_pe'] = prev['forward_pe']
            info['forward_pe_method'] = prev.get('forward_pe_method', 'bottom_up')
            info['forward_pe_stale'] = True
    return info


def main():
    print('開始抓取 大盤本益比 ...', flush=True)
    indexes = []

    print('S&P 500（multpl.com）...', flush=True)
    indexes.append(fetch_sp500())

    print('Nasdaq 100（^NDX 指數價 + QQQ trailingPE）...', flush=True)
    nasdaq = fetch_yf_index_with_etf_pe('^NDX', 'QQQ', 'Nasdaq 100 (NDX)', 'nasdaq')
    indexes.append(nasdaq)

    print('台股加權（^TWII 指數價 + 0050.TW trailingPE）...', flush=True)
    twii = fetch_yf_index_with_etf_pe('^TWII', '0050.TW', '台股加權 (TWII)', 'twii')
    indexes.append(twii)

    print('成分股加總（費半 PE/Forward PE；Nasdaq、台股 Forward PE）...', flush=True)
    prev = {i.get('key'): i for i in ((load_json(OUT_FILE) or {}).get('indexes') or [])}
    w_sox, w_ndx, w_tw = weights_from_etf('SOXQ'), weights_from_etf('QQQ'), weights_from_taiex()
    sox_proxy = 'SOXQ'
    if not w_sox:
        # SOXQ 抓不到 → 退回 etf_holdings.json 的 SOXX（ICE 半導體指數，成分股與費半高度重疊）
        w_sox, sox_proxy = weights_from_etf('SOXX'), 'SOXX'
    vals = fetch_valuations([w['symbol'] for w in w_sox + w_ndx + w_tw])
    fill_forward_pe(nasdaq, w_ndx, vals, prev.get('nasdaq'))
    fill_forward_pe(twii,   w_tw,  vals, prev.get('twii'))
    sox = build_sox(w_sox, vals, prev.get('sox'))
    sox['pe_proxy'] = sox_proxy
    indexes.append(sox)

    out = {
        'generated': datetime.datetime.now().isoformat(timespec='seconds'),
        'indexes': indexes,
    }
    tmp = OUT_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_FILE)

    for idx in indexes:
        pe   = idx.get('trailing_pe')
        fpe  = idx.get('forward_pe')
        hist = idx.get('history') or []
        cov  = idx.get('coverage') or (idx.get('bottom_up') or {}).get('forward_coverage')
        cov_txt = f' 成分股覆蓋={cov:.0%}' if cov else ''
        print(f"  {idx['key']:>7s} | trailing={pe} forward={fpe} history={len(hist)} pts{cov_txt}", flush=True)
    print(f'\n寫入 {OUT_FILE}', flush=True)


if __name__ == '__main__':
    main()
