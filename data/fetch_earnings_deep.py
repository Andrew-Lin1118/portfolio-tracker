# -*- coding: utf-8 -*-
"""
fetch_earnings_deep.py
========================
針對每個美股持倉抓取「最新一期財報」的深度分析所需資料，
寫入 data/earnings_analysis.json，供前端「📊 財報分析」分頁使用。

只抓「最近 90 天內」公布過財報的標的（避免分析過時資料）。
之後有新財報就自動追加，不會重抓舊的。

抓取資料：
  - 最近 8 季 income statement（總營收、毛利、營業利益、淨利、EPS）
  - 最近 8 季 cash flow（營業現金流）
  - YoY / QoQ 變化
  - 公司名稱、產業、最新公布日期
  - 近 30 天分析師評級異動（從 fundamentals.json 取）
  - Heuristic 亮點/警訊（依數字推導）

執行：
    python data/fetch_earnings_deep.py [--all]   # --all 強制全部重抓
排程建議：每日一次（GitHub Actions schedule）

依賴：yfinance（已在 update_fund.py 用）
"""
import os, sys, json, datetime, time
import argparse

# 讀持倉清單（從哪裡取？沒有 holdings 來源，改用 fundamentals.json 內的代碼）
ROOT = os.path.dirname(os.path.abspath(__file__))
FUND_FILE = os.path.join(ROOT, 'fundamentals.json')
OUT_FILE  = os.path.join(ROOT, 'earnings_analysis.json')

OFFICIAL_QUARTER_PATCHES = {
    # Seagate fiscal Q3 2026 official earnings release (published 2026-04-28).
    # yfinance quarterly statements can lag by a few days after the release;
    # keep this patch until upstream includes the 2026-04 fiscal quarter.
    'STX': {
        'period': '2026-04-03',
        'revenue': 3112000000.0,
        'gross_profit': 1447000000.0,
        'operating_income': 998000000.0,
        'net_income': 748000000.0,
        'eps_actual': 3.27,
        'gross_margin': 1447000000.0 / 3112000000.0 * 100,
        'operating_margin': 998000000.0 / 3112000000.0 * 100,
        'operating_cash_flow': 571000000.0,
        'source': 'Seagate fiscal Q3 2026 earnings release',
        'published_date': '2026-04-28',
    },
    # SK hynix 1Q26 official earnings release (published 2026-04-23).
    # yfinance may lag for Korean quarterly statements, so keep this patch
    # until the upstream statement includes the 2026-03 quarter.
    '000660.KS': {
        'period': '2026-03-31',
        'revenue': 52576300000000.0,
        'gross_profit': None,
        'operating_income': 37610300000000.0,
        'net_income': 40345900000000.0,
        'eps_actual': None,
        'gross_margin': None,
        'operating_margin': 37610300000000.0 / 52576300000000.0 * 100,
        'operating_cash_flow': None,
        'source': 'SK hynix 1Q26 earnings release',
        'published_date': '2026-04-23',
    },
}


def _load_existing():
    if not os.path.exists(OUT_FILE):
        return {}
    try:
        with open(OUT_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return d.get('data', {}) if isinstance(d, dict) else {}
    except Exception:
        return {}


def _load_fundamentals():
    if not os.path.exists(FUND_FILE):
        return {}, {}
    with open(FUND_FILE, encoding='utf-8') as f:
        d = json.load(f)
    fdata = d.get('data', d) if isinstance(d, dict) else {}
    # 兩種可能：{symbol: {...}} 或 {data: {symbol: {...}}}
    return fdata, d.get('generated', '')


def _yoy(cur, year_ago):
    if cur is None or year_ago is None:
        return None
    try:
        if abs(year_ago) < 1e-6:
            return None
        return (cur - year_ago) / abs(year_ago) * 100
    except Exception:
        return None


def _safe_float(v):
    try:
        if v is None: return None
        f = float(v)
        if f != f or f in (float('inf'), float('-inf')): return None
        return f
    except Exception:
        return None


def _calc_chg(cur_v, base_v):
    if cur_v is None or base_v is None or base_v == 0:
        return None
    return (cur_v - base_v) / abs(base_v) * 100


def _apply_official_quarter_patches(data):
    for sym, patch in OFFICIAL_QUARTER_PATCHES.items():
        entry = data.get(sym)
        if not isinstance(entry, dict):
            continue
        quarters = entry.get('quarters') or []
        quarters = [q for q in quarters if q.get('period') != patch['period']]
        quarters.insert(0, dict(patch))
        quarters = [q for q in quarters if q.get('revenue') is not None and q.get('net_income') is not None]
        quarters.sort(key=lambda q: q.get('period') or '', reverse=True)

        for i, q in enumerate(quarters):
            prev = quarters[i + 1] if i + 1 < len(quarters) else None
            yr_ago = next((p for p in quarters[i + 1:] if (p.get('period') or '')[:4] == str(int((q.get('period') or '0000')[:4]) - 1)
                           and (p.get('period') or '')[5:7] == (q.get('period') or '')[5:7]), None)
            q['rev_yoy'] = _calc_chg(q.get('revenue'), yr_ago and yr_ago.get('revenue'))
            q['rev_qoq'] = _calc_chg(q.get('revenue'), prev and prev.get('revenue'))
            q['eps_yoy'] = _calc_chg(q.get('eps_actual'), yr_ago and yr_ago.get('eps_actual'))
            q['eps_qoq'] = _calc_chg(q.get('eps_actual'), prev and prev.get('eps_actual'))

        entry['quarters'] = quarters
        entry['last_report_date'] = quarters[0]['period']
        entry['highlights'], entry['warnings'] = _heuristic_highlights(quarters)


def _heuristic_highlights(qtrs):
    """
    根據數字產生「亮點 / 警訊」(中文短句)
    qtrs: [最新, 上一季, 上上季 ...] 每筆含 revenue, gross_margin, operating_margin, net_income, eps, cash_flow
    """
    highlights = []
    warnings  = []
    if not qtrs or not qtrs[0]:
        return highlights, warnings
    cur = qtrs[0]
    prev = qtrs[1] if len(qtrs) > 1 else None
    yr_ago = qtrs[4] if len(qtrs) > 4 else None

    # 營收
    if cur.get('revenue') and yr_ago and yr_ago.get('revenue'):
        yoy = _yoy(cur['revenue'], yr_ago['revenue'])
        if yoy is not None:
            if yoy >= 30:    highlights.append(f'營收年增 {yoy:+.1f}%，高速成長')
            elif yoy >= 15:  highlights.append(f'營收年增 {yoy:+.1f}%，穩健成長')
            elif yoy >= 5:   highlights.append(f'營收年增 {yoy:+.1f}%，緩步擴張')
            elif yoy <= -10: warnings.append(f'營收年減 {yoy:+.1f}%，需留意需求疲軟')
            elif yoy <  0:   warnings.append(f'營收年減 {yoy:+.1f}%，動能轉弱')

    # 毛利率變動
    if cur.get('gross_margin') is not None and yr_ago and yr_ago.get('gross_margin') is not None:
        chg = cur['gross_margin'] - yr_ago['gross_margin']
        if chg >= 2:    highlights.append(f'毛利率年增 {chg:+.1f}pt，產品組合或定價力提升')
        elif chg <= -2: warnings.append(f'毛利率年減 {chg:+.1f}pt，留意成本壓力')

    # 營益率
    if cur.get('operating_margin') is not None and yr_ago and yr_ago.get('operating_margin') is not None:
        chg = cur['operating_margin'] - yr_ago['operating_margin']
        if chg >= 2:    highlights.append(f'營益率年增 {chg:+.1f}pt，營運槓桿發揮')
        elif chg <= -3: warnings.append(f'營益率年減 {chg:+.1f}pt，營運費用控制需注意')

    # 淨利率
    if cur.get('revenue') and cur.get('net_income') and cur['revenue'] != 0:
        nm_cur = cur['net_income'] / cur['revenue'] * 100
        if yr_ago and yr_ago.get('revenue') and yr_ago.get('net_income') and yr_ago['revenue'] != 0:
            nm_yr = yr_ago['net_income'] / yr_ago['revenue'] * 100
            chg = nm_cur - nm_yr
            if chg >= 3:    highlights.append(f'淨利率 {nm_cur:.1f}%，年增 {chg:+.1f}pt')
            elif chg <= -3: warnings.append(f'淨利率 {nm_cur:.1f}%，年減 {chg:+.1f}pt')

    # EPS surprise
    if cur.get('eps_actual') is not None and cur.get('eps_estimate') is not None:
        e_act, e_est = cur['eps_actual'], cur['eps_estimate']
        if e_est != 0:
            surp = (e_act - e_est) / abs(e_est) * 100
            if surp >= 10:   highlights.append(f'EPS ${e_act:.2f} 超越預期 {surp:+.1f}%')
            elif surp >= 3:  highlights.append(f'EPS 微幅超越預期 {surp:+.1f}%')
            elif surp <= -10:warnings.append(f'EPS ${e_act:.2f} 不如預期 {surp:+.1f}%')
            elif surp < 0:   warnings.append(f'EPS 略低於預期 {surp:+.1f}%')

    # 營運現金流
    if cur.get('operating_cash_flow') is not None and yr_ago and yr_ago.get('operating_cash_flow') is not None:
        if cur['operating_cash_flow'] < 0 and yr_ago['operating_cash_flow'] >= 0:
            warnings.append('營業現金流由正轉負，現金生成能力惡化')
        elif cur['operating_cash_flow'] > yr_ago['operating_cash_flow'] * 1.3:
            highlights.append('營業現金流年增超過 30%，現金品質佳')

    return highlights, warnings


def fetch_one(symbol, fund):
    """為單一 symbol 抓深度財報資料"""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)

        # 公司資訊
        info = {}
        try:
            info = t.info or {}
        except Exception:
            info = {}

        # 季度損益（最近 8 季）
        try:
            qis = t.quarterly_income_stmt
        except Exception:
            qis = None
        try:
            qcf = t.quarterly_cashflow
        except Exception:
            qcf = None

        if qis is None or qis.empty:
            return None

        cols = list(qis.columns)[:8]    # 最新 8 季
        cols.sort(reverse=True)         # 確保最新在前

        # 從 yfinance income statement 抽欄位（不同公司欄位名可能略不同，list 出 fallback）
        def _get_row(df, candidates, col):
            if df is None: return None
            for name in candidates:
                if name in df.index:
                    try:
                        v = df.at[name, col]
                        if v is None: continue
                        return _safe_float(v)
                    except Exception:
                        continue
            return None

        qtrs = []
        for col in cols:
            rev = _get_row(qis, ['Total Revenue', 'TotalRevenue'], col)
            gp  = _get_row(qis, ['Gross Profit', 'GrossProfit'], col)
            op  = _get_row(qis, ['Operating Income', 'OperatingIncome', 'EBIT'], col)
            ni  = _get_row(qis, ['Net Income', 'NetIncome', 'Net Income Common Stockholders'], col)
            eps = _get_row(qis, ['Diluted EPS', 'Basic EPS'], col)
            ocf = _get_row(qcf, ['Operating Cash Flow', 'OperatingCashFlow', 'Total Cash From Operating Activities'], col)

            gm = (gp / rev * 100) if (gp is not None and rev and rev != 0) else None
            om = (op / rev * 100) if (op is not None and rev and rev != 0) else None

            qtrs.append({
                'period': col.strftime('%Y-%m-%d') if hasattr(col, 'strftime') else str(col),
                'revenue':  rev,
                'gross_profit':    gp,
                'operating_income': op,
                'net_income':       ni,
                'eps_actual':       eps,
                'gross_margin':     gm,
                'operating_margin': om,
                'operating_cash_flow': ocf,
            })

        if not qtrs:
            return None

        # earnings_history 對齊（取 EPS estimate）
        eh = (fund or {}).get('earnings_history') or []
        for q in qtrs:
            for e in eh:
                if e.get('report_date') and e['report_date'].startswith(q['period'][:7]):
                    q['eps_estimate'] = _safe_float(e.get('eps_estimate'))
                    if q.get('eps_actual') is None:
                        q['eps_actual'] = _safe_float(e.get('eps_actual'))
                    q['surprise_pct'] = _safe_float(e.get('surprise_pct'))
                    break

        # yfinance sometimes exposes an earnings-date-only quarter before the full
        # income statement is available (TSM can show EPS without revenue/margins).
        # Do not treat those partial rows as a completed financial quarter.
        qtrs = [q for q in qtrs if q.get('revenue') is not None and q.get('net_income') is not None]
        if not qtrs:
            return None

        latest = qtrs[0]
        # 計算 YoY、QoQ
        yr_ago = qtrs[4] if len(qtrs) > 4 else None
        prev   = qtrs[1] if len(qtrs) > 1 else None

        latest['rev_yoy'] = _calc_chg(latest.get('revenue'), yr_ago and yr_ago.get('revenue'))
        latest['rev_qoq'] = _calc_chg(latest.get('revenue'), prev   and prev.get('revenue'))
        latest['eps_yoy'] = _calc_chg(latest.get('eps_actual'), yr_ago and yr_ago.get('eps_actual'))
        latest['eps_qoq'] = _calc_chg(latest.get('eps_actual'), prev   and prev.get('eps_actual'))

        highlights, warnings = _heuristic_highlights(qtrs)

        return {
            'symbol':           symbol,
            'name':             info.get('shortName') or info.get('longName') or symbol,
            'sector':           info.get('sector') or '',
            'industry':         info.get('industry') or '',
            'currency':         info.get('financialCurrency') or info.get('currency') or 'USD',
            'last_report_date': qtrs[0]['period'],
            'quarters':         qtrs,
            'highlights':       highlights,
            'warnings':         warnings,
            'next_earnings_date': (fund or {}).get('next_earnings_date'),
            'target_mean_price': (fund or {}).get('target_mean_price'),
            'number_of_analysts': (fund or {}).get('number_of_analysts'),
            'pe':  (fund or {}).get('pe'),
            'fpe': (fund or {}).get('fpe'),
            'fetched_at': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        }
    except Exception as e:
        print(f'  [{symbol}] 失敗：{type(e).__name__}: {str(e)[:120]}', flush=True)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true', help='強制重抓所有持倉（預設只抓近 90 天有發財報的）')
    ap.add_argument('--days', type=int, default=90, help='只抓 N 天內公布財報的標的（預設 90）')
    args = ap.parse_args()

    fdata, fund_gen = _load_fundamentals()
    if not fdata:
        print('沒有 fundamentals.json，先跑 update_fund.py', flush=True)
        return

    existing = _load_existing()
    _apply_official_quarter_patches(existing)

    # 決定要抓的清單
    today_iso = datetime.date.today().isoformat()
    cutoff = (datetime.date.today() - datetime.timedelta(days=args.days)).isoformat()
    targets = []
    for sym, item in fdata.items():
        if not isinstance(item, dict): continue
        # 接受所有市場（含 HK / KS / TW 等），由 yfinance 自行判斷有無季度資料
        # （之前過濾 '.' in sym 會誤殺槓桿 ETF 對應的韓股 000660.KS / 005930.KS）
        eh = item.get('earnings_history') or []
        latest_rd = None
        for e in eh:
            rd = e.get('report_date')
            if rd and (latest_rd is None or rd > latest_rd):
                latest_rd = rd
        if not latest_rd: continue
        if not args.all and latest_rd < cutoff: continue
        # 已抓過且資料中財報日 == 最新財報日 → 跳過（同月份就視為已分析）
        if not args.all:
            ex = existing.get(sym)
            if ex and ex.get('last_report_date', '').startswith(latest_rd[:7]):
                continue
        targets.append(sym)

    if not targets:
        print(f'近 {args.days} 天無新財報需要分析（{len(existing)} 已有資料）', flush=True)
        # 即使沒新資料也寫一次 generated 時間
        payload = {
            'generated': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'data': existing,
        }
        with open(OUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return

    print(f'要分析 {len(targets)} 檔：{", ".join(targets)}', flush=True)

    for i, sym in enumerate(targets, 1):
        print(f'  ({i}/{len(targets)}) {sym} ...', flush=True)
        result = fetch_one(sym, fdata.get(sym, {}))
        if result:
            existing[sym] = result
            print(f'    ✓ {result["last_report_date"]} · 亮點 {len(result["highlights"])} · 警訊 {len(result["warnings"])}', flush=True)
        time.sleep(0.5)

    _apply_official_quarter_patches(existing)

    payload = {
        'generated': datetime.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'data': existing,
    }
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'\n寫入 → {OUT_FILE}（共 {len(existing)} 檔分析）', flush=True)


if __name__ == '__main__':
    main()
