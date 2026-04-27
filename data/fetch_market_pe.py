# -*- coding: utf-8 -*-
"""
fetch_market_pe.py
==================
抓取 美股 / 台股 大盤本益比並產生 data/market_pe.json，供前端「總體經濟」分頁使用。

- 美股 S&P 500：multpl.com（trailing PE、Shiller CAPE、5 年歷史 → 用於本益比河流圖）
- Nasdaq：yfinance QQQ trailingPE（指數本身 yfinance 不提供，用 QQQ ETF 代理）
- 台股加權：yfinance 0050.TW trailingPE（指數本身 yfinance 不提供，用 0050.TW ETF 代理）

執行：
    python data/fetch_market_pe.py
排程建議：每日盤後一次（multpl.com 日更新）
"""

import os, sys, json, re, datetime
import urllib.request
import urllib.error

OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'market_pe.json')
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


# ── 透過 yfinance 取單一 ETF 的 trailingPE / forwardPE ───────────────
def fetch_yf_etf(symbol, name, key):
    info = {'key': key, 'name': name, 'symbol': symbol,
            'trailing_pe': None, 'forward_pe': None, 'history': []}
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        meta = {}
        try:    meta = t.info or {}
        except Exception: pass
        info['trailing_pe'] = meta.get('trailingPE')
        info['forward_pe']  = meta.get('forwardPE')
        # 取 20 年週線 → 推估歷史 PE（以 current_eps = price/PE 推算）
        try:
            hist = t.history(period='20y', interval='1wk', auto_adjust=False)
            cur_pe = info['trailing_pe']
            if hist is not None and not hist.empty and cur_pe and cur_pe > 0:
                # 取「最後一個有效收盤」(避免當週未收盤的 NaN，台股盤中常見)
                close_series = hist['Close'].dropna()
                if close_series.empty:
                    raise RuntimeError('全部收盤皆 NaN')
                last_close = float(close_series.iloc[-1])
                eps_est = last_close / cur_pe
                if eps_est > 0:
                    pts = []
                    for ts, row in hist.iterrows():
                        c = float(row['Close']) if row['Close'] == row['Close'] else None
                        if c is None: continue
                        pts.append({'d': ts.date().isoformat(), 'pe': round(c / eps_est, 2)})
                    # 抽稀為月線（每月最後一筆）
                    by_month = {}
                    for p in pts:
                        ym = p['d'][:7]
                        by_month[ym] = p  # 後面的覆蓋前面 → 最後一筆
                    info['history'] = sorted(by_month.values(), key=lambda x: x['d'])
        except Exception as e:
            print(f'  [{key}] history 失敗: {e}', flush=True)
    except Exception as e:
        print(f'  [{key}] yfinance 失敗: {e}', flush=True)
    return info


def main():
    print('開始抓取 大盤本益比 ...', flush=True)
    indexes = []

    print('S&P 500（multpl.com）...', flush=True)
    indexes.append(fetch_sp500())

    print('Nasdaq 100（QQQ via yfinance）...', flush=True)
    indexes.append(fetch_yf_etf('QQQ', 'Nasdaq 100 (QQQ)', 'nasdaq'))

    print('台股加權（0050.TW via yfinance）...', flush=True)
    indexes.append(fetch_yf_etf('0050.TW', '台股加權 (0050.TW)', 'twii'))

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
        print(f"  {idx['key']:>7s} | trailing={pe} forward={fpe} history={len(hist)} pts", flush=True)
    print(f'\n寫入 {OUT_FILE}', flush=True)


if __name__ == '__main__':
    main()
