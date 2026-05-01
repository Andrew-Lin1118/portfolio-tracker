# -*- coding: utf-8 -*-
"""
fetch_stockanalysis.py — Auto-fallback quarterly financials from stockanalysis.com
====================================================================================
當 yfinance 的 quarterly_income_stmt 還沒有最新一季資料時 (例如剛公布財報的 1-3 天內),
從 stockanalysis.com 抓資料補進來。

stockanalysis.com 聚合 SEC 8-K / 10-Q,通常 T+0 ~ T+1 就有最新一季數字,免 API key。

主要 entry point:
    fetch_quarterly_income(symbol) → {period_str: {revenue, gross_profit, ..., eps_diluted}}
    fetch_quarterly_cashflow(symbol) → {period_str: {operating_cash_flow, free_cash_flow}}

數字單位 = 百萬美元 (millions),呼叫端要自行 ×1e6 還原成元 (跟 yfinance 介面一致)。
"""
import urllib.request, urllib.error, re, ssl
from bs4 import BeautifulSoup

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# stockanalysis.com 表格列標籤 → 內部欄位
INCOME_LABEL_MAP = {
    'Revenue':            'revenue',
    'Gross Profit':       'gross_profit',
    'Operating Income':   'operating_income',
    'EBIT':               'ebit',
    'Net Income':         'net_income',
    'EPS (Diluted)':      'eps_diluted',
    'EPS (Basic)':        'eps_basic',
}
CASHFLOW_LABEL_MAP = {
    'Operating Cash Flow': 'operating_cash_flow',
    'Free Cash Flow':      'free_cash_flow',
    'Capital Expenditures':'capital_expenditures',
}

_MONTHS = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
           'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}


def _to_num(s):
    """Parse stockanalysis cell text to float; '(123.4)' → -123.4; '-' / 'N/A' → None"""
    s = (s or '').strip().replace(',', '').replace('$', '')
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    if s.endswith('%'):
        s = s[:-1]
    if s in ('', '-', 'N/A', 'Upgrade'):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_period_cell(txt):
    """\"Apr '26Apr 3, 2026\" → \"2026-04-03\""""
    if not txt:
        return None
    m = re.search(r'([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})', txt)
    if not m:
        return None
    mo = _MONTHS.get(m.group(1))
    if not mo:
        return None
    return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}"


def _http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'})
    try:
        return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise


def _parse_financials_table(html, label_map):
    """Generic: parse stockanalysis quarterly statement table → {period: {field: val}}"""
    if not html:
        return {}
    soup = BeautifulSoup(html, 'html.parser')
    tbl = soup.find('table')
    if not tbl:
        return {}

    dates = None
    label_rows = {}
    for r in tbl.find_all('tr'):
        cells = r.find_all(['th', 'td'])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True)
        if label == 'Period Ending':
            dates = [_parse_period_cell(c.get_text(strip=True)) for c in cells[1:]]
        elif label in label_map:
            label_rows[label_map[label]] = [_to_num(c.get_text(strip=True)) for c in cells[1:]]

    if not dates:
        return {}

    out = {}
    for i, d in enumerate(dates):
        if not d:
            continue
        out[d] = {k: (vals[i] if i < len(vals) else None) for k, vals in label_rows.items()}
    return out


def fetch_quarterly_income(symbol):
    """{period: {revenue, gross_profit, operating_income, net_income, eps_diluted, ...}}
    (數字單位 = 百萬美元;呼叫端負責 ×1e6 還原)"""
    url = f'https://stockanalysis.com/stocks/{symbol.lower()}/financials/?p=quarterly'
    html = _http_get(url)
    return _parse_financials_table(html, INCOME_LABEL_MAP)


def fetch_quarterly_cashflow(symbol):
    """{period: {operating_cash_flow, free_cash_flow, ...}}
    (數字單位 = 百萬美元)"""
    url = f'https://stockanalysis.com/stocks/{symbol.lower()}/financials/cash-flow-statement/?p=quarterly'
    html = _http_get(url)
    return _parse_financials_table(html, CASHFLOW_LABEL_MAP)


def fetch_quarterly_combined(symbol, include_cashflow=True):
    """同時抓 income + cashflow,合併同一 period 的兩邊欄位"""
    inc = fetch_quarterly_income(symbol) or {}
    if not include_cashflow:
        return inc
    cf = fetch_quarterly_cashflow(symbol) or {}
    out = {}
    for d in set(list(inc.keys()) + list(cf.keys())):
        merged = {}
        merged.update(inc.get(d, {}))
        merged.update(cf.get(d, {}))
        out[d] = merged
    return out


# ------------------------------------------------------------------
# self-test:python data/fetch_stockanalysis.py SNDK WDC
# ------------------------------------------------------------------
if __name__ == '__main__':
    import sys, json
    syms = sys.argv[1:] or ['SNDK', 'WDC', 'NBIS']
    for s in syms:
        print(f'\n=== {s} ===', flush=True)
        try:
            q = fetch_quarterly_combined(s)
            if not q:
                print('  no data', flush=True)
                continue
            for d in sorted(q.keys(), reverse=True)[:3]:
                print(f'  {d}: {json.dumps(q[d], ensure_ascii=False)}', flush=True)
        except Exception as e:
            print(f'  ERROR {type(e).__name__}: {e}', flush=True)
