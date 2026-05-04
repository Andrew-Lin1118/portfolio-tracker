"""
Naver Finance 韓股財報資料抓取（補 yfinance 漏的最新季度）

提供兩個對外函式給 update_fund.py：
  - fetch_naver_eps_history(stock_code) → list of dicts (quarter, report_date, eps_actual, eps_estimate, is_consensus, fiscal_period)
  - fetch_naver_latest_earnings_disclosure(stock_code) → 'YYYY-MM-DD' or None

stock_code 格式：'005930'（去掉 .KS 後綴）
"""
import urllib.request
import json
import re
import sys
from datetime import date, datetime, timedelta

NAVER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0',
    'Accept': 'application/json',
    'Accept-Language': 'ko,en;q=0.9',
    'Referer': 'https://m.stock.naver.com/',
}

# 業績類 disclosure 關鍵字（包含「실적」即可）
_EARNINGS_DISCLOSURE_KEYWORDS = ('실적', '잠정실적', '분기보고서', '사업보고서')


def _http_json(url, timeout=15):
    req = urllib.request.Request(url, headers=NAVER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _parse_naver_number(s):
    """ '1,783' / '5,298.5' / '-2,500' → float；空字串/非數字 → None """
    if s is None:
        return None
    s = str(s).strip()
    if not s or s in ('-', 'N/A'):
        return None
    try:
        return float(s.replace(',', ''))
    except (ValueError, TypeError):
        return None


def _fiscal_to_report_quarter(fiscal_yyyymm):
    """
    Naver fiscal period (YYYYMM, e.g. '202412') → (yfinance-style label, 估算 report_date)

    Naver 用會計期間結束月份標示季度（12=Q4, 03=Q1, 06=Q2, 09=Q3）；
    韓國公司財報通常在會計期間結束後 30–60 天公布，落在下一個 calendar quarter。
    yfinance 既有 fundamentals.json 的 'quarter' 欄位是依「公布所在 calendar quarter」命名。

    例：
      202412 (2024 fiscal Q4 ends Dec) → 公布在 2025/1 → label '2025 Q1', date '2025-01-30'
      202503 (2025 fiscal Q1 ends Mar) → 公布在 2025/4 → label '2025 Q2', date '2025-04-30'
      202506 (2025 fiscal Q2 ends Jun) → 公布在 2025/7 → label '2025 Q3', date '2025-07-30'
      202509 (2025 fiscal Q3 ends Sep) → 公布在 2025/10 → label '2025 Q4', date '2025-10-30'
    """
    if not fiscal_yyyymm or len(fiscal_yyyymm) != 6:
        return None, None
    try:
        y = int(fiscal_yyyymm[:4])
        m = int(fiscal_yyyymm[4:])
    except ValueError:
        return None, None

    if m == 12:
        rep_y, rep_q, rep_month, rep_day = y + 1, 1, 1, 30   # Jan ~30 (三星歷年都 1/29-30)
    elif m == 3:
        rep_y, rep_q, rep_month, rep_day = y, 2, 4, 30
    elif m == 6:
        rep_y, rep_q, rep_month, rep_day = y, 3, 7, 30
    elif m == 9:
        rep_y, rep_q, rep_month, rep_day = y, 4, 10, 30
    else:
        return None, None

    return f'{rep_y} Q{rep_q}', f'{rep_y}-{rep_month:02d}-{rep_day:02d}'


def fetch_naver_eps_history(stock_code, *, include_consensus=False):
    """
    取 Naver 季度 EPS history。

    回傳 list of dict：
      {
        'quarter':       '2025 Q4'          # yfinance 格式
        'report_date':   '2025-10-30'       # 估算（fiscal_end + ~30 天）
        'eps_actual':    1783.0 or None     # 已公布才有
        'eps_estimate':  None or 5298.0     # consensus 才有
        'surprise_pct':  None               # Naver 不直接給；上層自行算
        'is_consensus':  False              # True = 尚未公布的分析師估值
        'fiscal_period': '202509'           # Naver 原始 key（debug 用）
      }

    依 report_date 由舊到新排序。include_consensus=False 時剔除尚未公布的 row。
    若 API 失敗回 []。
    """
    url = f'https://m.stock.naver.com/api/stock/{stock_code}/finance/quarter'
    try:
        data = _http_json(url)
    except Exception as e:
        print(f'    [Naver] {stock_code} finance/quarter FAIL: {e}', flush=True)
        return []

    fi = data.get('financeInfo')
    if not fi:
        return []

    headers = fi.get('trTitleList', [])
    consensus_map = {h.get('key'): (h.get('isConsensus') == 'Y') for h in headers}

    eps_row = next((r for r in fi.get('rowList', []) if r.get('title') == 'EPS'), None)
    if not eps_row:
        return []

    columns = eps_row.get('columns', {})
    out = []
    for fiscal_key, cell in columns.items():
        eps_val = _parse_naver_number(cell.get('value') if isinstance(cell, dict) else cell)
        if eps_val is None:
            continue
        label, rep_date = _fiscal_to_report_quarter(fiscal_key)
        if not label:
            continue
        is_cons = consensus_map.get(fiscal_key, False)
        if is_cons and not include_consensus:
            continue
        out.append({
            'quarter':       label,
            'report_date':   rep_date,
            'eps_actual':    None if is_cons else eps_val,
            'eps_estimate': eps_val if is_cons else None,
            'surprise_pct':  None,
            'is_consensus':  is_cons,
            'fiscal_period': fiscal_key,
        })

    out.sort(key=lambda e: e['report_date'])
    return out


def fetch_naver_latest_earnings_disclosure(stock_code):
    """
    從 Naver disclosure list 找最近一筆「業績」相關 disclosure 的日期（YYYY-MM-DD）。
    用於修正 next_earnings_date：若此日期 > yfinance 給的 next_earnings_date，
    意味著上一次業績已發布，next 不應該還在過去。

    回傳 ('YYYY-MM-DD', title) 或 (None, None)
    """
    url = f'https://m.stock.naver.com/api/stock/{stock_code}/disclosure'
    try:
        items = _http_json(url)
    except Exception as e:
        print(f'    [Naver] {stock_code} disclosure FAIL: {e}', flush=True)
        return None, None

    if not isinstance(items, list):
        return None, None

    for item in items:  # disclosure list 已依時間 desc
        title = (item.get('title') or '')
        if any(k in title for k in _EARNINGS_DISCLOSURE_KEYWORDS):
            dt_raw = item.get('datetime') or ''
            # '2026-04-30T08:46:11' → '2026-04-30'
            m = re.match(r'(\d{4}-\d{2}-\d{2})', dt_raw)
            if m:
                return m.group(1), title.strip()
    return None, None


def stock_code_from_yf_symbol(yf_symbol):
    """ '005930.KS' → '005930'；非 .KS 標的回 None """
    if not yf_symbol or not yf_symbol.upper().endswith('.KS'):
        return None
    return yf_symbol.split('.')[0]


# ─────────────────────────────── CLI 自測 ───────────────────────────────
if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    code = sys.argv[1] if len(sys.argv) > 1 else '005930'
    print(f'=== Naver EPS history for {code} ===')
    rows = fetch_naver_eps_history(code, include_consensus=True)
    for r in rows:
        tag = 'CONS' if r['is_consensus'] else 'ACTL'
        v = r['eps_estimate'] if r['is_consensus'] else r['eps_actual']
        print(f"  {r['quarter']:8s}  {r['report_date']}  fiscal={r['fiscal_period']}  [{tag}]  {v}")
    print(f'\n=== Latest earnings disclosure ===')
    d, t = fetch_naver_latest_earnings_disclosure(code)
    print(f'  {d}  {t}')
